"""The eval loop: dataset x runner x scorers -> EvalResult."""

from __future__ import annotations

from harness.dataset import EvalCase
from harness.results import CaseResult, EvalResult, aggregate
from harness.runner import AgentRunner
from harness.scorers.base import Scorer
from harness.trajectory import score_trajectory


def run_eval(
    cases: list[EvalCase],
    runner: AgentRunner,
    scorers: list[Scorer],
    pass_threshold: float = 0.5,
) -> EvalResult:
    if not scorers:
        raise ValueError("at least one scorer is required")

    names = [s.name for s in scorers]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate scorer names: {names}")

    case_results = []
    for case in cases:
        agent_output = runner.run(case.input)
        trajectory_score = None
        if case.expected_trajectory is not None:
            trajectory_score = score_trajectory(case.expected_trajectory, agent_output.trajectory or [])
        case_results.append(
            CaseResult(
                case_id=case.id,
                output=agent_output.output,
                scores={
                    s.name: _validated(s.name, s.score(case.expected_output, agent_output.output))
                    for s in scorers
                },
                trajectory=agent_output.trajectory,
                trajectory_score=trajectory_score,
            )
        )

    return aggregate(case_results, pass_threshold=pass_threshold)


def _validated(name: str, score: float) -> float:
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"scorer {name!r} returned {score}, outside [0.0, 1.0]")
    return score
