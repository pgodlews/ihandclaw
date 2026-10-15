"""Final-screen gold checkpoints and strict line protocol (no model self-grading)."""
import json
import re


def parse_line(text, prefix):
    # Ambiguous, malformed or duplicate declarations fail closed.
    lines = [line[len(prefix) + 1:] for line in text.splitlines()
             if line.startswith(prefix + " ")]
    if len(lines) != 1:
        return None
    try:
        value = json.loads(lines[0])
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def parse_usage(text):
    usage = parse_line(text, "BENCH_USAGE")
    if not usage or any(type(usage.get(k)) is not int or usage[k] < 0
                        for k in ("input_tokens", "output_tokens")):
        return None
    return {k: usage[k] for k in ("input_tokens", "output_tokens", "cost_usd")
            if k in usage and (k != "cost_usd" or
               type(usage[k]) in (int, float) and 0 <= usage[k] < float("inf"))}


# OCR confuses these glyph pairs in the Notes title font (measured 2026-10-12: "lzfpe" read as "Izfpe").
_CONFUSABLE = str.maketrans({"I": "l", "i": "l", "1": "l", "|": "l", "O": "o", "0": "o"})


def _fold(text):
    return re.sub(r"\s+", " ", text).translate(_CONFUSABLE).lower()


def _digits_after_result(line):
    match = re.fullmatch(r"result:?\s*([0-9][0-9 ,.]*)", line, re.IGNORECASE)
    return re.sub(r"[ ,.]", "", match[1]) if match else None


def evaluate(elements, run_id, ios_version, stdout, product=69104):
    # Voice Control badge numbers can be merged into OCR text ("2 IOS 18.7.10"; measured 2026-10-12).
    lines = [re.sub(r"^\d{1,2}\s+(?=\D)", "", line.strip())
             for el in elements for line in str(el.get("text", "")).splitlines()]
    result = parse_line(stdout, "BENCH_RESULT")
    claim = result.get("success") if result and type(result.get("success")) is bool else None
    checkpoints = {
        "C1": any(_fold(f"Benchy {run_id}") in _fold(line) for line in lines),
        # iOS auto-capitalises a line start, so "iOS" may appear as "IOS" (measured 2026-10-12).
        "C2": any(re.fullmatch(r"ios\s+" + re.escape(ios_version), line, re.IGNORECASE) for line in lines),
        # No "=" in the note: iOS 18 Math Notes appends its own answer to "a x b =" (measured).
        "C3": any(_digits_after_result(line) == str(product) for line in lines),
        "C4": bool(result and claim is not None and result.get("ios_version") == ios_version
                   and re.sub(r"[ ,.]", "", str(result.get("calc_result"))) == str(product)),
    }
    return checkpoints, claim, result


def run_summary(result):
    d = result.metrics
    if d.get("setup_failed"):
        return f"{result.model} | SETUP_FAILED | model not started | {result.reason}"
    checks = " ".join(k + ("✓" if v else "✗") for k, v in d.get("checkpoints", {}).items())
    honesty = "FALSE SUCCESS" if d.get("false_success") else ("honest" if d.get("honest") else "dishonest" if d.get("claimed_success") is not None else "unclaimed")
    seconds = int(result.wall_time_s)
    usage = d.get("usage")
    tokens = f"{(usage['input_tokens'] + usage['output_tokens']) / 1000:g}k tok" if usage else "tokens unknown"
    actions = d.get("actions", result.steps)
    cost = f" | ${usage['cost_usd']:g}" if usage and "cost_usd" in usage else ""
    verdict = "INVALID" if d.get("invalid") else "PASS" if result.success else "FAIL"
    return (f"{result.model} | {checks} | {verdict} {honesty} | "
            f"{actions if actions is not None else 'unknown'} actions | {seconds // 60}m{seconds % 60:02d}s | {tokens}{cost}")
