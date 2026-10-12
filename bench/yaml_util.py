"""YAML utility with optional PyYAML acceleration and a built-in fallback parser.

Ensures bench scenarios can be loaded without external library requirements,
while taking advantage of PyYAML if installed in the environment.
"""

from __future__ import annotations
import json
import re
from typing import Any


def _parse_scalar(val: str) -> Any:
    val = val.strip()
    if not val:
        return ""
    if val in ("null", "Null", "NULL", "~"):
        return None
    if val in ("true", "True", "TRUE"):
        return True
    if val in ("false", "False", "FALSE"):
        return False
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        # Strip quotes and handle basic escapes
        inner = val[1:-1]
        if val.startswith('"'):
            inner = inner.encode("utf-8").decode("unicode_escape", errors="replace")
        return inner
    # Try integer
    try:
        return int(val)
    except ValueError:
        pass
    # Try float
    try:
        return float(val)
    except ValueError:
        pass
    return val


def parse_simple_yaml(text: str) -> Any:
    """Parse a subset of YAML (dictionaries, lists, scalars, comments) in pure Python."""
    lines = text.splitlines()

    # Pre-process: strip comments and track indent
    clean_lines: list[tuple[int, str]] = []
    for line in lines:
        # Check comment outside quotes
        in_quote: str | None = None
        comment_idx = -1
        for i, ch in enumerate(line):
            if ch in ('"', "'"):
                if in_quote == ch:
                    in_quote = None
                elif in_quote is None:
                    in_quote = ch
            elif ch == "#" and in_quote is None:
                comment_idx = i
                break
        if comment_idx != -1:
            line = line[:comment_idx]
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        clean_lines.append((indent, stripped.lstrip(" ")))

    if not clean_lines:
        return {}

    def _parse_block(idx: int, current_indent: int) -> tuple[Any, int]:
        if idx >= len(clean_lines):
            return {}, idx

        first_indent, first_content = clean_lines[idx]
        is_list = first_content.startswith("- ") or first_content == "-"

        if is_list:
            result_list = []
            while idx < len(clean_lines):
                indent, content = clean_lines[idx]
                if indent < current_indent:
                    break
                if indent != current_indent and not (content.startswith("- ") or content == "-"):
                    break
                if not (content.startswith("- ") or content == "-"):
                    break

                item_str = content[2:].strip() if content.startswith("- ") else ""
                idx += 1
                if not item_str:
                    # Item is a nested block
                    if idx < len(clean_lines) and clean_lines[idx][0] > indent:
                        child_val, idx = _parse_block(idx, clean_lines[idx][0])
                        result_list.append(child_val)
                    else:
                        result_list.append(None)
                elif ":" in item_str and not (item_str.startswith('"') or item_str.startswith("'")):
                    # Inline key: value at beginning of list item
                    k, _, v = item_str.partition(":")
                    k = k.strip()
                    v = v.strip()
                    sub_dict: dict[str, Any] = {}
                    if v:
                        sub_dict[k] = _parse_scalar(v)
                    else:
                        if idx < len(clean_lines) and clean_lines[idx][0] > indent:
                            val, idx = _parse_block(idx, clean_lines[idx][0])
                            sub_dict[k] = val
                        else:
                            sub_dict[k] = None
                    # Also parse subsequent keys at sub_dict level
                    sub_indent = indent + 2
                    while idx < len(clean_lines) and clean_lines[idx][0] >= sub_indent:
                        if clean_lines[idx][0] == sub_indent and ":" in clean_lines[idx][1]:
                            sub_line = clean_lines[idx][1]
                            sk, _, sv = sub_line.partition(":")
                            sk = sk.strip()
                            sv = sv.strip()
                            idx += 1
                            if sv:
                                sub_dict[sk] = _parse_scalar(sv)
                            else:
                                if idx < len(clean_lines) and clean_lines[idx][0] > sub_indent:
                                    val, idx = _parse_block(idx, clean_lines[idx][0])
                                    sub_dict[sk] = val
                                else:
                                    sub_dict[sk] = None
                        elif clean_lines[idx][0] > sub_indent:
                            # Child of previous key already handled
                            idx += 1
                        else:
                            break
                    result_list.append(sub_dict)
                else:
                    result_list.append(_parse_scalar(item_str))
            return result_list, idx

        else:
            result_dict: dict[str, Any] = {}
            while idx < len(clean_lines):
                indent, content = clean_lines[idx]
                if indent < current_indent:
                    break
                if indent != current_indent:
                    break

                if ":" not in content:
                    idx += 1
                    continue

                # Find colon outside quotes
                in_quote = None
                colon_idx = -1
                for i, ch in enumerate(content):
                    if ch in ('"', "'"):
                        if in_quote == ch:
                            in_quote = None
                        elif in_quote is None:
                            in_quote = ch
                    elif ch == ":" and in_quote is None:
                        colon_idx = i
                        break

                if colon_idx == -1:
                    idx += 1
                    continue

                key = content[:colon_idx].strip()
                val_str = content[colon_idx + 1:].strip()
                idx += 1

                if val_str:
                    # Check for inline list or scalar
                    if val_str.startswith("[") and val_str.endswith("]"):
                        # Parse simple inline JSON-like list
                        try:
                            result_dict[key] = json.loads(val_str)
                        except Exception:
                            result_dict[key] = [
                                _parse_scalar(x.strip()) for x in val_str[1:-1].split(",") if x.strip()
                            ]
                    else:
                        result_dict[key] = _parse_scalar(val_str)
                else:
                    # Check if there is an indented block following
                    if idx < len(clean_lines) and clean_lines[idx][0] > indent:
                        child_val, idx = _parse_block(idx, clean_lines[idx][0])
                        result_dict[key] = child_val
                    else:
                        result_dict[key] = None
            return result_dict, idx

    parsed, _ = _parse_block(0, clean_lines[0][0])
    return parsed


