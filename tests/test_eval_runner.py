import threading
import time

import pytest

from harness.dataset import filter_by_tags, load_dataset
from harness.eval_runner import run_eval
from harness.runner import AgentOutput
from harness.scorers import ExactMatchScorer


class StubAgent:
    """Always answers 'Paris' -- right for q1, wrong for everything else."""

    def run(self, input: str) -> AgentOutput:
        return AgentOutput(output="Paris")


class EchoAgent:
    def __init__(self, answers: dict[str, str]):
        self.answers = answers

    def run(self, input: str) -> AgentOutput:
        return AgentOutput(output=self.answers[input])


def test_end_to_end_stub_agent_on_simple_qa():
    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    result = run_eval(cases, StubAgent(), [ExactMatchScorer()])

    paris_rate = sum(1 for c in cases if c.expected_output == "Paris") / len(cases)
    assert result.scores["exact_match"].mean == pytest.approx(paris_rate)
    assert result.scores["exact_match"].per_case["q1"] == 1.0
    assert result.scores["exact_match"].per_case["q2"] == 0.0
    assert result.pass_rate == pytest.approx(paris_rate)
    assert result.error_count == 0


def test_perfect_agent_scores_one():
    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    agent = EchoAgent({c.input: c.expected_output for c in cases})
    result = run_eval(cases, agent, [ExactMatchScorer()])

    assert result.scores["exact_match"].mean == 1.0
    assert result.pass_rate == 1.0


def test_result_serializes_with_readme_core_shape(tmp_path):
    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    result = run_eval(cases, StubAgent(), [ExactMatchScorer()])

    out = tmp_path / "results" / "run.json"
    result.save(out)

    import json

    data = json.loads(out.read_text(encoding="utf-8"))
    # README core shape, plus production extras
    assert {"run_id", "timestamp", "scores", "pass_rate"} <= set(data)
    assert set(data["scores"]["exact_match"]) == {"mean", "per_case"}
    assert len(data["cases"]) == 60
    assert data["cases"][0]["latency_ms"] is not None


def test_requires_at_least_one_scorer():
    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    with pytest.raises(ValueError, match="at least one scorer"):
        run_eval(cases, StubAgent(), [])


# --- failure isolation ---


class ExplodingAgent:
    """Crashes on q2, answers 'Paris' otherwise."""

    def run(self, input: str) -> AgentOutput:
        if "2 + 2" in input:
            raise RuntimeError("agent blew up")
        return AgentOutput(output="Paris")


def test_one_crashing_case_does_not_kill_the_run():
    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    result = run_eval(cases, ExplodingAgent(), [ExactMatchScorer()])

    assert result.error_count == 1
    errored = [c for c in result.case_results if c.error]
    assert len(errored) == 1
    assert "agent blew up" in errored[0].error
    assert errored[0].scores == {"exact_match": 0.0}
    # the other cases still scored normally
    assert result.scores["exact_match"].per_case["q1"] == 1.0


def test_bad_scorer_is_isolated_as_case_error():
    class BadScorer:
        name = "bad"

        def score(self, expected: str, actual: str) -> float:
            return 2.0

    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    result = run_eval(cases, StubAgent(), [BadScorer()])
    assert result.error_count == len(cases)
    assert all("outside" in c.error for c in result.case_results)


# --- concurrency ---


class SlowAgent:
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def run(self, input: str) -> AgentOutput:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.05)
        with self.lock:
            self.active -= 1
        return AgentOutput(output="Paris")


def test_concurrency_runs_cases_in_parallel_with_same_results():
    cases = load_dataset("datasets/examples/simple_qa.jsonl")

    serial = run_eval(cases, StubAgent(), [ExactMatchScorer()], concurrency=1)
    agent = SlowAgent()
    parallel = run_eval(cases, agent, [ExactMatchScorer()], concurrency=5)

    assert agent.peak > 1  # actually overlapped
    assert parallel.scores["exact_match"].per_case == serial.scores["exact_match"].per_case
    # order preserved
    assert [c.case_id for c in parallel.case_results] == [c.id for c in cases]


def test_invalid_concurrency_rejected():
    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    with pytest.raises(ValueError, match="concurrency"):
        run_eval(cases, StubAgent(), [ExactMatchScorer()], concurrency=0)


# --- tag filtering ---


def test_filter_by_tags():
    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    math_only = filter_by_tags(cases, ["math"])
    assert math_only, "expected math-tagged cases"
    assert all("math" in c.tags for c in math_only)
    assert "q2" in {c.id for c in math_only}
    assert filter_by_tags(cases, ["nonexistent"]) == []
    both = filter_by_tags(cases, ["math", "geography"])
    assert len(both) == len(math_only) + len(filter_by_tags(cases, ["geography"]))
    assert all({"math", "geography"} & set(c.tags) for c in both)
