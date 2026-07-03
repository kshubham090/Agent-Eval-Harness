"""Trajectory comparison: how closely did the agent's tool-call sequence
follow the expected one?

Uses longest common subsequence (LCS), so order matters but the agent gets
partial credit for mostly-right paths. The score divides by the *longer* of
the two sequences, penalizing both missing steps and extra ones.
"""

from __future__ import annotations

from collections.abc import Sequence


def lcs_length(a: Sequence[str], b: Sequence[str]) -> int:
    # Classic DP over a (len(a)+1) x (len(b)+1) table, rolled to two rows.
    prev = [0] * (len(b) + 1)
    for x in a:
        curr = [0]
        for j, y in enumerate(b, start=1):
            curr.append(prev[j - 1] + 1 if x == y else max(prev[j], curr[j - 1]))
        prev = curr
    return prev[-1]


def score_trajectory(expected: Sequence[str], actual: Sequence[str]) -> float:
    """LCS(expected, actual) / max(len) -> 0.0-1.0."""
    if not expected and not actual:
        return 1.0
    if not expected or not actual:
        return 0.0
    return lcs_length(expected, actual) / max(len(expected), len(actual))
