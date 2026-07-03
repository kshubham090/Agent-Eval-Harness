"""Meta-eval: evaluate the evaluator.

An LLM judge is itself a model with biases -- before trusting its scores, run
it against a calibration set of (expected, actual, human_score) triples where
a human already decided the right grade, and measure how closely the judge
agrees. If agreement is poor, fix the judge prompt (or the judge model)
before believing any eval run that uses it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from harness.dataset import DatasetError
from harness.scorers.base import Scorer


@dataclass(frozen=True)
class JudgeCase:
    """One calibration case: a graded (expected, actual) pair."""

    id: str
    expected: str
    actual: str
    human_score: float  # the ground-truth grade, 0.0-1.0


def load_judge_dataset(path: str | Path) -> list[JudgeCase]:
    path = Path(path)
    cases: list[JudgeCase] = []
    seen_ids: set[str] = set()

    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                raise DatasetError(f"line {line_no}: invalid JSON: {e}") from e

            for name in ("id", "expected", "actual"):
                if not isinstance(record.get(name), str) or not record[name].strip():
                    raise DatasetError(f"line {line_no}: missing or empty required string field {name!r}")
            score = record.get("human_score")
            if not isinstance(score, (int, float)) or not 0.0 <= score <= 1.0:
                raise DatasetError(f"line {line_no}: 'human_score' must be a number in [0.0, 1.0]")
            if record["id"] in seen_ids:
                raise DatasetError(f"line {line_no}: duplicate case id {record['id']!r}")

            seen_ids.add(record["id"])
            cases.append(JudgeCase(
                id=record["id"],
                expected=record["expected"],
                actual=record["actual"],
                human_score=float(score),
            ))

    if not cases:
        raise DatasetError(f"{path}: judge dataset is empty")
    return cases


@dataclass
class JudgeScorePair:
    case_id: str
    human: float
    judge: float

    @property
    def error(self) -> float:
        return abs(self.judge - self.human)


@dataclass
class MetaEvalResult:
    pairs: list[JudgeScorePair]
    mean_absolute_error: float
    agreement_rate: float  # fraction of cases within tolerance of the human score
    tolerance: float


def evaluate_judge(cases: list[JudgeCase], judge: Scorer, tolerance: float = 0.25) -> MetaEvalResult:
    """Run the judge over graded cases and measure agreement with humans.

    agreement_rate counts a case as agreed when |judge - human| <= tolerance;
    mean_absolute_error is the average |judge - human| across all cases.
    """
    if not cases:
        raise ValueError("no judge cases to evaluate")

    pairs = [
        JudgeScorePair(case_id=c.id, human=c.human_score, judge=judge.score(c.expected, c.actual))
        for c in cases
    ]
    return MetaEvalResult(
        pairs=pairs,
        mean_absolute_error=sum(p.error for p in pairs) / len(pairs),
        agreement_rate=sum(1 for p in pairs if p.error <= tolerance) / len(pairs),
        tolerance=tolerance,
    )
