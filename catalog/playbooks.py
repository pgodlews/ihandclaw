"""Validated, inert navigation advice; loading a playbook never sends actions."""
from pathlib import Path
import re

from .loader import ValidationError, _load_yaml_file

FIELDS = {
    "schema_version", "id", "app_name", "task", "ios_version", "locale",
    "entry_steps", "landmarks", "gotchas", "locale_notes", "verification",
    "evidence", "safety",
}


def validate_playbook(data):
    if not isinstance(data, dict) or set(data) != FIELDS:
        raise ValidationError("Playbook must have exactly the documented fields")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValidationError("Unsupported playbook schema_version")
    if not isinstance(data["id"], str) or not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", data["id"]):
        raise ValidationError("Playbook id must be lowercase snake_case")
    for field in ("app_name", "task", "ios_version", "locale", "safety"):
        if not isinstance(data[field], str) or not data[field].strip():
            raise ValidationError(f"{field} must be nonempty text")
    for field in ("entry_steps", "landmarks", "gotchas", "locale_notes", "verification"):
        if not isinstance(data[field], list) or not data[field] or any(
            not isinstance(item, str) or not item.strip() for item in data[field]
        ):
            raise ValidationError(f"{field} must be a nonempty list of text")
    evidence = data["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise ValidationError("evidence must be nonempty")
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"status", "source", "scope"}:
            raise ValidationError("Evidence needs status, source and scope")
        if item["status"] not in ("measured", "read", "estimated"):
            raise ValidationError("Evidence status must be measured/read/estimated")
        if any(not isinstance(item[key], str) or not item[key].strip() for key in ("source", "scope")):
            raise ValidationError("Evidence source and scope must be nonempty text")
    return data


def load_playbooks(directory=None):
    directory = Path(directory) if directory is not None else Path(__file__).with_name("playbooks")
    books = {}
    for path in sorted(directory.glob("*.yaml")):
        book = validate_playbook(_load_yaml_file(path))
        if path.stem != book["id"] or book["id"] in books:
            raise ValidationError("Playbook filename must match its unique id")
        books[book["id"]] = book
    return books
