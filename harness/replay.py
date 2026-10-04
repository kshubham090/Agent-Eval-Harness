"""Validate recordings and score their saved responses without calling an agent.

Recordings are evidence, not executable sessions. Replaying cannot restore a
tool environment, and rescoring never repeats tool calls or changes files.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
import platform
import re
import time

from harness.eval_runner import describe_scorers
from harness.results import (
    CaseResult, ERROR_CASE_POLICY, PASS_RULE, PROTOCOL_KEYS, _nonnegative,
    aggregate, flatten_metrics, grader_config_key, protocol_metadata, summarize_runs, validate_probability,
)
from harness.runner import validate_json_value
from harness.scorers.base import Scorer
from harness.trajectory import score_trajectory


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _string(value: object, name: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValueError(f"{name} must be {'a nonempty string' if nonempty else 'a string'}")
    return value


def _strings(value: object, name: str, *, nonempty: bool = False, unique: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list of strings")
    for item in value:
        _string(item, name, nonempty=nonempty)
    if unique and len(set(value)) != len(value):
        raise ValueError(f"{name} must not contain duplicates")
    return value


def _case_from_dict(raw: dict) -> CaseResult:
    _object(raw, "case")
    case_id = _string(raw.get("id"), "case id", nonempty=True)
    scores = _object(raw.get("scores"), f"case {case_id!r} scores")
    if not scores:
        raise ValueError("case scores must contain at least one scorer")
    for name, value in scores.items():
        _string(name, "scorer name", nonempty=True)
        validate_probability(value, f"scorer {name!r}")
    for name in ("trajectory", "expected_trajectory"):
        if raw.get(name) is not None:
            _strings(raw[name], name)
    trajectory_score = raw.get("trajectory_score")
    if trajectory_score is not None:
        validate_probability(trajectory_score, "trajectory_score")
    if (raw.get("expected_trajectory") is None) != (trajectory_score is None):
        raise ValueError("trajectory score requires its saved expected_trajectory and vice versa")
    error = raw.get("error")
    if error is not None:
        _string(error, "case error", nonempty=True)
    error_stage = raw.get("error_stage")
    if error_stage not in (None, "agent", "validation", "scorer"):
        raise ValueError("error_stage must be agent, validation, scorer, or null")
    if error is None and error_stage is not None:
        raise ValueError("error_stage requires an error")
    usage = _object(raw.get("usage", {}), "usage")
    for key, value in usage.items():
        _string(key, "usage counter name", nonempty=True)
        _nonnegative(value, f"usage[{key!r}]")
    for name in ("latency_ms", "agent_latency_ms", "cost_usd", "rescore_latency_ms"):
        if raw.get(name) is not None:
            _nonnegative(raw[name], name)
    events = raw.get("events", [])
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise ValueError("events must be a list of JSON objects")
    return CaseResult(
        case_id=case_id, input=_string(raw.get("input"), "case input"),
        expected_output=_string(raw.get("expected_output"), "case expected_output"),
        output=_string(raw.get("output"), "case output"), scores=scores,
        trajectory=raw.get("trajectory"), trajectory_score=trajectory_score,
        expected_trajectory=raw.get("expected_trajectory"), error=error, error_stage=error_stage,
        latency_ms=raw.get("latency_ms"), agent_latency_ms=raw.get("agent_latency_ms"),
        rescore_latency_ms=raw.get("rescore_latency_ms"),
        tags=_strings(raw.get("tags", []), "tags", nonempty=True, unique=True),
        usage=usage, cost_usd=raw.get("cost_usd"),
        metadata=_object(raw.get("metadata", {}), "case metadata"), events=events,
    )


def _same(actual: object, expected: object, name: str) -> None:
    """Check derived evidence without treating bools as numeric scores."""
    if type(expected) is int:
        if type(actual) is not int or actual != expected:
            raise ValueError(f"recording {name} disagrees with raw case results")
    elif type(actual) in (int, float) and type(expected) is float:
        if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f"recording {name} disagrees with raw case results")
    elif isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            raise ValueError(f"recording {name} has missing or inconsistent fields")
        for key in expected:
            _same(actual[key], expected[key], f"{name}.{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise ValueError(f"recording {name} has inconsistent entries")
        for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
            _same(left, right, f"{name}[{index}]")
    elif type(actual) is not type(expected) or actual != expected:
        raise ValueError(f"recording {name} disagrees with raw case results")


def _validate_metadata(payload: dict) -> dict:
    metadata = _object(payload.get("metadata"), "recording metadata")
    grader_config_key(payload)
    protocol = protocol_metadata(payload)
    if set(protocol) != set(PROTOCOL_KEYS):
        raise ValueError("recording must include its complete scoring protocol")
    if protocol["pass_rule"] != PASS_RULE or protocol["error_case_policy"] != ERROR_CASE_POLICY:
        raise ValueError("recording uses an unsupported scoring protocol")
    if "dataset_sha" in payload and payload["dataset_sha"] is not None:
        _string(payload["dataset_sha"], "dataset_sha", nonempty=True)
    if "rescore" in metadata:
        rescore = _object(metadata["rescore"], "rescore provenance")
        if not re.fullmatch(r"[0-9a-f]{64}", str(rescore.get("source_sha256", ""))):
            raise ValueError("rescore source_sha256 must be a lowercase SHA-256 digest")
        _strings(rescore.get("source_run_ids"), "rescore source_run_ids", nonempty=True)
        if rescore.get("agent_calls") != 0 or type(rescore.get("agent_calls")) is not int:
            raise ValueError("rescore provenance must declare zero agent_calls")
    if "rescore_history" in metadata:
        history = metadata["rescore_history"]
        if not isinstance(history, list) or any(not isinstance(entry, dict) for entry in history):
            raise ValueError("rescore_history must be a list of provenance objects")
    return metadata


def _validate(payload: dict) -> None:
    _object(payload, "recording")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] not in (2, 3):
        raise ValueError("replay requires a complete schema version 2 or 3 recording")
    _validate_metadata(payload)
    if payload.get("type") == "multi_run":
        runs = payload.get("runs")
        count = payload.get("run_count")
        if type(count) is not int or count < 1 or not isinstance(runs, list) or len(runs) != count:
            raise ValueError("multi-run recording must contain a positive run_count and matching runs")
        for run in runs:
            if not isinstance(run, dict) or run.get("type") == "multi_run":
                raise ValueError("multi-run recordings must contain individual runs")
            _validate(run)
        rebuilt = summarize_runs(runs)
        for name in ("mean", "std", "dataset_sha"):
            _same(payload.get(name), rebuilt[name], name)
        _same(protocol_metadata(payload), protocol_metadata(rebuilt), "scoring protocol")
        # Also validate metric names and bounds before a gate uses this file.
        flatten_metrics(payload)
        return
    if payload.get("type") not in (None, "single_run"):
        raise ValueError("unsupported recording type")
    _string(payload.get("run_id"), "run_id", nonempty=True)
    _string(payload.get("timestamp"), "timestamp", nonempty=True)
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("replay requires a nonempty list of saved raw cases")
    cases = [_case_from_dict(case) for case in raw_cases]
    rebuilt = aggregate(cases, pass_threshold=payload["metadata"]["pass_threshold"],
                        dataset_sha=payload.get("dataset_sha")).to_dict()
    for name in ("scores", "pass_rate", "error_count", "summary"):
        _same(payload.get(name), rebuilt[name], name)
    _same(payload.get("trajectory_score"), rebuilt.get("trajectory_score"), "trajectory_score")
    # Individual raw error cases cannot contain positive scores even if the
    # headline was independently zeroed to appear consistent.
    for raw, normalized in zip(raw_cases, rebuilt["cases"], strict=True):
        _same(raw["scores"], normalized["scores"], f"case {raw['id']!r} scores")
        _same(raw.get("trajectory_score"), normalized["trajectory_score"],
              f"case {raw['id']!r} trajectory_score")


def validate_recording(payload: dict) -> dict:
    """Return an independent, validated recording; never normalize bad evidence.

    Full raw cases and a supported pass protocol are required. Legacy compact
    baselines still work with the baseline commands, but cannot be rescored.
    Unknown additive JSON fields are retained. Hashes identify content; they
    are not signatures or proof that the producer was trustworthy.
    """
    try:
        validate_json_value(payload, "recording")
        _validate(payload)
        return deepcopy(payload)
    except (RecursionError, OverflowError) as exc:
        raise ValueError("recording contains unsupported nesting or numeric magnitude") from exc


def load_recording(path: str | Path) -> dict:
    """Read strict finite JSON, rejecting duplicate fields at every depth."""
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"recording contains duplicate JSON field {key!r}")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"recording contains nonfinite JSON constant {value}")

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"),
                             object_pairs_hook=unique_fields, parse_constant=reject_constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("recording must be UTF-8 JSON with supported nesting") from exc
    return validate_recording(payload)


def _rescore_case(raw: dict, scorers: list[Scorer]) -> CaseResult:
    case = _case_from_dict(deepcopy(raw))
    # An output-validation failure or unknown legacy error is not evidence of
    # a valid agent response. Never turn one into a pass by grading its text.
    if case.error is not None and case.error_stage != "scorer":
        case.scores = {scorer.name: 0.0 for scorer in scorers}
        case.trajectory_score = 0.0 if case.expected_trajectory is not None else None
        case.rescore_latency_ms = None
        return case
    start = time.perf_counter()
    if case.error is not None:
        case.metadata["previous_scoring_error"] = {"error": case.error, "stage": case.error_stage}
    case.error = None
    case.error_stage = None
    try:
        case.scores = {
            scorer.name: validate_probability(scorer.score(case.expected_output, case.output),
                                               f"scorer {scorer.name!r}")
            for scorer in scorers
        }
        case.trajectory_score = (
            validate_probability(score_trajectory(case.expected_trajectory, case.trajectory or []),
                                 "trajectory_score")
            if case.expected_trajectory is not None else None
        )
    except Exception as exc:
        case.scores = {scorer.name: 0.0 for scorer in scorers}
        case.trajectory_score = 0.0 if case.expected_trajectory is not None else None
        case.error = f"{type(exc).__name__}: {exc}"
        case.error_stage = "scorer"
    finally:
        case.rescore_latency_ms = (time.perf_counter() - start) * 1000
    return case


def rescore_result(payload: dict, scorers: list[Scorer], pass_threshold: float | None = None,
                   concurrency: int = 1) -> dict:
    """Regrade saved outputs by case ID, without calling the original agent.

    The original execution latencies, events, usage and cost remain evidence
    from the source run. Only ``rescore_latency_ms`` measures the new grading.
    A judge scorer can itself call a model and incur unmetered grading cost.
    """
    recording = validate_recording(payload)
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency must be an integer >= 1")
    if pass_threshold is not None:
        pass_threshold = validate_probability(pass_threshold, "pass_threshold")
    if not scorers:
        raise ValueError("at least one scorer is required")
    names = [scorer.name for scorer in scorers]
    if any(not isinstance(name, str) or not name.strip() for name in names) or len(set(names)) != len(names):
        raise ValueError("scorer names must be nonempty and unique")
    source_runs = recording["runs"] if recording.get("type") == "multi_run" else [recording]
    if any(run["metadata"].get("evaluation_kind") == "coding"
           or any(grader["type"] == "harness.coding.held_out_tests"
                  for grader in run["metadata"].get("grader_config", []))
           for run in source_runs):
        raise ValueError("coding recordings require rerunning their held-out tests; text rescore is unsupported")
    graders = describe_scorers(scorers)
    source_hash = hashlib.sha256(json.dumps(recording, sort_keys=True, ensure_ascii=False,
                                           separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    provenance = {
        "source_sha256": source_hash, "source_run_ids": [run["run_id"] for run in source_runs],
        "agent_calls": 0, "protocol": "saved_output_rescore_v1",
        "timing": "latency_ms and agent_latency_ms are source timings; rescore_latency_ms is new grader time",
        "cost": "cost_usd and usage are source agent telemetry; new grader cost is not metered",
        "python": platform.python_version(),
    }
    try:
        provenance["harness_version"] = version("agent-eval-harness")
    except PackageNotFoundError:
        provenance["harness_version"] = "uninstalled source checkout"
    results = []
    for source in source_runs:
        metadata = deepcopy(source["metadata"])
        if "rescore" in metadata:
            metadata.setdefault("rescore_history", []).append(metadata["rescore"])
        metadata["rescore"] = {**provenance, "source_run_id": source["run_id"]}
        metadata["grader_config"] = deepcopy(graders)
        metadata["scorers"] = list(names)
        metadata["concurrency"] = concurrency
        metadata["evaluation_mode"] = "rescore"
        threshold = source["metadata"]["pass_threshold"] if pass_threshold is None else pass_threshold
        if concurrency == 1:
            cases = [_rescore_case(raw, scorers) for raw in source["cases"]]
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                cases = list(pool.map(lambda raw: _rescore_case(raw, scorers), source["cases"]))
        results.append(aggregate(cases, pass_threshold=threshold,
                                 dataset_sha=source.get("dataset_sha"), metadata=metadata).to_dict())
    result = summarize_runs(results) if recording.get("type") == "multi_run" else results[0]
    return validate_recording(result)
