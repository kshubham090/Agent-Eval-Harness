"""Save/load/compare baseline snapshots.

A baseline is a saved EvalResult from a known-good run. Future runs are
compared metric-by-metric against it; a drop bigger than the threshold on any
metric is a regression, and the CI gate turns that into a failing exit code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASELINES_DIR = "baselines"


def save_baseline(name: str, result: dict, baselines_dir: str | Path = DEFAULT_BASELINES_DIR) -> Path:
    path = Path(baselines_dir) / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return path


def load_baseline(name: str, baselines_dir: str | Path = DEFAULT_BASELINES_DIR) -> dict:
    path = Path(baselines_dir) / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"no baseline named {name!r} in {baselines_dir}/")
    return json.loads(path.read_text(encoding="utf-8"))


def _metrics(result: dict) -> dict[str, float]:
    """Flatten a result dict into comparable metric-name -> mean pairs."""
    metrics = {f"scorer:{name}": s["mean"] for name, s in result.get("scores", {}).items()}
    if result.get("trajectory_score") is not None:
        metrics["trajectory"] = result["trajectory_score"]["mean"]
    metrics["pass_rate"] = result["pass_rate"]
    return metrics


@dataclass
class MetricDelta:
    metric: str
    baseline: float
    current: float

    @property
    def delta(self) -> float:
        return self.current - self.baseline


@dataclass
class BaselineComparison:
    deltas: list[MetricDelta]
    regressions: list[MetricDelta] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.regressions


def compare_to_baseline(current: dict, baseline: dict, threshold: float = 0.05) -> BaselineComparison:
    """Compare a current result to a baseline.

    A metric regresses when it drops by more than `threshold` (absolute) below
    the baseline. Every metric present in the baseline must exist in the
    current run -- a vanished metric would otherwise hide a regression.
    """
    current_metrics = _metrics(current)
    baseline_metrics = _metrics(baseline)

    missing = set(baseline_metrics) - set(current_metrics)
    if missing:
        raise ValueError(f"baseline metrics missing from current run: {sorted(missing)}")

    deltas = [
        MetricDelta(metric=name, baseline=baseline_metrics[name], current=current_metrics[name])
        for name in sorted(baseline_metrics)
    ]
    regressions = [d for d in deltas if d.delta < -threshold]
    return BaselineComparison(deltas=deltas, regressions=regressions)
