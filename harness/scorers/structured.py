"""Deterministic assertions for structured responses and text containment."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation


def _reject_constant(value):
    raise ValueError(f"non-standard JSON constant {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _decimal(value):
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("JSON number exceeds supported range") from exc


def strict_json(text):
    # Preserve decimal precision instead of merging distinct numbers after float rounding.
    return json.loads(text, parse_float=_decimal, parse_constant=_reject_constant,
                      object_pairs_hook=_unique_object)


def _equal(a, b):
    # JSON booleans must not compare equal to numbers (Python's True == 1).
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


class JSONMatchScorer:
    """Compare parsed JSON values; ignore object key order and whitespace."""
    name = "json_match"

    def score(self, expected: str, actual: str) -> float:
        try:
            reference = strict_json(expected)
        except ValueError as exc:
            raise ValueError(f"expected_output is not valid JSON: {exc}") from exc
        try:
            response = strict_json(actual)
        except ValueError:
            return 0.0
        return float(_equal(reference, response))


class ContainsScorer:
    """Case-sensitive substring assertion; not a semantic correctness judge."""
    name = "contains"

    def score(self, expected: str, actual: str) -> float:
        if not expected:
            raise ValueError("contains scorer needs a nonempty expected_output")
        return float(expected in actual)
