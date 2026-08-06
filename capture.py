"""macOS screen capture: probe the displays, grab one of them.

Ported from the solver's capture path. Shells out to `/usr/sbin/screencapture`
and `sips`, so this module is macOS-only; everything else in the kit is
portable.
"""

import subprocess
import sys
from pathlib import Path

# The last two captures stay on disk for manual inspection only. Nothing reads
# previous.png — the rotation exists so you can eyeball what changed between
# two cycles without instrumenting anything.
WORK_DIR = Path("/tmp/capturekit")

# How many display indices to probe before giving up.
MAX_PROBE = 4


def get_resolution(path: Path) -> str:
    r = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        capture_output=True, text=True
    )
    w = h = "?"
    for line in r.stdout.splitlines():
        if "pixelWidth:" in line:
            w = line.split()[-1]
        elif "pixelHeight:" in line:
            h = line.split()[-1]
    return f"{w}×{h}"


def detect_display(override=None) -> int:
    """Pick a display index, probing each one by actually capturing it.

    Two heuristics, both load-bearing:

    - a display counts only if screencapture exits 0 AND the probe file is
      larger than 1024 bytes. A missing display can still exit 0 and leave a
      near-empty file behind.
    - with several displays, the FIRST index above 1 wins. Index 1 is the
      built-in screen; the interesting content is almost always on the
      external monitor.
    """
    if override is not None:
        print(f"  {'display:':<10}index {override} (manual override)")
        return override

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    probe = WORK_DIR / "probe.png"
    found = []

    for idx in range(1, MAX_PROBE + 1):
        probe.unlink(missing_ok=True)
        r = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-D", str(idx), str(probe)],
            capture_output=True
        )
        if r.returncode == 0 and probe.exists() and probe.stat().st_size > 1024:
            found.append((idx, get_resolution(probe)))

    probe.unlink(missing_ok=True)

    if not found:
        sys.exit("[ERROR] No displays detected. Use --display N to specify one.")

    if len(found) == 1:
        idx, res = found[0]
        print(f"  {'display:':<10}index {idx} ({res}, only display found)")
        return idx

    idx, res = next(((i, r) for i, r in found if i > 1), found[0])
    print(f"  {'display:':<10}index {idx} "
          f"({res}, auto-detected secondary from {len(found)} displays)")
    return idx


def capture(display_idx: int) -> Path:
    """Capture one display to WORK_DIR/current.png and return the path.

    `-x` suppresses the shutter sound; `-D N` selects the display. The capture
    is kept at full resolution — downscaling for upload happens later, in
    image_processing, so the on-disk copy stays faithful.
    """
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    curr = WORK_DIR / "current.png"
    prev = WORK_DIR / "previous.png"

    if curr.exists():
        curr.rename(prev)

    r = subprocess.run(
        ["/usr/sbin/screencapture", "-x", "-D", str(display_idx), str(curr)],
        capture_output=True
    )
    if r.returncode != 0 or not curr.exists():
        raise RuntimeError(
            f"screencapture exited {r.returncode}: {r.stderr.decode().strip()}")
    return curr
