"""Result dataclasses + aggregation.

The flow: run_eval produces one CaseResult per eval case, then aggregate()
rolls them up into an EvalResult (per-scorer means, pass rate, error count).
For statistical robustness, summarize_runs() combines several EvalResults
from repeated runs into a multi-run summary with per-metric mean and stddev.
"""

from __future__ import annotations

import json
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class CaseResult:
    """Raw outcome of running one case: agent output + per-scorer scores."""

    case_id: str
    input: str
    expected_output: str
    output: str
    scores: dict[str, float]  # scorer name -> 0.0-1.0
    trajectory: list[str] | None = None
    trajectory_score: float | None = None
    error: str | None = None  # set when the agent or a scorer raised
    latency_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.case_id,
            "input": self.input,
            "expected_output": self.expected_output,
            "output": self.output,
            "scores": self.scores,
            "trajectory": self.trajectory,
            "trajectory_score": self.trajectory_score,
            "error": self.error,
            "latency_ms": self.latency_ms,
        }


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
    error_count: int = 0
    dataset_sha: str | None = None
    metadata: dict = field(default_factory=dict)
    case_results: list[CaseResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "scores": {
                name: {"mean": s.mean, "per_case": s.per_case}
                for name, s in self.scores.items()
            },
            "pass_rate": self.pass_rate,
            "error_count": self.error_count,
            "cases": [c.to_dict() for c in self.case_results],
        }
        if self.trajectory_score is not None:
            d["trajectory_score"] = {
                "mean": self.trajectory_score.mean,
                "per_case": self.trajectory_score.per_case,
            }
        if self.dataset_sha:
            d["dataset_sha"] = self.dataset_sha
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def aggregate(
    case_results: list[CaseResult],
    pass_threshold: float = 0.5,
    dataset_sha: str | None = None,
    metadata: dict | None = None,
) -> EvalResult:
    """Roll per-case scores up into an EvalResult.

    A case "passes" when the mean of its scorer scores (including trajectory
    score, when present) is >= pass_threshold. Errored cases carry 0.0 scores,
    so they count against every mean and the pass rate.
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
        error_count=sum(1 for c in case_results if c.error is not None),
        dataset_sha=dataset_sha,
        metadata=metadata or {},
        case_results=case_results,
    )


def flatten_metrics(result: dict) -> dict[str, float]:
    """Flatten a result dict (single- or multi-run) into metric -> value."""
    if result.get("type") == "multi_run":
        return dict(result["mean"])
    metrics = {f"scorer:{name}": s["mean"] for name, s in result.get("scores", {}).items()}
    if result.get("trajectory_score") is not None:
        metrics["trajectory"] = result["trajectory_score"]["mean"]
    metrics["pass_rate"] = result["pass_rate"]
    return metrics


def summarize_runs(run_dicts: list[dict]) -> dict:
    """Combine repeated runs into a multi-run summary with mean/std per metric.

    Agents are nondeterministic; a single run's 0.78 vs 0.80 can be pure
    noise. Multiple runs turn each metric into mean +/- std, which is what a
    regression gate should actually compare.
    """
    if not run_dicts:
        raise ValueError("no runs to summarize")

    metric_values: dict[str, list[float]] = {}
    for d in run_dicts:
        for name, value in flatten_metrics(d).items():
            metric_values.setdefault(name, []).append(value)

    return {
        "type": "multi_run",
        "run_count": len(run_dicts),
        "dataset_sha": run_dicts[0].get("dataset_sha"),
        "metadata": run_dicts[0].get("metadata", {}),
        "mean": {k: _mean(v) for k, v in metric_values.items()},
        "std": {k: (statistics.stdev(v) if len(v) > 1 else 0.0) for k, v in metric_values.items()},
        "runs": run_dicts,
    }
