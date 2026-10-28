"""The payload builder (PLAN_M3 §3.2, §3.5, D95): technique + wording + goal + canary -> text.

Pure functions, no model and no network. The builder only renders text and declares what is in
it; where the text is placed (a tool result, a file, a tool description) is step 5.

Markers (lab/tracing/markers.py):
- the canary is always in the payload as plain text, so the matcher finds it whatever the
  technique;
- a goal marker is in plain text too, except in an obfuscated payload. There it is only in the
  encoded part, which the matcher can't read: base64 hides it, and Unicode tag characters are
  category Cf, which D77's `normalise` drops. So the payload *declares* its decoded markers
  (`decoded_markers`): the markers the environment wrote at hop 0 although a search of the text
  doesn't show them. Without the declaration, the first agent to write the decoded IBAN would
  look like it invented it ("unsourced") instead of passing it on.

The builder checks its own declaration: every marker it names really is in the decoded payload.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from lab.attacks.agentdojo import USER_NAME
from lab.attacks.cases import Goal, Technique
from lab.attacks.techniques import POINTER_FILE, TEMPLATES, Split
from lab.tracing.markers import Marker, normalise

Encoding = Literal["base64", "tags"]
Stage = Literal["pointer", "action"]

# Unicode tag characters U+E0020..U+E007E mirror printable ASCII U+0020..U+007E. All are
# category Cf.
TAG_OFFSET: Final = 0xE0000
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")


class Payload(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    canary: str
    # Markers a search of `text` finds: the canary, and the goal markers if they are not encoded.
    plain_markers: tuple[Marker, ...]
    # Goal markers that are only in the encoded part. Declared at hop 0 (see the module docstring).
    decoded_markers: tuple[Marker, ...]

    @property
    def hop0_markers(self) -> tuple[Marker, ...]:
        """Every marker the environment puts into the run with this payload."""
        return self.plain_markers + self.decoded_markers


def encoding_for(goal: Goal) -> Encoding:
    """PLAN_M3 §3.5, k=5: sabotage uses Unicode tags, the other goals use base64."""
    return "tags" if goal == "sabotage" else "base64"


def encode_base64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def encode_tags(text: str) -> str:
    """Hide printable ASCII in invisible tag characters. Anything else would not survive the
    mapping, so it is an error and not a silent loss."""
    if any(not 0x20 <= ord(ch) <= 0x7E for ch in text):
        raise ValueError("Unicode-tag encoding takes printable ASCII only")
    return "".join(chr(TAG_OFFSET + ord(ch)) for ch in text)


def decode(text: str) -> str:
    """Undo both encodings inside a payload: tag characters become ASCII, and a base64 run that
    decodes to printable text is replaced by that text. A reader's view, for tests and checks."""
    text = "".join(
        chr(ord(ch) - TAG_OFFSET) if 0xE0020 <= ord(ch) <= 0xE007E else ch for ch in text
    )

    def replace(match: re.Match[str]) -> str:
        try:
            decoded = base64.b64decode(match.group(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return match.group()
        return decoded if decoded.isprintable() else match.group()

    return _BASE64_RUN.sub(replace, text)


def build_payload(
    technique: Technique,
    split: Split,
    *,
    goal: Goal,
    goal_text: str,
    goal_markers: Sequence[str],
    canary: str,
    user: str = USER_NAME,
    stage: Stage | None = None,
    encoding: Encoding | None = None,
) -> Payload:
    """Render one case's payload.

    `goal_text` is the injection task's instruction and `goal_markers` its operative literals
    (the attacker IBAN, `new_password`), which must occur in it. `stage` is required for
    multi_hop; a pointer carries no goal, so it declares no goal marker. `encoding` defaults to
    the goal's (`encoding_for`) and applies to obfuscation only.
    """
    if technique == "important_instructions":
        raise ValueError("important_instructions is AgentDojo's own text (lab.attacks.agentdojo)")
    variants = TEMPLATES[technique]
    variant: str
    if technique == "multi_hop":
        if stage is None:
            raise ValueError("multi_hop needs stage='pointer' or stage='action'")
        variant = stage
    elif stage is not None:
        raise ValueError(f"stage applies to multi_hop only, not {technique}")
    elif technique == "obfuscation":
        variant = encoding or encoding_for(goal)
    elif encoding is not None:
        raise ValueError(f"encoding applies to obfuscation only, not {technique}")
    else:
        variant = "default"

    carries_goal = stage != "pointer"
    encoded = ""
    if technique == "obfuscation":
        encoded = encode_tags(goal_text) if variant == "tags" else encode_base64(goal_text)
    text = variants[variant][split].format(
        goal=goal_text, canary=canary, user=user, file=POINTER_FILE, encoded=encoded
    )

    # The declaration must be true: every marker is in the decoded payload.
    seen = normalise(decode(text))
    wanted = list(goal_markers) if carries_goal else []
    missing = [m for m in [canary, *wanted] if normalise(m) not in seen]
    if missing:
        raise ValueError(f"Markers not in the decoded payload: {missing}")
    if canary not in text:
        raise ValueError("The canary must be in the payload as plain text")

    goal_hits = [Marker(value=m, kind="goal") for m in wanted]
    if technique == "obfuscation":
        plain, decoded = [], goal_hits  # the goal text is only in the encoded part
    else:
        plain, decoded = goal_hits, []
    return Payload(
        text=text,
        canary=canary,
        plain_markers=(Marker(value=canary, kind="canary"), *plain),
        decoded_markers=tuple(decoded),
    )