def load_yaml(text: str) -> Any:
    """Load YAML string using PyYAML if present, or fallback parser."""
    try:
        import yaml
        return yaml.safe_load(text)
    except ImportError:
        return parse_simple_yaml(text)


def dump_yaml(data: Any, indent_level: int = 0) -> str:
    """Dump basic Python objects to a clean YAML string."""
    try:
        import yaml
        return yaml.safe_dump(data, sort_keys=False)
    except ImportError:
        pass

    # Pure Python dumper fallback
    lines = []
    prefix = "  " * indent_level
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                lines.append(f"{prefix}{k}:")
                lines.append(dump_yaml(v, indent_level + 1))
            elif isinstance(v, list):
                lines.append(f"{prefix}{k}:")
                lines.append(dump_yaml(v, indent_level + 1))
            elif isinstance(v, str):
                if "\n" in v or ":" in v or "#" in v or "'" in v:
                    lines.append(f'{prefix}{k}: "{v}"')
                else:
                    lines.append(f"{prefix}{k}: {v}")
            elif v is None:
                lines.append(f"{prefix}{k}: null")
            elif isinstance(v, bool):
                lines.append(f"{prefix}{k}: {'true' if v else 'false'}")
            else:
                lines.append(f"{prefix}{k}: {v}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                first = True
                for k, v in item.items():
                    if first:
                        if isinstance(v, (dict, list)):
                            lines.append(f"{prefix}- {k}:")
                            lines.append(dump_yaml(v, indent_level + 2))
                        else:
                            val_str = f'"{v}"' if isinstance(v, str) and (":" in v or " " in v) else str(v)
                            lines.append(f"{prefix}- {k}: {val_str}")
                        first = False
                    else:
                        if isinstance(v, (dict, list)):
                            lines.append(f"{prefix}  {k}:")
                            lines.append(dump_yaml(v, indent_level + 2))
                        else:
                            val_str = f'"{v}"' if isinstance(v, str) and (":" in v or " " in v) else str(v)
                            lines.append(f"{prefix}  {k}: {val_str}")
            elif isinstance(item, list):
                lines.append(f"{prefix}-")
                lines.append(dump_yaml(item, indent_level + 1))
            else:
                lines.append(f"{prefix}- {item}")
    return "\n".join(lines)
