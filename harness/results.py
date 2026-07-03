"""Result dataclasses + aggregation.

The flow: run_eval produces one CaseResult per eval case, then aggregate()
rolls them up into an EvalResult (per-scorer means, pass rate) matching the
result shape in the README.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class CaseResult:
    """Raw outcome of running one case: agent output + per-scorer scores."""

    case_id: str
    output: str
    scores: dict[str, float]  # scorer name -> 0.0-1.0
    trajectory: list[str] | None = None
    trajectory_score: float | None = None


@dataclass
class ScorerSummary:
    mean: float
    per_case: dict[str, float]  # case id -> score


@dataclass
class EvalResult:
    run_id: str
    timestamp: str
    scores: dict[str, ScorerSummary]  # scorer name -> summary
    pass_rate: float
    trajectory_score: ScorerSummary | None = None

    def to_dict(self) -> dict:
        d = {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "scores": {
                name: {"mean": s.mean, "per_case": s.per_case}
                for name, s in self.scores.items()
            },
            "pass_rate": self.pass_rate,
        }
        if self.trajectory_score is not None:
            d["trajectory_score"] = {
                "mean": self.trajectory_score.mean,
                "per_case": self.trajectory_score.per_case,
            }
        return d

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def aggregate(case_results: list[CaseResult], pass_threshold: float = 0.5) -> EvalResult:
    """Roll per-case scores up into an EvalResult.

    A case "passes" when the mean of its scorer scores (including trajectory
    score, when present) is >= pass_threshold.
    """
    if not case_results:
        raise ValueError("no case results to aggregate")

    scorer_names = list(case_results[0].scores)
    scores = {
        name: ScorerSummary(
            mean=_mean([c.scores[name] for c in case_results]),
            per_case={c.case_id: c.scores[name] for c in case_results},
        )
        for name in scorer_names
    }

    trajectory_score = None
    scored_traj = [c for c in case_results if c.trajectory_score is not None]
    if scored_traj:
        trajectory_score = ScorerSummary(
            mean=_mean([c.trajectory_score for c in scored_traj]),
            per_case={c.case_id: c.trajectory_score for c in scored_traj},
        )

    def case_passes(c: CaseResult) -> bool:
        values = list(c.scores.values())
        if c.trajectory_score is not None:
            values.append(c.trajectory_score)
        return _mean(values) >= pass_threshold

    return EvalResult(
        run_id=uuid.uuid4().hex[:8],
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        scores=scores,
        pass_rate=_mean([1.0 if case_passes(c) else 0.0 for c in case_results]),
        trajectory_score=trajectory_score,
    )
