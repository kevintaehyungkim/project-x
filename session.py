"""Session logging and output paths.

Every terminal line this kit prints is mirrored, ANSI-stripped, into one
`log/raw/<stem>.txt` per session. The state file shares that stem, so a log
and the knowledge it accumulated are trivially linkable.

Nothing here may crash the capture loop: every write failure is swallowed.
"""

import io
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent

OUTPUT_DIR = ROOT / "output"
RAW_DIR = OUTPUT_DIR / "raw"          # one response body per cycle

LOG_DIR = ROOT / "log"
LOG_RAW_DIR = LOG_DIR / "raw"         # tee'd terminal output, one per session
LOG_STATE_DIR = LOG_DIR / "state"     # cumulative state md, one per session
LOG_SHOT_DIR = LOG_DIR / "screenshots"  # every image actually uploaded
LOG_CALL_DIR = LOG_DIR / "calls"      # one request/response pair per API call

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')

_SESSION_STEM = ""
_LOG_FILE = None
_CYCLE = 0


def ts() -> str:
    """Wall-clock time for a stage line, e.g. '7:02:11 PM'."""
    return datetime.now().strftime("%-I:%M:%S %p")


def session_stem() -> str:
    """Sortable, space-free session stem: 2026-08-06_143002.

    Deliberately not the solver's '8-6-26 @ 14.30.02' — that format exists
    because a reporting tool parses the date back out of the filename, and
    that tool is not part of this kit.
    """
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def log(msg: str = "", tag: str | None = None, *, plain: bool = False, **kw):
    """The canonical stage-line emitter.

    Every stage line leads with the timestamp — `[<time>] message`, or
    `[<time>] [tag] message` for component events (`api`, `capture`, `state`).
    `plain=True` (or an empty message) prints untouched, for indented startup
    metadata rows and the blank separator between cycles.
    """
    if plain or not msg:
        print(msg, **kw)
        return
    print(f"[{ts()}] " + (f"[{tag}] " if tag else "") + msg, **kw)


class _SessionTee(io.TextIOBase):
    """Mirror a terminal stream into the session log file, ANSI-stripped.

    Log-file failures are swallowed so logging can never break the loop."""

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log = log_file

    def write(self, text):
        n = self._stream.write(text)
        if self._log is not None:
            try:
                self._log.write(_ANSI_RE.sub('', text))
                self._log.flush()
            except Exception:
                pass
        return n

    def flush(self):
        self._stream.flush()

    def isatty(self):
        return self._stream.isatty()


def start_logging() -> str:
    """Create log/raw/ and tee stdout/stderr into this session's file.

    Returns the session stem, which names the log, the state file, and every
    output this session writes."""
    global _SESSION_STEM, _LOG_FILE
    _SESSION_STEM = session_stem()
    try:
        LOG_RAW_DIR.mkdir(parents=True, exist_ok=True)
        _LOG_FILE = open(LOG_RAW_DIR / f"{_SESSION_STEM}.txt", "a",
                         encoding="utf-8")
    except OSError as e:
        print(f"[{ts()}] WARNING: session log unavailable ({e}) — "
              f"terminal only.")
        _LOG_FILE = None
    sys.stdout = _SessionTee(sys.stdout, _LOG_FILE)
    sys.stderr = _SessionTee(sys.stderr, _LOG_FILE)
    return _SESSION_STEM


def close_logging():
    """Restore the real streams and close the log file. Never raises."""
    global _LOG_FILE
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if isinstance(stream, _SessionTee):
            setattr(sys, name, stream._stream)
    if _LOG_FILE is not None:
        try:
            _LOG_FILE.close()
        except Exception:
            pass
        _LOG_FILE = None


def stem() -> str:
    return _SESSION_STEM


def state_path() -> Path:
    return LOG_STATE_DIR / f"{_SESSION_STEM}.md"


def next_cycle() -> int:
    """Cycle counter, so a session's outputs sort in the order they happened."""
    global _CYCLE
    _CYCLE += 1
    return _CYCLE


def _rel(path: Path) -> str:
    """Repo-relative path for a log line, so the printed path is the real one."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def save_response(text: str, cycle: int) -> Path | None:
    """Write one reply body to output/raw/<stem>-<cycle>.txt."""
    try:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        path = RAW_DIR / f"{_SESSION_STEM}-{cycle}.txt"
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        log(f"WARNING: could not save response ({e})", tag="state")
        return None
    log(f"Saved response file={_rel(path)}")
    return path


def save_screenshot(image_bytes: bytes, media_type: str, cycle: int):
    """Keep a copy of the exact image that was uploaded.

    The upload, not the capture: this is the preprocessed WebP the model
    actually saw, which is the one worth having when an answer looks wrong.
    Never raises — a failed copy must not stop the API call."""
    try:
        LOG_SHOT_DIR.mkdir(parents=True, exist_ok=True)
        ext = media_type.rsplit("/", 1)[-1] or "webp"
        (LOG_SHOT_DIR / f"{_SESSION_STEM}-{cycle}.{ext}").write_bytes(image_bytes)
    except Exception:
        return
