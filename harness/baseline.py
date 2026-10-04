"""Save/load/compare baseline snapshots.

A baseline is a saved EvalResult from a known-good run. Future runs are
compared metric-by-metric against it; a drop bigger than the threshold on any
metric is a regression, and the CI gate turns that into a failing exit code.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from harness.results import flatten_metrics, protocol_metadata, validate_probability

DEFAULT_BASELINES_DIR = "baselines"


def _baseline_path(name: str, baselines_dir: str | Path) -> Path:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", name):
        raise ValueError(
            "baseline name must be 1-128 letters, numbers, dots, underscores, or hyphens "
            "and cannot begin with a dot"
        )
    directory = Path(baselines_dir)
    path = directory / f"{name}.json"
    # Also reject an existing baseline symlink that redirects outside this directory.
    if path.resolve().parent != directory.resolve():
        raise ValueError("baseline path must stay inside the baselines directory")
    return path


def save_baseline(name: str, result: dict, baselines_dir: str | Path = DEFAULT_BASELINES_DIR) -> Path:
    path = _baseline_path(name, baselines_dir)
    flatten_metrics(result)
    protocol_metadata(result)
    serialized = json.dumps(result, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")
    return path


def load_baseline(name: str, baselines_dir: str | Path = DEFAULT_BASELINES_DIR) -> dict:
    path = _baseline_path(name, baselines_dir)
    if not path.exists():
        raise FileNotFoundError(f"no baseline named {name!r} in {baselines_dir}/")
    result = json.loads(path.read_text(encoding="utf-8"))
    flatten_metrics(result)
    protocol_metadata(result)
    return result


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
    threshold = validate_probability(threshold, "regression threshold")
    current_protocol, baseline_protocol = protocol_metadata(current), protocol_metadata(baseline)
    for key in current_protocol.keys() & baseline_protocol.keys():
        if current_protocol[key] != baseline_protocol[key]:
            raise ValueError(
                f"scoring protocol changed: {key} was {baseline_protocol[key]!r}, "
                f"now {current_protocol[key]!r}; re-save the baseline with the same protocol"
            )
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
    # A decimal boundary such as .8 -> .75 must not fail a .05 gate because
    # subtraction differs by one or two floating-point rounding units.
    regressions = [
        d for d in deltas
        if d.delta < -threshold and not math.isclose(
            d.delta, -threshold, rel_tol=0.0,
            abs_tol=4 * math.ulp(max(abs(d.baseline), abs(d.current))),
        )
    ]
    return BaselineComparison(deltas=deltas, regressions=regressions)
