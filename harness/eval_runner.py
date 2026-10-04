"""The eval loop: dataset x runner x scorers -> EvalResult.

Production behaviors:
- Concurrency: cases run in a thread pool (agents and LLM scorers are
  I/O-bound), preserving dataset order in the results.
- Failure isolation: one crashing case doesn't kill the run. The error is
  recorded on that case, its scores are 0.0, and the run continues.
- Latency: wall-clock ms per case, recorded on every CaseResult.
"""

from __future__ import annotations

import json
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from copy import deepcopy

from harness.dataset import EvalCase
from harness.results import CaseResult, EvalResult, _nonnegative, aggregate, validate_probability
from harness.runner import AgentRunner, validate_json_value
from harness.scorers.base import Scorer
from harness.trajectory import score_trajectory


def _validated(name: str, score: float) -> float:
    return validate_probability(score, f"scorer {name!r}")


def describe_scorers(scorers: list[Scorer]) -> list[dict]:
    """Record safe effective settings without inspecting arbitrary object state.

    Custom scorers may expose ``evaluation_config()`` returning intentionally
    public JSON settings. Closures, API clients, and private attributes are
    never serialized; callable names identify custom backends, not their code.
    """
    descriptions = []
    for scorer in scorers:
        scorer_type = f"{type(scorer).__module__}.{type(scorer).__qualname__}"
        settings = {}
        reproducibility = "custom scorer; implementation and environment must be retained"
        if scorer_type == "harness.scorers.exact.ExactMatchScorer":
            settings = {"case_sensitive": scorer.case_sensitive}
            reproducibility = "deterministic for fixed harness version and settings"
        elif scorer_type == "harness.scorers.regex_scorer.RegexScorer":
            settings = {"full_match": scorer.full_match}
            reproducibility = "deterministic for fixed harness version and settings"
        elif scorer_type in (
            "harness.scorers.structured.ContainsScorer",
            "harness.scorers.structured.JSONMatchScorer",
        ):
            reproducibility = "deterministic for fixed harness version"
        elif scorer_type == "harness.scorers.embedding.EmbeddingScorer":
            settings = {"model_name": scorer.model_name,
                        "backend_callable": _callable_identity(
                            scorer._embed, "harness.scorers.embedding._load_sentence_transformer.<locals>.<lambda>")}
            reproducibility = "model weights, backend, package versions, and environment are not pinned"
        elif scorer_type == "harness.scorers.llm_judge.LLMJudgeScorer":
            from harness.scorers.llm_judge import JUDGE_PROMPT_TEMPLATE, JUDGE_SYSTEM_PROMPT
            settings = {
                "model": scorer.model, "backend_callable": _callable_identity(
                    scorer._complete, "harness.scorers.llm_judge._make_anthropic_backend.<locals>.complete"),
                "default_backend": {"max_tokens": 512, "system_prompt": JUDGE_SYSTEM_PROMPT},
                "prompt_template": JUDGE_PROMPT_TEMPLATE,
            }
            reproducibility = "external judge output may vary; model aliases and custom backends are not pinned"
        config = getattr(scorer, "evaluation_config", None)
        if callable(config):
            settings = config()
            if not isinstance(settings, dict):
                raise ValueError("scorer evaluation_config() must return a JSON object")
        validate_json_value(settings, "scorer settings")
        descriptions.append({"name": scorer.name, "type": scorer_type,
                             "settings": deepcopy(settings), "reproducibility": reproducibility})
    return descriptions


def _callable_identity(function, default_identity: str) -> str:
    if function is None:
        return "default backend"
    identity = f"{getattr(function, '__module__', type(function).__module__)}.{getattr(function, '__qualname__', type(function).__qualname__)}"
    return "default backend" if identity == default_identity else identity


