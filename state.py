"""The cumulative state file.

`log/state/<session>.md` holds what has been learned about whatever is on
screen — one canonical snapshot per section, rewritten in place, plus an
append-only History. It is re-sent to the model with every capture, which is
what lets a later screenshot build on an earlier one instead of replacing it.

Layout:

    # <title> (<ts>)
    ## <Section A>

    <body>

    ## <Section B>

    <body>

    ## History

    ### <ts> — <status> — <notes>

Three properties this file exists to guarantee:

- **Nothing deletes knowledge.** A section the model omits keeps its prior
  body; an empty section over a non-empty prior is refused outright. Content
  that scrolled off screen survives a capture that does not show it.
- **History is last and append-only**, so it never displaces a section.
- **The write is atomic** (tmp + os.replace), because this file goes out on
  the wire next cycle. A half-written one would be sent verbatim.

Section names come from the Prompt, not from here — see `prompts.Prompt`.
"""

import os
import re
from pathlib import Path

from session import log

# Most recent History entries re-sent to the model. History is the only part
# that grows without bound; capping it on the READ side keeps the file on disk
# complete while shrinking the wire payload.
HISTORY_KEEP = 6

HISTORY_SECTION = "History"

# The model may return this instead of a body to mean "keep what you have".
# Cheaper than re-emitting an unchanged section every cycle.
UNCHANGED = "unchanged"


def _state_shrinks(old: str, new: str) -> bool:
    """True when replacing `old` with `new` would lose lines.

    Accepted anyway when every non-blank prior line still appears in the new
    text (an in-place correction) or the new text is at least as long — only a
    genuine shrink that also drops lines is refused."""
    old_lines = [l.strip() for l in old.splitlines() if l.strip()]
    new_lines = [l.strip() for l in new.splitlines() if l.strip()]
    if len(new_lines) >= len(old_lines):
        return False
    kept = set(new_lines)
    return not all(l in kept for l in old_lines)


