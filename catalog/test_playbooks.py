from copy import deepcopy

import pytest

from catalog.loader import ValidationError
from catalog.playbooks import load_playbooks, validate_playbook


def test_seed_playbooks_load():
    books = load_playbooks()
    assert set(books) == {"notes_new_note", "settings_ios_version"}
    assert books["notes_new_note"]["app_name"] == "Notes"


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("id", "Bad ID"),
    ("entry_steps", []), ("landmarks", [1]), ("locale", ""),
    ("evidence", [{"status": "guessed", "source": "brief", "scope": "test"}]),
])
def test_invalid_playbook(field, value):
    book = deepcopy(load_playbooks()["notes_new_note"])
    book[field] = value
    with pytest.raises(ValidationError):
        validate_playbook(book)


def test_missing_and_unknown_fields():
    book = deepcopy(load_playbooks()["notes_new_note"])
    del book["safety"]
    with pytest.raises(ValidationError):
        validate_playbook(book)
    book["safety"] = "Read only"
    book["execute"] = True
    with pytest.raises(ValidationError):
        validate_playbook(book)


def test_filename_must_match_id(tmp_path):
    from pathlib import Path
    source = Path(__file__).with_name("playbooks") / "notes_new_note.yaml"
    (tmp_path / "wrong.yaml").write_text(source.read_text())
    with pytest.raises(ValidationError):
        load_playbooks(tmp_path)
