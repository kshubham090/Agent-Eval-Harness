import pytest

from harness.trajectory import lcs_length, score_trajectory


def test_identical_trajectories_score_one():
    steps = ["search_flights", "select_cheapest", "confirm_booking"]
    assert score_trajectory(steps, steps) == 1.0


def test_completely_different_trajectories_score_zero():
    assert score_trajectory(["a", "b"], ["x", "y"]) == 0.0


def test_missing_step_gets_partial_credit():
    expected = ["search_flights", "select_cheapest", "confirm_booking"]
    actual = ["search_flights", "confirm_booking"]
    assert score_trajectory(expected, actual) == pytest.approx(2 / 3)


def test_extra_steps_are_penalized():
    expected = ["search", "book"]
    actual = ["search", "check_weather", "browse_hotels", "book"]
    assert score_trajectory(expected, actual) == pytest.approx(2 / 4)


def test_order_matters():
    expected = ["a", "b", "c"]
    reversed_actual = ["c", "b", "a"]
    assert score_trajectory(expected, reversed_actual) == pytest.approx(1 / 3)


def test_empty_vs_empty_is_perfect():
    assert score_trajectory([], []) == 1.0


def test_empty_actual_scores_zero():
    assert score_trajectory(["a"], []) == 0.0
    assert score_trajectory([], ["a"]) == 0.0


def test_lcs_length_basic():
    assert lcs_length(["a", "b", "c", "d"], ["b", "d"]) == 2
    assert lcs_length(["a"], ["a"]) == 1
    assert lcs_length(["a"], ["b"]) == 0


def test_eval_runner_scores_trajectories():
    from harness.dataset import load_dataset
    from harness.eval_runner import run_eval
    from harness.runner import AgentOutput
    from harness.scorers import ExactMatchScorer

    class PerfectTrajectoryAgent:
        def __init__(self, cases):
            self.by_input = {c.input: c for c in cases}

        def run(self, input: str) -> AgentOutput:
            case = self.by_input[input]
            return AgentOutput(output=case.expected_output, trajectory=list(case.expected_trajectory))

    cases = load_dataset("datasets/examples/tool_calling.jsonl")
    result = run_eval(cases, PerfectTrajectoryAgent(cases), [ExactMatchScorer()])

    assert result.trajectory_score is not None
    assert result.trajectory_score.mean == 1.0
    assert result.pass_rate == 1.0


def test_eval_runner_scores_missing_trajectory_as_zero():
    from harness.dataset import load_dataset
    from harness.eval_runner import run_eval
    from harness.runner import AgentOutput
    from harness.scorers import ExactMatchScorer

    class NoTrajectoryAgent:
        def run(self, input: str) -> AgentOutput:
            return AgentOutput(output="whatever", trajectory=None)

    cases = load_dataset("datasets/examples/tool_calling.jsonl")
    result = run_eval(cases, NoTrajectoryAgent(), [ExactMatchScorer()])

    assert result.trajectory_score.mean == 0.0
