"""Serializable evaluation outcomes and descriptive statistics.

Case-sampling intervals and repeated-run variation have different assumptions;
neither proves that a fixed benchmark represents every real-world task.
"""
from __future__ import annotations

import json
import math
import statistics
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from numbers import Real
from pathlib import Path
from harness.runner import validate_json_value

SCHEMA_VERSION = 3
PASS_RULE = "mean_of_scorers_and_optional_trajectory"
ERROR_CASE_POLICY = "fail_and_zero_scores"
PROTOCOL_KEYS = ("pass_threshold", "pass_rule", "error_case_policy")


def validate_probability(value: float, name: str) -> float:
    """Require a finite real number in [0, 1], without accepting bools."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number in [0.0, 1.0], got {value!r}")
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} returned {value}, outside [0.0, 1.0]")
    return float(value)


def _nonnegative(value: float, name: str) -> float:
    try:
        finite = math.isfinite(value)
    except (TypeError, OverflowError):
        finite = False
    if (isinstance(value, bool) or not isinstance(value, Real)
            or not finite or value < 0):
        raise ValueError(f"{name} must be a finite nonnegative number, got {value!r}")
    return float(value)


@dataclass
class CaseResult:
    """One attempted case; latency_ms includes agent execution and scoring."""

    case_id: str
    input: str
    expected_output: str
    output: str
    scores: dict[str, float]
    trajectory: list[str] | None = None
    trajectory_score: float | None = None
    error: str | None = None
    latency_ms: float | None = None
    agent_latency_ms: float | None = None
    tags: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    metadata: dict = field(default_factory=dict)
    expected_trajectory: list[str] | None = None
    events: list[dict] = field(default_factory=list)
    error_stage: str | None = None
    rescore_latency_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.case_id, "input": self.input,
            "expected_output": self.expected_output, "output": self.output,
            "scores": self.scores, "trajectory": self.trajectory,
            "trajectory_score": self.trajectory_score, "error": self.error,
            "latency_ms": self.latency_ms, "agent_latency_ms": self.agent_latency_ms,
            "tags": list(self.tags), "usage": self.usage,
            "cost_usd": self.cost_usd, "metadata": self.metadata,
            "expected_trajectory": self.expected_trajectory,
            "events": self.events, "error_stage": self.error_stage,
            "rescore_latency_ms": self.rescore_latency_ms,
        }


@dataclass
class ScorerSummary:
    mean: float
    per_case: dict[str, float]


@dataclass
class EvalResult:
    run_id: str
    timestamp: str
    scores: dict[str, ScorerSummary]
    pass_rate: float
    trajectory_score: ScorerSummary | None = None
    error_count: int = 0
    dataset_sha: str | None = None
    metadata: dict = field(default_factory=dict)
    case_results: list[CaseResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        d = {
            "schema_version": self.schema_version, "run_id": self.run_id,
            "timestamp": self.timestamp,
            "scores": {name: {"mean": s.mean, "per_case": s.per_case} for name, s in self.scores.items()},
            "pass_rate": self.pass_rate, "error_count": self.error_count,
            "cases": [c.to_dict() for c in self.case_results], "summary": self.summary,
        }
        if self.trajectory_score is not None:
            d["trajectory_score"] = {
                "mean": self.trajectory_score.mean, "per_case": self.trajectory_score.per_case,
            }
        if self.dataset_sha:
            d["dataset_sha"] = self.dataset_sha
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, allow_nan=False), encoding="utf-8")


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _case_passes(case: CaseResult, threshold: float) -> bool:
    if case.error is not None:
        return False
    values = list(case.scores.values())
    if case.trajectory_score is not None:
        values.append(case.trajectory_score)
    return bool(values) and _mean(values) >= threshold


def _wilson_interval(passed: int, count: int) -> dict:
    z = 1.959963984540054
    fraction = passed / count
    denominator = 1 + z * z / count
    center = (fraction + z * z / (2 * count)) / denominator
    radius = z * math.sqrt(fraction * (1 - fraction) / count + z * z / (4 * count * count)) / denominator
    return {
        "lower": max(0.0, center - radius), "upper": min(1.0, center + radius),
        "confidence": 0.95, "method": "wilson",
        "assumptions": (
            "Binomial reference interval: meaningful for generalization only if cases are "
            "independent representative draws with a common pass probability. A fixed or "
            "correlated benchmark may violate these assumptions. This does not measure "
            "stochastic-agent uncertainty across repeated runs."
        ),
    }


def _latency_summary(values: list[float], total: int) -> dict:
    ordered = sorted(values)

    def percentile(p: float) -> float | None:
        if not ordered:
            return None
        position = (len(ordered) - 1) * p
        low, high = math.floor(position), math.ceil(position)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return {
        "count": len(values), "missing_count": total - len(values),
        "mean": _mean(values) if values else None,
        "min": min(values) if values else None, "max": max(values) if values else None,
        "p50": percentile(0.50), "p95": percentile(0.95), "p99": percentile(0.99),
        "percentile_method": "linear_interpolation",
    }


def _reported_summary(values: list[float], total: int) -> dict:
    return {
        "reported_total": math.fsum(values) if values else None,
        "reported_count": len(values), "missing_count": total - len(values),
    }


def _total_tokens(usage: dict) -> float | None:
    if "total_tokens" in usage:
        return usage["total_tokens"]
    for input_key, output_key in (("input_tokens", "output_tokens"), ("prompt_tokens", "completion_tokens")):
        if input_key in usage and output_key in usage:
            return usage[input_key] + usage[output_key]
    return None


def _summarize_cases(cases: list[CaseResult], threshold: float) -> dict:
    count = len(cases)
    passed = sum(_case_passes(c, threshold) for c in cases)
    errors = sum(c.error is not None for c in cases)
    usage_keys = sorted({key for c in cases for key in c.usage})
    tokens = [_total_tokens(c.usage) for c in cases]
    summary = {
        "case_count": count, "passed_count": passed, "failed_count": count - passed,
        "error_count": errors, "error_rate": errors / count, "pass_rate": passed / count,
        "pass_rate_ci95": _wilson_interval(passed, count),
        "scores": {name: _mean([c.scores[name] for c in cases]) for name in cases[0].scores},
        "latency_ms": _latency_summary([c.latency_ms for c in cases if c.latency_ms is not None], count),
        "agent_latency_ms": _latency_summary(
            [c.agent_latency_ms for c in cases if c.agent_latency_ms is not None], count
        ),
        "cost_usd": _reported_summary([c.cost_usd for c in cases if c.cost_usd is not None], count),
        "tokens": _reported_summary([v for v in tokens if v is not None], count),
        "usage": {
            key: _reported_summary([c.usage[key] for c in cases if key in c.usage], count)
            for key in usage_keys
        },
    }
    if any(c.rescore_latency_ms is not None for c in cases):
        summary["rescore_latency_ms"] = _latency_summary(
            [c.rescore_latency_ms for c in cases if c.rescore_latency_ms is not None], count
        )
    return summary


def _validate_cases(cases: list[CaseResult]) -> None:
    if not cases:
        raise ValueError("no case results to aggregate")
    ids = [c.case_id for c in cases]
    if any(not isinstance(case_id, str) or not case_id.strip() for case_id in ids):
        raise ValueError("case ids must be nonempty strings")
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    scorer_names = set(cases[0].scores)
    if not scorer_names or any(not isinstance(name, str) or not name.strip() for name in scorer_names):
        raise ValueError("at least one named scorer is required")
    for case in cases:
        if set(case.scores) != scorer_names:
            raise ValueError(f"case {case.case_id!r} has different scorer metrics")
        for name, value in case.scores.items():
            validate_probability(value, f"scorer {name!r}")
        if case.trajectory_score is not None:
            validate_probability(case.trajectory_score, "trajectory score")
        for name in ("latency_ms", "agent_latency_ms", "cost_usd", "rescore_latency_ms"):
            value = getattr(case, name)
            if value is not None:
                _nonnegative(value, name)
        if not isinstance(case.usage, dict):
            raise ValueError("usage must be a dictionary of numeric counters")
        for name, value in case.usage.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("usage counter names must be nonempty strings")
            _nonnegative(value, f"usage[{name!r}]")
        if any(not isinstance(tag, str) or not tag.strip() for tag in case.tags):
            raise ValueError("tags must be nonempty strings")
        if not isinstance(case.events, list) or any(not isinstance(event, dict) for event in case.events):
            raise ValueError("events must be a list of JSON objects")
        validate_json_value(case.events, "events")
        if case.error_stage not in (None, "agent", "validation", "scorer"):
            raise ValueError("error_stage must be agent, validation, scorer, or null")
        if case.error is None and case.error_stage is not None:
            raise ValueError("error_stage requires an error")


def aggregate(
    case_results: list[CaseResult],
    pass_threshold: float = 0.5,
    dataset_sha: str | None = None,
    metadata: dict | None = None,
) -> EvalResult:
    """Aggregate normalized scores; all attempted cases stay in denominators.

    A case passes when its unweighted mean of output scorers plus trajectory
    (when present) reaches the threshold. Errors never pass, even at zero.
    """
    pass_threshold = validate_probability(pass_threshold, "pass_threshold")
    _validate_cases(case_results)
    case_results = [
        replace(c, scores={name: 0.0 for name in c.scores},
                trajectory_score=0.0 if c.trajectory_score is not None else None)
        if c.error is not None else c
        for c in case_results
    ]
    scores = {
        name: ScorerSummary(
            mean=_mean([c.scores[name] for c in case_results]),
            per_case={c.case_id: c.scores[name] for c in case_results},
        )
        for name in case_results[0].scores
    }
    trajectory_score = None
    scored_traj = [c for c in case_results if c.trajectory_score is not None]
    if scored_traj:
        trajectory_score = ScorerSummary(
            mean=_mean([c.trajectory_score for c in scored_traj]),
            per_case={c.case_id: c.trajectory_score for c in scored_traj},
        )
    summary = _summarize_cases(case_results, pass_threshold)
    summary["by_tag"] = {
        tag: _summarize_cases([c for c in case_results if tag in c.tags], pass_threshold)
        for tag in sorted({tag for c in case_results for tag in c.tags})
    }
    run_metadata = dict(metadata or {})
    run_metadata.update(pass_threshold=pass_threshold, pass_rule=PASS_RULE, error_case_policy=ERROR_CASE_POLICY)
    return EvalResult(
        run_id=uuid.uuid4().hex[:8],
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        scores=scores, pass_rate=summary["pass_rate"], trajectory_score=trajectory_score,
        error_count=summary["error_count"], dataset_sha=dataset_sha,
        metadata=run_metadata, case_results=case_results, summary=summary,
    )


def flatten_metrics(result: dict) -> dict[str, float]:
    """Extract comparable quality metrics, rejecting invalid saved results."""
    try:
        if result.get("type") == "multi_run":
            metrics = dict(result["mean"])
        else:
            metrics = {f"scorer:{name}": s["mean"] for name, s in result.get("scores", {}).items()}
            if result.get("trajectory_score") is not None:
                metrics["trajectory"] = result["trajectory_score"]["mean"]
            metrics["pass_rate"] = result["pass_rate"]
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("invalid evaluation result: expected scores and pass_rate metrics") from exc
    if not metrics or "pass_rate" not in metrics:
        raise ValueError("result must contain pass_rate and comparable metrics")
    metrics = {name: validate_probability(value, f"metric {name!r}") for name, value in metrics.items()}
    if result.get("type") == "multi_run":
        _validate_saved_multi_run(result, metrics)
    return metrics


def _validate_saved_multi_run(result: dict, metrics: dict[str, float]) -> None:
    """A saved repeated-run summary must agree with its constituent runs."""
    count, runs = result.get("run_count"), result.get("runs")
    if type(count) is not int or count < 1 or not isinstance(runs, list) or len(runs) != count:
        raise ValueError("multi-run result must contain a positive run_count and matching runs")
    if any(not isinstance(run, dict) for run in runs):
        raise ValueError("multi-run constituent runs must be result objects")
    rebuilt = summarize_runs(runs)
    if set(metrics) != set(rebuilt["mean"]):
        raise ValueError("multi-run metric sets disagree with constituent runs")
    std = result.get("std")
    if not isinstance(std, dict) or set(std) != set(metrics):
        raise ValueError("multi-run result must include std for every metric")
    for name, value in metrics.items():
        if not math.isclose(value, rebuilt["mean"][name], rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f"multi-run mean for {name!r} disagrees with constituent runs")
        spread = _nonnegative(std[name], f"std[{name!r}]")
        if not math.isclose(spread, rebuilt["std"][name], rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f"multi-run std for {name!r} disagrees with constituent runs")
    if result.get("dataset_sha") != rebuilt.get("dataset_sha"):
        raise ValueError("multi-run dataset fingerprint disagrees with constituent runs")
    if protocol_metadata(result) != protocol_metadata(rebuilt):
        raise ValueError("multi-run scoring protocol disagrees with constituent runs")
    if grader_config_key(result) != grader_config_key(rebuilt):
        raise ValueError("multi-run grader_config disagrees with constituent runs")


def grader_config_key(result: dict) -> str | None:
    """Canonical public grader settings, distinct from legacy absent settings."""
    metadata = result.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("result metadata must be an object")
    if "grader_config" not in metadata:
        return None
    config = metadata["grader_config"]
    if not isinstance(config, list) or not config:
        raise ValueError("grader_config must be a nonempty list of grader descriptions")
    names = []
    for grader in config:
        if not isinstance(grader, dict):
            raise ValueError("grader_config entries must be objects")
        for field in ("name", "type"):
            if not isinstance(grader.get(field), str) or not grader[field].strip():
                raise ValueError(f"grader_config {field} must be a nonempty string")
        if not isinstance(grader.get("settings"), dict):
            raise ValueError("grader_config settings must be a JSON object")
        names.append(grader["name"])
    if len(names) != len(set(names)):
        raise ValueError("grader_config names must be unique")
    if "scores" in result and set(names) != set(result["scores"]):
        raise ValueError("grader_config names must match the recorded scorers")
    validate_json_value(config, "grader_config")
    return json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def protocol_metadata(result: dict) -> dict:
    """Known scoring protocol; legacy files may omit these fields."""
    if not isinstance(result, dict):
        raise ValueError("evaluation result must be an object")
    metadata = result.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("result metadata must be an object")
    protocol = {key: metadata[key] for key in PROTOCOL_KEYS if key in metadata}
    if "pass_threshold" in protocol:
        protocol["pass_threshold"] = validate_probability(protocol["pass_threshold"], "pass_threshold")
    for key in ("pass_rule", "error_case_policy"):
        if key in protocol and (not isinstance(protocol[key], str) or not protocol[key].strip()):
            raise ValueError(f"{key} must be a nonempty string")
    return protocol


def _case_identities(result: dict) -> tuple | None:
    """Compare case content/selection when raw cases or per-case scores exist."""
    if "cases" in result:
        cases = result["cases"]
        if not isinstance(cases, list) or not cases:
            raise ValueError("result cases must be a nonempty list")
        ids = [case["id"] for case in cases]
        if any(not isinstance(case_id, str) or not case_id.strip() for case_id in ids):
            raise ValueError("case ids must be nonempty strings")
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate case ids")
        for scorer in result.get("scores", {}).values():
            if "per_case" in scorer and set(scorer["per_case"]) != set(ids):
                raise ValueError("result has inconsistent per-case scorer identities")
        trajectory = result.get("trajectory_score")
        if trajectory is not None and "per_case" in trajectory:
            trajectory_ids = {case["id"] for case in cases if case.get("trajectory_score") is not None}
            if set(trajectory["per_case"]) != trajectory_ids:
                raise ValueError("result has inconsistent per-case trajectory identities")
        return tuple(sorted(
            (case["id"], case.get("input"), case.get("expected_output"),
             tuple(sorted(set(case.get("tags", [])))), case.get("trajectory_score") is not None,
             tuple(case["expected_trajectory"]) if case.get("expected_trajectory") is not None else None)
            for case in cases
        ))
    per_case_sets = [set(s.get("per_case", {})) for s in result.get("scores", {}).values()]
    if per_case_sets and any(per_case_sets):
        if any(ids != per_case_sets[0] for ids in per_case_sets[1:]):
            raise ValueError("result has inconsistent per-case scorer identities")
        trajectory = result.get("trajectory_score")
        trajectory_ids = None
        if trajectory is not None and "per_case" in trajectory:
            trajectory_ids = set(trajectory["per_case"])
            if not trajectory_ids <= per_case_sets[0]:
                raise ValueError("result has inconsistent per-case trajectory identities")
        return (tuple(sorted(per_case_sets[0])),
                tuple(sorted(trajectory_ids)) if trajectory_ids is not None else None)
    return None


def summarize_runs(run_dicts: list[dict]) -> dict:
    """Compute mean and sample SD for repeated full runs of the same protocol.

    Variation is across runs, not pooled cases. A single run retains SD=0 for
    compatibility; it supplies no variance estimate.
    """
    if not run_dicts:
        raise ValueError("no runs to summarize")
    if any(run.get("type") == "multi_run" for run in run_dicts):
        raise ValueError("summarize_runs expects individual runs, not multi-run summaries")
    metrics = [flatten_metrics(run) for run in run_dicts]
    first = run_dicts[0]
    first_protocol = protocol_metadata(first)
    first_graders = grader_config_key(first)
    first_cases = _case_identities(first)
    for run, current_metrics in zip(run_dicts[1:], metrics[1:]):
        if set(current_metrics) != set(metrics[0]):
            raise ValueError("cannot summarize runs with different metric sets")
        if run.get("dataset_sha") != first.get("dataset_sha"):
            raise ValueError("cannot summarize runs with different dataset fingerprints")
        if protocol_metadata(run) != first_protocol:
            raise ValueError("cannot summarize runs with different scoring protocol or pass_threshold")
        if grader_config_key(run) != first_graders:
            raise ValueError("cannot summarize runs with different grader_config or missing grader settings")
        if _case_identities(run) != first_cases:
            raise ValueError("cannot summarize runs with different case identities or selections")
    metric_values = {name: [metric[name] for metric in metrics] for name in metrics[0]}
    return {
        "schema_version": SCHEMA_VERSION, "type": "multi_run", "run_count": len(run_dicts),
        "dataset_sha": first.get("dataset_sha"), "metadata": first.get("metadata", {}),
        "mean": {key: _mean(values) for key, values in metric_values.items()},
        "std": {
            key: statistics.stdev(values) if len(values) > 1 else 0.0
            for key, values in metric_values.items()
        },
        "statistics": {
            "unit": "full evaluation run",
            "standard_deviation": "sample (ddof=1); zero with one run is not a variance estimate",
            "note": (
                "Descriptive variation across repeated runs on the same cases and scoring protocol. "
                "Cases are not pooled as independent new samples; means and SDs alone do not "
                "establish statistical significance."
            ),
        },
        "runs": run_dicts,
    }
