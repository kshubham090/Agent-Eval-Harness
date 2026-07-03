"""Scorer protocol: every scorer maps (expected, actual) to a 0.0-1.0 score."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Scorer(Protocol):
    name: str

    def score(self, expected: str, actual: str) -> float: ...
