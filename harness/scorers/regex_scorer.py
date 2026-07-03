"""Regex scorer: treats expected_output as a regex pattern to test against
the agent's actual output.

This lets a golden case assert on *shape* rather than exact text, e.g.
expected_output = "(?i)paris" accepts "Paris", "paris", "The answer is Paris".
"""

from __future__ import annotations

import re


class RegexScorer:
    name = "regex"

    def __init__(self, full_match: bool = False):
        """full_match=True requires the pattern to consume the whole output;
        the default searches for the pattern anywhere in it."""
        self.full_match = full_match

    def score(self, expected: str, actual: str) -> float:
        try:
            pattern = re.compile(expected)
        except re.error as e:
            raise ValueError(f"invalid regex in expected_output {expected!r}: {e}") from e

        actual = actual.strip()
        matched = pattern.fullmatch(actual) if self.full_match else pattern.search(actual)
        return 1.0 if matched else 0.0
