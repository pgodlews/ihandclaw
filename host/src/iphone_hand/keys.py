"""US hardware layout to HID keyboard usages (SPEC: mapping lives on host)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Key:
    mods: int
    usage: int


MODIFIERS = {"ctrl": 0x01, "control": 0x01, "shift": 0x02, "alt": 0x04,
             "option": 0x04, "cmd": 0x08, "command": 0x08, "gui": 0x08,
             "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40, "rcmd": 0x80}
NAMED = {"enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29,
         "backspace": 0x2A, "tab": 0x2B, "space": 0x2C, "capslock": 0x39,
         "insert": 0x49, "home": 0x4A, "pageup": 0x4B, "delete": 0x4C,
         "end": 0x4D, "pagedown": 0x4E, "right": 0x4F, "left": 0x50,
         "down": 0x51, "up": 0x52, **{f"f{i}": 0x39 + i for i in range(1, 13)}}
_PLAIN = "-=[]\\;'`,./"
_SHIFT = '_+{}|:"~<>?'
_USAGES = [0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38]
CHARACTERS = {c: Key(0, u) for c, u in zip(_PLAIN, _USAGES)}
CHARACTERS.update({c: Key(2, u) for c, u in zip(_SHIFT, _USAGES)})
CHARACTERS.update({c: Key(0, 4 + i) for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")})
CHARACTERS.update({c: Key(2, 4 + i) for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")})
CHARACTERS.update({c: Key(0, 0x1E + i) for i, c in enumerate("1234567890")})
CHARACTERS.update({c: Key(2, 0x1E + i) for i, c in enumerate("!@#$%^&*()")})
CHARACTERS.update({" ": Key(0, 0x2C), "\n": Key(0, 0x28), "\t": Key(0, 0x2B)})


def parse_chord(combo: str) -> tuple[int, list[int]]:
    """Modifiers plus up to six held keys, e.g. tab+f (Full Keyboard Access Find).

    Keys are pressed in order and held together; send them as KREPORT states.
    """
    tokens = combo.strip().lower().split("+")
    if not tokens or any(not t for t in tokens):
        raise ValueError("Use a chord such as tab+f; spell the plus key as 'plus'")
    mods, usages = 0, []
    for token in tokens:
        if token in MODIFIERS:
            mods |= MODIFIERS[token]
            continue
        name = "+" if token == "plus" else token
        if name in NAMED:
            key = Key(0, NAMED[name])
        elif name in CHARACTERS and len(name) == 1:
            key = CHARACTERS[name]
        else:
            raise ValueError(f"Unknown key {token!r}")
        mods |= key.mods
        usages.append(key.usage)
    if not usages or len(usages) > 6 or len(set(usages)) != len(usages):
        raise ValueError("A chord needs 1-6 distinct non-modifier keys")
    return mods, usages


def text_keys(text: str) -> list[Key]:
    # Validate the whole string before sending any part of it.
    result = []
    for i, char in enumerate(text):
        if char not in CHARACTERS:
            raise ValueError(f"Unsupported US-layout character {char!r} (U+{ord(char):04X}) at index {i}")
        result.append(CHARACTERS[char])
    return result


def parse_combo(combo: str) -> Key:
    tokens = combo.strip().lower().split("+")
    if not tokens or any(not t for t in tokens):
        raise ValueError("Use a combo such as cmd+space; spell the plus key as 'plus'")
    mods = 0
    for token in tokens[:-1]:
        if token not in MODIFIERS:
            raise ValueError(f"Unknown modifier {token!r}")
        mods |= MODIFIERS[token]
    name = tokens[-1]
    if name in NAMED:
        return Key(mods, NAMED[name])
    if name == "plus":
        name = "+"
    if name in CHARACTERS and len(name) == 1:
        key = CHARACTERS[name]
        return Key(mods | key.mods, key.usage)
    raise ValueError(f"Unknown key {name!r}")
