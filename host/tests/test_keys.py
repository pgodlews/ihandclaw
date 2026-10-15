import string

import pytest

from iphone_hand.keys import Key, parse_chord, parse_combo, text_keys


@pytest.mark.parametrize("text,expected", [
    ("aAzZ", [(0, 4), (2, 4), (0, 29), (2, 29)]),
    ("1!0)", [(0, 30), (2, 30), (0, 39), (2, 39)]),
    (" \n\t", [(0, 44), (0, 40), (0, 43)]),
    ("-=_[+]", [(0, 45), (0, 46), (2, 45), (0, 47), (2, 46), (0, 48)]),
    ("\\|'\"`~", [(0, 49), (2, 49), (0, 52), (2, 52), (0, 53), (2, 53)]),
    (",<.>/?;:", [(0, 54), (2, 54), (0, 55), (2, 55), (0, 56), (2, 56), (0, 51), (2, 51)]),
])
def test_us_text(text, expected):
    assert text_keys(text) == [Key(*p) for p in expected]


def test_all_printable_ascii_and_unicode_error():
    assert len(text_keys(string.ascii_letters + string.digits + string.punctuation + " ")) == 95
    with pytest.raises(ValueError, match=r"U\+00E9.*index 1"):
        text_keys("aé")
    assert text_keys("") == []


@pytest.mark.parametrize("combo,expected", [
    ("cmd+space", (8, 44)), ("cmd+shift+3", (10, 32)), ("ctrl+alt+delete", (5, 76)),
    ("tab", (0, 43)), ("enter", (0, 40)), ("esc", (0, 41)), ("cmd+h", (8, 11)),
    ("up", (0, 82)), ("down", (0, 81)), ("left", (0, 80)), ("right", (0, 79)),
    ("command+option+shift+p", (14, 19)), ("shift+plus", (2, 46)),
    ("f12", (0, 69)), ("rctrl+rshift+rcmd+a", (176, 4)),
])
def test_combos(combo, expected):
    assert parse_combo(combo) == Key(*expected)


@pytest.mark.parametrize("combo", ["", "cmd++", "hyper+a", "cmd+banana", "tab+enter"])
def test_bad_combos(combo):
    with pytest.raises(ValueError):
        parse_combo(combo)


@pytest.mark.parametrize("combo,expected", [
    ("tab+f", (0, [43, 9])), ("tab+b", (0, [43, 5])), ("shift+tab+h", (2, [43, 11])),
    ("tab+plus", (2, [43, 46])), ("a", (0, [4])),
])
def test_chords(combo, expected):
    assert parse_chord(combo) == expected


@pytest.mark.parametrize("combo", ["", "cmd", "tab+tab", "tab+banana", "a+b+c+d+e+f+g", "tab++"])
def test_bad_chords(combo):
    with pytest.raises(ValueError):
        parse_chord(combo)
