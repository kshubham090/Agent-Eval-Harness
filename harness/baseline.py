"""Save/load/compare baseline snapshots.

A baseline is a saved EvalResult from a known-good run. Future runs are
compared metric-by-metric against it; a drop bigger than the threshold on any
metric is a regression, and the CI gate turns that into a failing exit code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from harness.results import flatten_metrics

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


def compare_to_baseline(
    current: dict,
    baseline: dict,
    threshold: float = 0.05,
    ignore_dataset_mismatch: bool = False,
) -> BaselineComparison:
    """Compare a current result to a baseline.

    A metric regresses when it drops by more than `threshold` (absolute) below
    the baseline. Every metric present in the baseline must exist in the
    current run -- a vanished metric would otherwise hide a regression. When
    both sides carry a dataset_sha, they must match: comparing runs from
    different dataset versions is meaningless.
    """
    current_sha, baseline_sha = current.get("dataset_sha"), baseline.get("dataset_sha")
    if current_sha and baseline_sha and current_sha != baseline_sha and not ignore_dataset_mismatch:
        raise ValueError(
            f"dataset changed since the baseline was saved ({baseline_sha} -> {current_sha}); "
            "re-save the baseline, or pass --allow-dataset-change to compare anyway"
        )

    current_metrics = flatten_metrics(current)
    baseline_metrics = flatten_metrics(baseline)

    missing = set(baseline_metrics) - set(current_metrics)
    if missing:
        raise ValueError(f"baseline metrics missing from current run: {sorted(missing)}")

    deltas = [
        MetricDelta(metric=name, baseline=baseline_metrics[name], current=current_metrics[name])
        for name in sorted(baseline_metrics)
    ]
    regressions = [d for d in deltas if d.delta < -threshold]
    return BaselineComparison(deltas=deltas, regressions=regressions)
