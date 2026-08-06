# capturekit

Capture a macOS display, send it to Claude with a prompt of your choosing, and
keep the raw log, the cumulative state, and every request/response pair on disk.

Extracted from the `alakazam` solver — this is that project's capture → API →
logging spine with the renderers, viewers, streaming, and delivery removed.

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in ANTHROPIC_API_KEY
python run.py --list          # what's defined
python run.py --prompt sample --once
python run.py --prompt sample --interval 30
```

Grant Terminal (or your IDE) **Screen Recording** permission the first time —
System Settings → Privacy & Security → Screen Recording. Without it,
`screencapture` silently returns a picture of the desktop wallpaper.

## Adding a prompt

Everything you configure lives in `prompts.py`. Add an entry:

```python
PROMPTS = {
    "watch": Prompt(
        system="You are watching a build log. Report only new errors.",
        model="claude-sonnet-5",     # per-prompt model — the point of the file
        effort="low",
    ),
}
```

Then `python run.py --prompt watch`.

`Prompt` fields:

| Field | Default | What it does |
|---|---|---|
| `system` | — | The system prompt. Cached, so a long one is cheap to re-send. |
| `model` | `claude-opus-5` | Exact ID. `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5`, `claude-fable-5`. |
| `effort` | `medium` | `low`–`max`. Thinking depth and total token spend. |
| `max_tokens` | `16000` | Ceiling on thinking + response combined. |
| `user_suffix` | `"Analyze this screenshot."` | Trailing text of the user message. |
| `json` | `False` | Parse the reply as JSON instead of saving it as text. |
| `state_sections` | `()` | Cumulative state-file sections. Empty = no state file. |
| `state_key` | `"state"` | Which JSON key holds the section dict. |
| `no_shrink_sections` | `()` | Sections where a reply that drops lines is refused. |

`--model` and `--effort` override an entry for one run without editing it.

## Text prompts vs JSON prompts

**`json=False`** (the `sample` entry): the reply is saved verbatim to
`output/raw/<session>-<n>.txt`. Nothing is parsed. Write whatever prompt you
like.

**`json=True`** (the `structured` entry): the reply goes through a
brace-balanced extractor that tolerates prose around the object, then — if
`state_sections` is set — merges into this session's state file. The reply
shape is:

```json
{"status": "...", "state": {"<Section>": "<text>"}, "result": "..."}
```

## The state file

`log/state/<session>.md` is **cumulative knowledge**, not a log of what each
screenshot showed. It is re-sent to the model with every capture, which is what
lets a later screenshot build on an earlier one. Three rules it enforces:

- **Nothing deletes knowledge.** A section the model omits (or returns as the
  literal string `"unchanged"`) keeps its prior body. An empty section over a
  non-empty prior is refused and noted. Content that scrolled off screen
  survives a capture that does not show it.
- **`## History` stays last** and append-only, so it can never displace a
  section. It is capped to the last 6 entries *on the way out to the API* —
  the file on disk stays complete.
- **The write is atomic** (tmp + `os.replace`), because a half-written file
  would be sent to the model verbatim on the next cycle.

Put source code, long lists, or anything else transcribed off-screen into
`no_shrink_sections` — a reply that drops lines from those is refused rather
than applied.

## What lands where

```
output/raw/<session>-<n>.txt     the reply body, one file per cycle
log/raw/<session>.txt            every terminal line, ANSI-stripped
log/state/<session>.md           the cumulative state file
log/screenshots/<session>-<n>.webp   the exact image uploaded (not the capture)
log/calls/<n>.json               request + response, minus the image and the
                                 system prompt body (length + sha256 only)
log/calls/<n>_tokens.json        per-call usage
/tmp/capturekit/current.png      the last full-resolution capture
```

Log filenames use a sortable `YYYY-MM-DD_HHMMSS` stem, shared between a
session's log and its state file.

## Notes

- **Capture is macOS-only** — `capture.py` shells out to `screencapture` and
  `sips`. Everything else is portable; swap that one module to port the kit.
- **Display auto-detection prefers the secondary monitor.** A display counts
  only if `screencapture` exits 0 *and* the probe file is over 1024 bytes; with
  several found, the first index above 1 wins. `--display N` overrides.
- **Screenshots are downscaled to 2560px on the long edge** and re-encoded as
  WebP before upload — typically a 93–97% size reduction with no visible loss.
  The on-disk capture stays a full-resolution PNG. Read the comment in
  `image_processing.py` before lowering that number; 1568 halves the token cost
  and measurably breaks text-heavy screenshots.
- **The system prompt is cached with a 1-hour TTL**, because the loop re-sends
  the same prefix every cycle. Caching has a **minimum prefix length** (512
  tokens on Claude Opus 5, 1024 on Sonnet 5) and short prompts silently fall
  below it — `cache_read=0` on a ~700-character system prompt is expected, not
  a fault. It starts paying off once your prompt is long enough to matter.
- **`effort` is omitted when set to `""`.** Some models reject the parameter
  outright — Haiku 4.5 among them — so an empty effort means "don't send it",
  not "send the default".
- **All calls stream** and use adaptive thinking. `budget_tokens`,
  `temperature`, `top_p`, and `top_k` are rejected by current models; effort is
  the depth control.
- **A bad cycle never ends the run.** API errors, capture failures, and
  unparseable JSON are logged and the loop continues to the next interval.
# project-x-