class State:
    """One session's cumulative state file, scoped to a prompt's sections."""

    def __init__(self, path: Path, sections: tuple[str, ...], *,
                 no_shrink: tuple[str, ...] = (), title: str = "Session",
                 history_keep: int = HISTORY_KEEP):
        self.path = path
        self.sections = tuple(sections)
        self.no_shrink = frozenset(no_shrink)
        self.title = title
        self.history_keep = history_keep
        names = self.sections + (HISTORY_SECTION,)
        self._head_re = re.compile(
            r"^## (" + "|".join(re.escape(n) for n in names) + r")\s*$", re.M)

    # -- reading ----------------------------------------------------------

    def read(self) -> str:
        """The file's content with History capped, or "" if there is none.

        This is what rides the API message. A capping bug must never block a
        call, so any failure falls back to the raw text."""
        if not self.sections or not self.path.exists():
            return ""
        try:
            return self._cap_history(
                self.path.read_text(encoding="utf-8").strip())
        except OSError:
            return ""
        except Exception:  # noqa: BLE001 - capping must never block a call
            try:
                return self.path.read_text(encoding="utf-8").strip()
            except OSError:
                return ""

    def _cap_history(self, text: str) -> str:
        """Keep every section whole; keep only the last N History entries."""
        head, sep, hist = text.partition(f"\n## {HISTORY_SECTION}\n")
        if not sep:
            return text
        entries = re.split(r"(?m)^(?=### )", hist)
        # entries[0] is whatever sits above the first "### " — always kept.
        lead, rest = entries[0], [e for e in entries[1:] if e.strip()]
        if len(rest) <= self.history_keep:
            return text
        dropped = len(rest) - self.history_keep
        kept = "".join(rest[-self.history_keep:])
        return (f"{head}{sep}{lead}_… {dropped} earlier history "
                f"{'entry' if dropped == 1 else 'entries'} elided …_\n\n{kept}"
                .rstrip())

    # -- parsing / rendering ----------------------------------------------

    def _split(self, text: str):
        """(preamble, {section: body}, history_body).

        A file with no recognized headings comes back as pure preamble, so a
        hand-written or differently-shaped file is appended to rather than
        reinterpreted."""
        heads = list(self._head_re.finditer(text))
        if not heads:
            return text, {}, ""
        preamble = text[:heads[0].start()]
        sections, history = {}, ""
        for i, m in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            body = text[m.end():end]
            if m.group(1) == HISTORY_SECTION:
                history = body
            else:
                sections[m.group(1)] = body
        return preamble, sections, history

    def _render(self, preamble: str, sections: dict, history: str) -> str:
        """Inverse of _split. Empty sections are omitted, so a file with
        nothing in them renders back to its preamble unchanged."""
        out = preamble.rstrip("\n")
        for name in self.sections:
            body = (sections.get(name) or "").strip()
            if body:
                out += f"\n\n## {name}\n\n{body}"
        if history.strip():
            out += f"\n\n## {HISTORY_SECTION}\n\n{history.strip()}"
        return out + "\n"

    # -- merging ----------------------------------------------------------

    def _normalize(self, returned) -> dict:
        """Model output -> {section: text|None}. None means "keep prior"."""
        if not isinstance(returned, dict):
            return {}
        out, unknown = {}, []
        for key, value in returned.items():
            if key not in self.sections:
                unknown.append(str(key))
                continue
            if value is None or not isinstance(value, str):
                out[key] = None
            elif value.strip() == UNCHANGED:
                out[key] = None
            else:
                out[key] = value
        if unknown:
            log(f"WARNING: dropped unknown state section(s) "
                f"{', '.join(sorted(unknown))}", tag="state")
        return out

    def _merge(self, prior: dict, returned: dict):
        """Apply the merge-safety rules. Returns (merged, notes).

        Nothing here may DELETE knowledge: an omitted section keeps the prior
        one, and an empty one over a non-empty prior is refused outright."""
        merged, notes = dict(prior), []
        for name, value in returned.items():
            if value is None:                       # "unchanged" / absent
                continue
            new = value.strip()
            old = (prior.get(name) or "").strip()
            if not new:
                if old:
                    notes.append(f"declined empty {name} (prior kept)")
                continue
            if name in self.no_shrink and old and _state_shrinks(old, new):
                notes.append(f"declined shrinking {name} (prior kept)")
                continue
            if new != old:
                merged[name] = new
                notes.append(f"updated {name}")
        return merged, notes

    # -- writing ----------------------------------------------------------

    def _write_atomic(self, text: str) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.parent / f".{self.path.name}.tmp"
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError as e:
            log(f"WARNING: could not write state ({e})", tag="state")
            return False

    def update(self, returned, *, timestamp: str, status: str = "",
               note: str = "") -> list[str]:
        """Merge one reply's sections into the file and record a History entry.

        Returns the merge notes (also logged). Never raises: nothing about the
        capture loop depends on this file being writable."""
        if not self.sections:
            return []
        try:
            incoming = self._normalize(returned)
            text = self.read()

            # Create gate: don't mint a file that would hold only History.
            if not text and not any(
                    (v or "").strip() for v in incoming.values()):
                return []

            preamble, prior, history = self._split(text)
            if not preamble.strip():
                preamble = f"# {self.title} ({timestamp})\n"

            merged, notes = self._merge(prior, incoming)
            entry = (f"### {timestamp} — {status or 'update'} — "
                     + (note or "; ".join(notes) or "no section changed"))
            history = (history.rstrip("\n") + "\n\n" + entry
                       if history.strip() else entry)

            if self._write_atomic(self._render(preamble, merged, history)):
                log("State " + ("updated: " + "; ".join(notes) if notes
                                else "unchanged"), tag="state")
            return notes
        except Exception as e:  # noqa: BLE001 - never fatal
            log(f"WARNING: state update skipped ({e})", tag="state")
            return []
