import copy

import pytest

from harness.dataset import EvalCase
from harness.eval_runner import run_eval
from harness.gates import evaluate_gates
from harness.results import summarize_runs
from harness.runner import AgentOutput
from harness.scorers.exact import ExactMatchScorer


def recording(*, cost=0.1, answer="yes"):
    class Agent:
        def run(self, prompt):
            return AgentOutput(answer, cost_usd=cost)
    return run_eval([EvalCase("one", "prompt", "yes")], Agent(), [ExactMatchScorer()]).to_dict()


def test_cost_is_sum_across_repeated_runs_and_exact_limit_passes():
    data = summarize_runs([recording(), recording()])
    assert evaluate_gates(data, max_cost_usd=0.2).passed
    verdict = evaluate_gates(data, max_cost_usd=0.15)
    assert not verdict.passed
    assert verdict.checks[-1]["value"] == 0.2


def test_unreported_cost_fails_even_with_high_budget():
    verdict = evaluate_gates(recording(cost=None), max_cost_usd=100)
    assert not verdict.passed
    assert verdict.checks[-1]["missing_count"] == 1
    assert verdict.checks[-1]["value"] is None


def test_quality_must_pass_in_each_run():
    data = summarize_runs([recording(), recording(answer="no")])
    assert not evaluate_gates(data, min_pass_rate=0.5).passed


def test_latency_gate_uses_agent_measurement():
    data = recording()
    assert evaluate_gates(data, max_p95_latency_ms=60_000).passed
    assert not evaluate_gates(data, max_p95_latency_ms=0).passed


def test_tampered_summary_cannot_bypass_gate():
    data = copy.deepcopy(recording(cost=1))
    data["summary"]["cost_usd"]["reported_total"] = 0
    with pytest.raises(ValueError):
        evaluate_gates(data, max_cost_usd=0.5)


@pytest.mark.parametrize("limits", [{"max_cost_usd": float("nan")}, {"max_p95_latency_ms": -1},
                                    {"min_pass_rate": True}, {"max_error_rate": 1.1}])
def test_limits_reject_invalid_numbers(limits):
    with pytest.raises(ValueError):
        evaluate_gates(recording(), **limits)
