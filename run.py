#!/usr/bin/env python3
"""Capture a screen, send it to Claude, log the reply. Repeat.

    python run.py --prompt sample
    python run.py --prompt structured --interval 60
    python run.py --prompt sample --image shot.png --once

Everything type-specific lives in prompts.py; this file is only the loop.
"""

import argparse
import json
import time
from pathlib import Path

import anthropic

import capture as capture_mod
import claude_client
import prompts
import session
from state import State


def parse_args():
    p = argparse.ArgumentParser(
        description="Screen capture -> Claude -> raw log + cumulative state.")
    p.add_argument("--prompt", metavar="NAME",
                   help=f"which prompt to run. One of: {', '.join(prompts.names())}")
    p.add_argument("--list", action="store_true",
                   help="list the defined prompts and exit")
    p.add_argument("--display", type=int, default=None, metavar="N",
                   help="display index; omit to auto-detect (prefers secondary)")
    p.add_argument("--interval", type=int, default=30, metavar="SECONDS",
                   help="seconds between captures (default 30; 0 = run once)")
    p.add_argument("--once", action="store_true",
                   help="capture once and exit (same as --interval 0)")
    p.add_argument("--image", metavar="PATH", default=None,
                   help="skip capture and send this file instead (implies --once)")
    p.add_argument("--model", default="", metavar="ID",
                   help="override the prompt's model for this run")
    p.add_argument("--effort", default="", metavar="LEVEL",
                   choices=["", "low", "medium", "high", "xhigh", "max"],
                   help="override the prompt's effort for this run")
    return p.parse_args()


def _startup(prompt, args, stem, interval):
    session.log("capturekit starting")
    rows = [
        ("session:", stem),
        ("prompt:", args.prompt),
        ("model:", prompt.model),
        ("effort:", prompt.effort),
        ("format:", "json" if prompt.json else "text"),
        ("state:", ", ".join(prompt.state_sections) or "off"),
        ("interval:", "single capture" if interval <= 0 else f"{interval}s"),
    ]
    for label, value in rows:
        session.log(f"  {label:<10}{value}", plain=True)


def run(args, prompt, stem) -> int:
    interval = 0 if (args.once or args.image) else max(0, args.interval)
    _startup(prompt, args, stem, interval)

    client = claude_client.build_client()

    fixed_image = Path(args.image) if args.image else None
    if fixed_image is not None:
        if not fixed_image.exists():
            raise SystemExit(f"[ERROR] No such image: {fixed_image}")
        session.log(f"  {'image:':<10}{fixed_image}", plain=True)
        display_idx = None
    else:
        display_idx = capture_mod.detect_display(args.display)

    state = State(session.state_path(), prompt.state_sections,
                  no_shrink=prompt.no_shrink_sections,
                  title=args.prompt) if prompt.state_sections else None

    if interval > 0:
        session.log("Press Ctrl+C to stop.", plain=True)

    while True:
        session.log()                       # blank separator per cycle
        cycle = session.next_cycle()

        if fixed_image is not None:
            image_path = fixed_image
        else:
            try:
                image_path = capture_mod.capture(display_idx)
                session.log("Screenshot captured")
            except RuntimeError as e:
                session.log(f"WARNING: capture failed ({e})", tag="capture")
                if interval <= 0:
                    return 1
                time.sleep(interval)
                continue

        try:
            reply = claude_client.call(
                client, prompt, image_path,
                state_text=state.read() if state else "",
                cycle=cycle,
            )
        except anthropic.APIError as e:
            # Rate limits, overloads, transport failures. One bad cycle must
            # never end the run.
            session.log(f"API error: {e}", tag="api")
            if interval <= 0:
                return 1
            time.sleep(interval)
            continue
        except json.JSONDecodeError as e:
            # The full response body is in log/calls/ — a reply that did not
            # parse is exactly the one worth reading back.
            session.log(f"Reply was not valid JSON ({e}) — "
                        f"see log/calls/", tag="api")
            if interval <= 0:
                return 1
            time.sleep(interval)
            continue

        session.log(f"Reply received in {reply.elapsed:.1f}s"
                    + (f" — {reply.clause}" if reply.clause else ""),
                    tag="api")
        session.save_response(reply.text, cycle)

        if state is not None:
            payload = reply.parsed if isinstance(reply.parsed, dict) else {}
            state.update(payload.get(prompt.state_key),
                         timestamp=session.ts(),
                         status=str(payload.get("status") or ""))

        if interval <= 0:
            return 0
        time.sleep(interval)


def main() -> int:
    args = parse_args()
    if args.list:
        for name in prompts.names():
            entry = prompts.PROMPTS[name]
            kind = "json" if entry.json else "text"
            print(f"{name:<16} {entry.model:<20} {entry.effort:<8} {kind}")
        return 0
    if not args.prompt:
        raise SystemExit(
            f"[ERROR] --prompt is required. Available: "
            f"{', '.join(prompts.names())} (or --list)")

    prompt = prompts.get(args.prompt).resolved(args.model, args.effort)
    stem = session.start_logging()
    try:
        return run(args, prompt, stem)
    except KeyboardInterrupt:
        session.log()
        session.log("capturekit stopped")
        return 0
    finally:
        session.close_logging()


if __name__ == "__main__":
    raise SystemExit(main())
