"""Prompt definitions — the one file you edit to add work.

Each entry pairs a system prompt with the model that should run it, so
choosing a model is a property of the prompt rather than a global setting.
`run.py --prompt <name>` selects one.

This module also loads `.env` (override=False, so a shell-set variable still
wins for a one-off run). It is imported by everything that needs config, which
is why the load lives here rather than in a separate module.
"""

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=False)

# Fallback when a Prompt does not name a model. Set MODEL in .env to change it
# globally; naming a model on the Prompt itself is the primary mechanism.
DEFAULT_MODEL = os.getenv("MODEL") or "claude-opus-5"

# low | medium | high | xhigh | max. Controls thinking depth and total token
# spend. `high` is the API default; `medium` is a reasonable balance for
# screenshot work that is mostly reading rather than reasoning.
DEFAULT_EFFORT = "medium"


@dataclass(frozen=True)
class Prompt:
    """One named unit of work: a system prompt plus how to run it."""

    system: str

    # Which model answers this prompt. Exact IDs only — claude-opus-5,
    # claude-sonnet-5, claude-haiku-4-5, claude-fable-5. Never append a date
    # suffix; the aliases are complete as written.
    model: str = DEFAULT_MODEL

    # Set to "" to omit the parameter entirely — required for models that
    # reject it, Haiku 4.5 among them.
    effort: str = DEFAULT_EFFORT

    # Hard ceiling on thinking + response text combined. Requests always
    # stream, so a large value here costs nothing until it is used.
    max_tokens: int = 16000

    # Text appended to the user message after the image and any state file.
    user_suffix: str = "Analyze this screenshot."

    # True  -> the reply is parsed as JSON (brace-balanced extraction, so
    #          prose around the object is tolerated) and, if `state_sections`
    #          is set, merged into this session's cumulative state file.
    # False -> the reply is saved as raw text and nothing else happens.
    json: bool = False

    # Cumulative state-file sections, in the order they are rendered. The
    # model returns {"<section name>": "<text>"} under `state_key`; a section
    # it omits keeps whatever the file already holds. Empty tuple = no state
    # file for this prompt.
    state_sections: tuple[str, ...] = ()

    # Key in the JSON reply holding the section dict.
    state_key: str = "state"

    # Sections where a reply that DROPS lines is refused rather than applied.
    # Use for anything transcribed off-screen (source code, long lists) where
    # a screenshot showing less than last time must not erase what was seen.
    # Must be a subset of state_sections.
    no_shrink_sections: tuple[str, ...] = ()

    def resolved(self, model: str = "", effort: str = "") -> "Prompt":
        """A copy with CLI overrides applied. Empty values keep the entry's."""
        return replace(self,
                       model=model or self.model,
                       effort=effort or self.effort)


# ---------------------------------------------------------------------------
# Prompts. Add yours here.
# ---------------------------------------------------------------------------

SAMPLE_SYSTEM = "sampel prompt..."


STRUCTURED_SYSTEM = """\
You are watching a screen and maintaining notes about what is on it.

Return ONLY a JSON object, no prose around it:

{
  "status": "<no_change|new|updated>",
  "state": {
    "Current Spec": "<what is on screen right now, in full>",
    "Notes": "<anything worth remembering about it>"
  },
  "result": "<your answer for this capture>"
}

Rules for "state":
- It is CUMULATIVE knowledge, not a description of this one screenshot.
  You are given the current file; merge into it rather than replacing it.
- Omit a section, or set it to the literal string "unchanged", to keep what
  the file already holds. Never blank a section to say "not visible now".
- "status" is "no_change" when the screen shows nothing new.
"""


PROMPTS: dict[str, Prompt] = {
    # Plain-text prompt: the reply is saved verbatim, nothing is parsed.
    "sample": Prompt(
        system=SAMPLE_SYSTEM,
        model="claude-opus-5",
    ),

    # JSON prompt: the reply is parsed and merged into log/state/<session>.md.
    "structured": Prompt(
        system=STRUCTURED_SYSTEM,
        model="claude-sonnet-5",
        json=True,
        state_sections=("Current Spec", "Notes"),
    ),
}


def get(name: str) -> Prompt:
    """Look up a prompt, or fail with the list of valid names."""
    try:
        return PROMPTS[name]
    except KeyError:
        valid = ", ".join(sorted(PROMPTS)) or "(none defined)"
        raise SystemExit(
            f"[ERROR] Unknown prompt {name!r}. Available: {valid}") from None


def names() -> list[str]:
    return sorted(PROMPTS)
