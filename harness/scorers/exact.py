"""Exact string match: 1.0 or 0.0, nothing in between."""

from __future__ import annotations


class ExactMatchScorer:
    name = "exact_match"

    def __init__(self, case_sensitive: bool = True):
        self.case_sensitive = case_sensitive

    def score(self, expected: str, actual: str) -> float:
        a, b = expected.strip(), actual.strip()
        if not self.case_sensitive:
            a, b = a.lower(), b.lower()
        return 1.0 if a == b else 0.0
