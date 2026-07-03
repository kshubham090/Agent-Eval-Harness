import pytest

from harness.dataset import load_dataset
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

    assert result.scores["exact_match"].mean == pytest.approx(1 / 5)
    assert result.scores["exact_match"].per_case["q1"] == 1.0
    assert result.scores["exact_match"].per_case["q2"] == 0.0
    assert result.pass_rate == pytest.approx(1 / 5)


def test_perfect_agent_scores_one():
    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    agent = EchoAgent({c.input: c.expected_output for c in cases})
    result = run_eval(cases, agent, [ExactMatchScorer()])

    assert result.scores["exact_match"].mean == 1.0
    assert result.pass_rate == 1.0


def test_result_serializes_to_readme_shape(tmp_path):
    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    result = run_eval(cases, StubAgent(), [ExactMatchScorer()])

    out = tmp_path / "results" / "run.json"
    result.save(out)

    import json

    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) == {"run_id", "timestamp", "scores", "pass_rate"}
    assert set(data["scores"]["exact_match"]) == {"mean", "per_case"}


def test_requires_at_least_one_scorer():
    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    with pytest.raises(ValueError, match="at least one scorer"):
        run_eval(cases, StubAgent(), [])


def test_rejects_out_of_range_scores():
    class BadScorer:
        name = "bad"

        def score(self, expected: str, actual: str) -> float:
            return 2.0

    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    with pytest.raises(ValueError, match="outside"):
        run_eval(cases, StubAgent(), [BadScorer()])
