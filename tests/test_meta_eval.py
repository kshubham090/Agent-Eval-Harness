import pytest

from harness.dataset import DatasetError
from harness.meta_eval import JudgeCase, evaluate_judge, load_judge_dataset
from harness.scorers import ExactMatchScorer, LLMJudgeScorer


def cases():
    return [
        JudgeCase(id="a", expected="Paris", actual="Paris", human_score=1.0),
        JudgeCase(id="b", expected="Paris", actual="The capital is Paris", human_score=1.0),
        JudgeCase(id="c", expected="Paris", actual="London", human_score=0.0),
        JudgeCase(id="d", expected="Paris", actual="A European capital", human_score=0.5),
    ]


class OracleJudge:
    """Perfect judge: always returns the human score (for testing the math)."""

    name = "oracle"

    def __init__(self, answers: dict[tuple[str, str], float]):
        self.answers = answers

    def score(self, expected: str, actual: str) -> float:
        return self.answers[(expected, actual)]


def test_perfect_judge_has_zero_error_and_full_agreement():
    oracle = OracleJudge({(c.expected, c.actual): c.human_score for c in cases()})
    result = evaluate_judge(cases(), oracle)
    assert result.mean_absolute_error == 0.0
    assert result.agreement_rate == 1.0


def test_exact_match_is_a_bad_judge_on_paraphrases():
    # Exact match scores "The capital is Paris" as 0.0 where a human says 1.0
    # -- meta-eval is exactly how you detect that a scorer is too strict.
    result = evaluate_judge(cases(), ExactMatchScorer(), tolerance=0.25)
    assert result.mean_absolute_error > 0.3
    assert result.agreement_rate == 0.5  # right on a and c only


def test_agreement_respects_tolerance():
    always_half = OracleJudge({(c.expected, c.actual): 0.5 for c in cases()})
    strict = evaluate_judge(cases(), always_half, tolerance=0.1)
    lenient = evaluate_judge(cases(), always_half, tolerance=0.5)
    assert strict.agreement_rate == 0.25  # only the 0.5 human case
    assert lenient.agreement_rate == 1.0


def test_pairs_carry_per_case_detail():
    oracle = OracleJudge({(c.expected, c.actual): c.human_score for c in cases()})
    result = evaluate_judge(cases(), oracle)
    by_id = {p.case_id: p for p in result.pairs}
    assert by_id["c"].human == 0.0
    assert by_id["c"].judge == 0.0


def test_empty_cases_raise():
    with pytest.raises(ValueError, match="no judge cases"):
        evaluate_judge([], ExactMatchScorer())


def test_llm_judge_with_fake_backend_can_be_meta_evaled():
    # An overly generous fake judge: everything gets 1.0
    generous = LLMJudgeScorer(complete_fn=lambda p: '{"score": 1.0, "reasoning": "looks fine"}')
    result = evaluate_judge(cases(), generous, tolerance=0.25)
    assert result.agreement_rate == 0.5  # right on the two human_score=1.0 cases only


# --- loader ---


def test_loads_example_calibration_dataset():
    loaded = load_judge_dataset("datasets/examples/judge_calibration.jsonl")
    assert len(loaded) == 10
    assert all(0.0 <= c.human_score <= 1.0 for c in loaded)


@pytest.mark.parametrize(
    "bad_line, match",
    [
        ("not json", "invalid JSON"),
        ('{"expected": "x", "actual": "y", "human_score": 1.0}', "'id'"),
        ('{"id": "a", "actual": "y", "human_score": 1.0}', "'expected'"),
        ('{"id": "a", "expected": "x", "human_score": 1.0}', "'actual'"),
        ('{"id": "a", "expected": "x", "actual": "y"}', "human_score"),
        ('{"id": "a", "expected": "x", "actual": "y", "human_score": 1.5}', "human_score"),
        ('{"id": "a", "expected": "x", "actual": "y", "human_score": "high"}', "human_score"),
    ],
)
def test_loader_rejects_malformed_records(tmp_path, bad_line, match):
    path = tmp_path / "judge.jsonl"
    path.write_text(bad_line, encoding="utf-8")
    with pytest.raises(DatasetError, match=match):
        load_judge_dataset(path)


def test_loader_rejects_duplicate_ids(tmp_path):
    line = '{"id": "a", "expected": "x", "actual": "y", "human_score": 1.0}'
    path = tmp_path / "judge.jsonl"
    path.write_text(line + "\n" + line, encoding="utf-8")
    with pytest.raises(DatasetError, match="duplicate case id"):
        load_judge_dataset(path)


def test_loader_rejects_empty_dataset(tmp_path):
    path = tmp_path / "judge.jsonl"
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="empty"):
        load_judge_dataset(path)
