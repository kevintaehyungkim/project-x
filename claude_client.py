"""The Claude call: image in, text (or parsed JSON) out.

One request per capture. The system prompt is cached, the image is
preprocessed and sent first, and every call is persisted to log/calls/ so a
surprising answer can be read back later.
"""

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anthropic

import image_processing
from prompts import Prompt
from session import LOG_CALL_DIR, log, save_screenshot

# Adaptive thinking: the model decides when and how much to think. Never
# `budget_tokens` — that shape is rejected outright on current models; depth is
# controlled by Prompt.effort instead.
THINKING = {"type": "adaptive"}


def build_client() -> anthropic.Anthropic:
    """Construct the client, or exit with a message that says what to fix."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit(
            "[ERROR] ANTHROPIC_API_KEY is not set. Copy .env.example to .env "
            "and fill it in, or export the variable.")
    return anthropic.Anthropic(api_key=api_key)


def _cache_control() -> dict:
    """cache_control for the system block.

    1h rather than the 5-minute default: this kit re-sends the same system
    prefix every cycle with a multi-second call in between, so a 5-minute
    entry expires on roughly half the cycles of a slow interval. A 1h entry
    costs 2x on write against 1.25x, and pays that back after three reads."""
    return {"type": "ephemeral", "ttl": "1h"}


def _response_text(message) -> str:
    """Text of a response's first text block.

    With adaptive thinking enabled, thinking blocks precede the text block, so
    content[0] is not the answer."""
    for block in message.content:
        if block.type == "text":
            return block.text
    raise ValueError("model response contained no text block")


def _usage_dict(response) -> dict:
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        return {
            # Read off the RESPONSE, not the request constant — this is what
            # the API actually served.
            "model": str(getattr(response, "model", "") or ""),
            "input_tokens": inp,
            "output_tokens": out,
            "total": inp + out,
            "cache_read_input_tokens":
                getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens":
                getattr(usage, "cache_creation_input_tokens", 0) or 0,
        }
    except Exception:
        return {}


def _token_clause(data: dict) -> str:
    if not data:
        return ""
    parts = [f"in={data['input_tokens']}", f"out={data['output_tokens']}",
             f"total={data['total']}"]
    if data["cache_read_input_tokens"]:
        parts.append(f"cache_read={data['cache_read_input_tokens']}")
    if data["cache_creation_input_tokens"]:
        parts.append(f"cache_write={data['cache_creation_input_tokens']}")
    clause = "tokens: " + " ".join(parts)
    return f"model={data['model']} {clause}" if data["model"] else clause


def extract_json_object(raw: str) -> dict:
    """Extract and parse the outermost JSON object from a model response.

    The response may contain prose (even prose with stray braces, e.g. trace
    notation like ``{u1}``) before the actual JSON object, so every ``{`` is
    tried in order until one brace-balanced candidate parses. `strict=False`
    because code and transcribed text routinely carry raw control characters
    inside the strings."""
    start = raw.find('{')
    if start == -1:
        raise json.JSONDecodeError("No JSON object found in response", raw, 0)
    last_err = None
    while start != -1:
        depth = 0
        in_str = escape_next = False
        closed = False
        for i, ch in enumerate(raw[start:], start):
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_str:
                escape_next = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    closed = True
                    try:
                        return json.loads(raw[start:i + 1], strict=False)
                    except json.JSONDecodeError as e:
                        last_err = e
                    break
        if not closed and last_err is None:
            last_err = json.JSONDecodeError(
                "Truncated JSON response (no closing brace — raise max_tokens)",
                raw, len(raw))
        start = raw.find('{', start + 1)
    raise last_err


@dataclass
class Reply:
    text: str
    parsed: dict | None = None
    elapsed: float = 0.0
    usage: dict = field(default_factory=dict)

    @property
    def clause(self) -> str:
        return _token_clause(self.usage)


_CALL_N = 0
_CALL_WARNED = False


def _save_call(prompt: Prompt, request: dict, response, raw: str, *,
               elapsed: float, parsed=None, error: str = ""):
    """Persist one API call to log/calls/.

    Two deliberate omissions: the base64 IMAGE (log/screenshots/ already holds
    the exact bytes uploaded) and the SYSTEM PROMPT body (only its length and
    sha256 — enough to prove which prompt served a call, and to spot a prompt
    change, without writing it to disk on every cycle).

    A parse failure is recorded too — a response that did not become usable
    output is exactly the one worth reading back. Never raises."""
    global _CALL_N, _CALL_WARNED
    _CALL_N += 1
    n = _CALL_N
    try:
        LOG_CALL_DIR.mkdir(parents=True, exist_ok=True)
        usage = _usage_dict(response) if response is not None else {}
        stamp = datetime.now().isoformat(timespec="seconds")
        record = {
            "n": n,
            "at": stamp,
            "elapsed_s": round(float(elapsed), 3),
            "model": usage.get("model", ""),
            "stop_reason": str(getattr(response, "stop_reason", "") or ""),
            "request": request,
            "response_text": raw,
            "parsed": parsed,
            "parse_error": error,
        }
        (LOG_CALL_DIR / f"{n}.json").write_text(
            json.dumps(record, indent=2, default=str), encoding="utf-8")
        (LOG_CALL_DIR / f"{n}_tokens.json").write_text(
            json.dumps({"n": n, "at": stamp,
                        "elapsed_s": round(float(elapsed), 3), **usage},
                       indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - never fatal
        if not _CALL_WARNED:
            _CALL_WARNED = True
            log(f"WARNING: could not record API calls ({e}) — "
                f"continuing without them.", tag="state")


def call(client: anthropic.Anthropic, prompt: Prompt, image_path: Path, *,
         state_text: str = "", extra_context: str = "",
         cycle: int = 0) -> Reply:
    """Send one screenshot and return the reply.

    Raises anthropic.APIError on a transport/API failure and
    json.JSONDecodeError when a `json=True` prompt returns something
    unparseable — the caller decides whether that ends the run."""
    image_bytes, media_type, img_detail = \
        image_processing.prepare_image_for_api(image_path)
    log("Request sent" + (f" — {img_detail}" if img_detail else ""),
        tag="api", flush=True)
    save_screenshot(image_bytes, media_type, cycle)
    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")

    text = ""
    if extra_context:
        text += extra_context.rstrip() + "\n\n"
    if state_text:
        text += (
            "Cumulative state file (what is already known — merge into it, do "
            "not replace it with only what is visible now):\n"
            + state_text + "\n\n"
        )
    text += prompt.user_suffix

    params = dict(
        max_tokens=prompt.max_tokens,
        thinking=THINKING,
        system=[{
            "type": "text",
            "text": prompt.system,
            "cache_control": _cache_control(),
        }],
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_data,
                    },
                },
                {"type": "text", "text": text},
            ],
        }],
    )
    # Omitted rather than defaulted: `effort` is rejected outright by some
    # models (Haiku 4.5 among them), so an empty effort has to mean "don't
    # send the parameter", not "send the default".
    if prompt.effort:
        params["output_config"] = {"effort": prompt.effort}

    record = {
        "model": prompt.model,
        "effort": prompt.effort,
        "max_tokens": prompt.max_tokens,
        "system_chars": len(prompt.system),
        "system_sha256": hashlib.sha256(
            prompt.system.encode("utf-8")).hexdigest(),
        "extra_context": extra_context,
        "user_text": text,
        "image": {"media_type": media_type,
                  "bytes": len(image_bytes or b""),
                  "detail": img_detail},
    }

    t0 = time.monotonic()
    # Streaming is required, not stylistic: a non-streaming request with a
    # large max_tokens is refused by the SDK (and would risk an HTTP timeout).
    with client.messages.stream(model=prompt.model, **params) as stream:
        response = stream.get_final_message()
    elapsed = time.monotonic() - t0

    raw = _response_text(response).strip()
    usage = _usage_dict(response)

    parsed = None
    if prompt.json:
        try:
            parsed = extract_json_object(raw)
        except Exception as e:
            _save_call(prompt, record, response, raw, elapsed=elapsed,
                       error=f"{type(e).__name__}: {e}")
            raise

    _save_call(prompt, record, response, raw, elapsed=elapsed, parsed=parsed)
    return Reply(text=raw, parsed=parsed, elapsed=elapsed, usage=usage)
