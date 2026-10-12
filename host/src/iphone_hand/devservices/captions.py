"""Estimated English caption interpretation; unknown tokens are preserved.

Captions are unstructured, comma-delimited, and may contain private data. Never
log them. The first token is the leading label, not a lossless AX label decoder.
"""

from dataclasses import dataclass, field
import re


_ROLES = {name.casefold(): name for name in (
    "Button", "Toggle", "Header", "Search Field", "Tab", "Link", "Image",
    "Text Field", "Adjustable", "Static Text", "Text", "Switch", "Slider",
    "Secure Text Field", "Keyboard Key", "Menu Item", "Tab Bar",
)}
_STATES = {name.casefold(): name for name in (
    "Selected", "Not Selected", "On", "Off", "Enabled", "Disabled", "Dimmed",
    "Checked", "Unchecked", "Expanded", "Collapsed", "Focused", "Not Enabled",
)}
_HINT = re.compile(
    r"^(?:double[- ]tap\b|triple[- ]tap\b|swipe\b|drag\b|tap\b|"
    r"touch and hold\b|use\b|updates frequently$|starts media session$)", re.I
)


@dataclass(frozen=True)
class Element:
    label: str = field(repr=False)
    roles: tuple[str, ...] = ()
    value: str | None = field(default=None, repr=False)
    states: tuple[str, ...] = ()
    hints: tuple[str, ...] = field(default=(), repr=False)
    uid: str | None = field(default=None, repr=False)
    platform_identifier: str | None = field(default=None, repr=False)
    caption: str = field(default="", repr=False)
    spoken_description: str | None = field(default=None, repr=False)


def parse_caption(
    caption: str | None, *, uid: str | None = None,
    platform_identifier: str | None = None, spoken_description: str | None = None,
) -> Element:
    """Split a leading label and classify remaining tokens, without dropping any.

    Numeric toggle values remain strings ("0" is not asserted to mean Off).
    Unknown suffix tokens form the value; localized roles are not guessed.
    """
    tokens = [token.strip() for token in (caption or "").split(",")]
    roles, states, hints, values = [], [], [], []
    for token in tokens[1:]:
        if not token:
            continue
        key = token.casefold()
        if key in _ROLES:
            roles.append(_ROLES[key])
        elif key in _STATES:
            states.append(_STATES[key])
        elif _HINT.match(token):
            hints.append(token)
        else:
            values.append(token)
    return Element(
        label=tokens[0], roles=tuple(dict.fromkeys(roles)),
        value=", ".join(values) or None, states=tuple(dict.fromkeys(states)),
        hints=tuple(hints), uid=uid, platform_identifier=platform_identifier,
        caption=caption or "", spoken_description=spoken_description,
    )