def _run_case(case: EvalCase, runner: AgentRunner, scorers: list[Scorer]) -> CaseResult:
    start = time.perf_counter()
    agent_latency_ms = None
    output = ""
    trajectory = None
    usage = {}
    cost_usd = None
    metadata = {}
    events = []
    error_stage = "agent"
    try:
        try:
            agent_output = runner.run(case.input)
        finally:
            agent_latency_ms = (time.perf_counter() - start) * 1000
        error_stage = "validation"
        output = agent_output.output
        if not isinstance(output, str):
            output = ""
            raise ValueError("agent output must be a string")
        trajectory = agent_output.trajectory
        if trajectory is not None and (
            not isinstance(trajectory, list) or not all(isinstance(step, str) for step in trajectory)
        ):
            trajectory = None
            raise ValueError("agent trajectory must be a list of strings or null")
        raw_usage = getattr(agent_output, "usage", {})
        if not isinstance(raw_usage, dict):
            raise ValueError("agent usage must be a dictionary of numeric counters")
        validated_usage = {}
        for name, value in raw_usage.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("usage counter names must be nonempty strings")
            numeric_value = _nonnegative(value, f"usage[{name!r}]")
            # Real implementations such as Fraction are valid counters but
            # cannot be encoded as JSON. Keep built-in integers exact.
            validated_usage[name] = value if type(value) in (int, float) else numeric_value
        usage = validated_usage
        raw_cost = getattr(agent_output, "cost_usd", None)
        if raw_cost is not None:
            cost_usd = _nonnegative(raw_cost, "cost_usd")
        raw_metadata = getattr(agent_output, "metadata", {})
        if not isinstance(raw_metadata, dict):
            raise ValueError("agent metadata must be a dictionary")
        validate_json_value(raw_metadata, "agent metadata")
        metadata = deepcopy(raw_metadata)
        raw_events = getattr(agent_output, "events", [])
        if not isinstance(raw_events, list) or any(not isinstance(event, dict) for event in raw_events):
            raise ValueError("agent events must be a list of JSON objects")
        validate_json_value(raw_events, "agent events")
        events = deepcopy(raw_events)
        error_stage = "scorer"
        scores = {
            s.name: _validated(s.name, s.score(case.expected_output, output))
            for s in scorers
        }
        trajectory_score = None
        if case.expected_trajectory is not None:
            trajectory_score = _validated(
                "trajectory", score_trajectory(case.expected_trajectory, trajectory or [])
            )
        return CaseResult(
            case_id=case.id,
            input=case.input,
            expected_output=case.expected_output,
            output=output,
            scores=scores,
            trajectory=trajectory,
            trajectory_score=trajectory_score,
            latency_ms=(time.perf_counter() - start) * 1000,
            agent_latency_ms=agent_latency_ms,
            tags=list(case.tags),
            usage=usage,
            cost_usd=cost_usd,
            metadata=metadata,
            expected_trajectory=list(case.expected_trajectory) if case.expected_trajectory is not None else None,
            events=events,
        )
    except Exception as e:  # isolate the failure to this case
        return CaseResult(
            case_id=case.id,
            input=case.input,
            expected_output=case.expected_output,
            output=output,
            scores={s.name: 0.0 for s in scorers},
            trajectory=trajectory,
            trajectory_score=0.0 if case.expected_trajectory is not None else None,
            error=f"{type(e).__name__}: {e}",
            latency_ms=(time.perf_counter() - start) * 1000,
            agent_latency_ms=agent_latency_ms,
            tags=list(case.tags),
            usage=usage,
            cost_usd=cost_usd,
            metadata=metadata,
            expected_trajectory=list(case.expected_trajectory) if case.expected_trajectory is not None else None,
            events=events,
            error_stage=error_stage,
        )


def run_eval(
    cases: list[EvalCase],
    runner: AgentRunner,
    scorers: list[Scorer],
    pass_threshold: float = 0.5,
    concurrency: int = 1,
    dataset_sha: str | None = None,
    metadata: dict | None = None,
) -> EvalResult:
    pass_threshold = validate_probability(pass_threshold, "pass_threshold")
    if not cases:
        raise ValueError("at least one evaluation case is required")
    ids = [case.id for case in cases]
    if any(not isinstance(case_id, str) or not case_id.strip() for case_id in ids):
        raise ValueError("case ids must be nonempty strings")
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    if any(not isinstance(tag, str) or not tag.strip() for case in cases for tag in case.tags):
        raise ValueError("tags must be nonempty strings")
    if not scorers:
        raise ValueError("at least one scorer is required")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be an integer >= 1")

    names = [s.name for s in scorers]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("scorer names must be nonempty strings")
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate scorer names: {names}")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be a dictionary")
    validate_json_value(metadata, "metadata")
    run_metadata = deepcopy(metadata or {})
    run_metadata["grader_config"] = describe_scorers(scorers)
    if dataset_sha is None:
        canonical = json.dumps([asdict(c) for c in cases], sort_keys=True, ensure_ascii=False)
        dataset_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    if concurrency == 1:
        case_results = [_run_case(case, runner, scorers) for case in cases]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            # executor.map preserves input order regardless of completion order
            case_results = list(pool.map(lambda c: _run_case(c, runner, scorers), cases))

    return aggregate(
        case_results,
        pass_threshold=pass_threshold,
        dataset_sha=dataset_sha,
        metadata=run_metadata,
    )
