"""The eval loop: dataset x runner x scorers -> EvalResult.

Production behaviors:
- Concurrency: cases run in a thread pool (agents and LLM scorers are
  I/O-bound), preserving dataset order in the results.
- Failure isolation: one crashing case doesn't kill the run. The error is
  recorded on that case, its scores are 0.0, and the run continues.
- Latency: wall-clock ms per case, recorded on every CaseResult.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from harness.dataset import EvalCase
from harness.results import CaseResult, EvalResult, aggregate
from harness.runner import AgentRunner
from harness.scorers.base import Scorer
from harness.trajectory import score_trajectory


def _validated(name: str, score: float) -> float:
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"scorer {name!r} returned {score}, outside [0.0, 1.0]")
    return score


def _run_case(case: EvalCase, runner: AgentRunner, scorers: list[Scorer]) -> CaseResult:
    start = time.perf_counter()
    try:
        agent_output = runner.run(case.input)
        scores = {
            s.name: _validated(s.name, s.score(case.expected_output, agent_output.output))
            for s in scorers
        }
        trajectory_score = None
        if case.expected_trajectory is not None:
            trajectory_score = score_trajectory(case.expected_trajectory, agent_output.trajectory or [])
        return CaseResult(
            case_id=case.id,
            input=case.input,
            expected_output=case.expected_output,
            output=agent_output.output,
            scores=scores,
            trajectory=agent_output.trajectory,
            trajectory_score=trajectory_score,
            latency_ms=(time.perf_counter() - start) * 1000,
        )
    except Exception as e:  # isolate the failure to this case
        return CaseResult(
            case_id=case.id,
            input=case.input,
            expected_output=case.expected_output,
            output="",
            scores={s.name: 0.0 for s in scorers},
            trajectory=None,
            trajectory_score=0.0 if case.expected_trajectory is not None else None,
            error=f"{type(e).__name__}: {e}",
            latency_ms=(time.perf_counter() - start) * 1000,
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
    if not scorers:
        raise ValueError("at least one scorer is required")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")

    names = [s.name for s in scorers]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate scorer names: {names}")

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
        metadata=metadata,
    )
