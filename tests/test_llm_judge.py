import pytest

from harness.scorers import LLMJudgeScorer, Scorer
from harness.scorers.llm_judge import JudgeError, parse_judge_response


def make_judge(response: str) -> LLMJudgeScorer:
    return LLMJudgeScorer(complete_fn=lambda prompt: response)


def test_judge_parses_clean_json_score():
    judge = make_judge('{"score": 0.9, "reasoning": "equivalent meaning"}')
    assert judge.score("Paris", "The capital is Paris") == 0.9


def test_judge_tolerates_prose_around_json():
    raw = 'Here is my grade:\n{"score": 0.5, "reasoning": "partial"}\nDone.'
    assert parse_judge_response(raw) == 0.5


def test_judge_accepts_integer_score():
    assert parse_judge_response('{"score": 1, "reasoning": "exact"}') == 1.0


def test_judge_clamps_out_of_range_scores():
    assert parse_judge_response('{"score": 1.7, "reasoning": "overshoot"}') == 1.0
    assert parse_judge_response('{"score": -0.2, "reasoning": "undershoot"}') == 0.0


def test_judge_rejects_response_without_json():
    with pytest.raises(JudgeError, match="no JSON object"):
        parse_judge_response("I think it deserves a 0.8")


def test_judge_rejects_json_without_numeric_score():
    with pytest.raises(JudgeError, match="numeric 'score'"):
        parse_judge_response('{"score": "high", "reasoning": "..."}')


@pytest.mark.parametrize("raw", [
    '{"score": true}', '{"score": false}', '{"score": null}',
    '{"score": NaN}', '{"score": Infinity}', '{"score": -Infinity}',
    '{"score": 1e309}', '{"score": -1e309}',
    '{"score": 0.1, "score": 1.0}',
    '[]', 'null', None, 1,
    '{"score": ' + '9' * 400 + '}',
])
def test_judge_rejects_invalid_scores_instead_of_awarding_a_pass(raw):
    with pytest.raises(JudgeError):
        parse_judge_response(raw)


def test_invalid_judge_response_is_isolated_as_failed_case():
    from harness.dataset import EvalCase
    from harness.eval_runner import run_eval
    from harness.runner import AgentOutput

    class Agent:
        def run(self, input):
            return AgentOutput("wrong answer")

    result = run_eval([EvalCase("q1", "question", "correct answer")], Agent(), [make_judge('{"score": NaN}')])
    assert result.error_count == 1
    assert result.pass_rate == 0.0
    assert result.case_results[0].scores["llm_judge"] == 0.0


def test_judge_errors_do_not_echo_provider_response():
    with pytest.raises(JudgeError) as error:
        parse_judge_response("private-provider-response")
    assert "private-provider-response" not in str(error.value)


def test_judge_prompt_contains_expected_and_actual():
    seen = {}

    def spy(prompt: str) -> str:
        seen["prompt"] = prompt
        return '{"score": 1.0, "reasoning": "ok"}'

    LLMJudgeScorer(complete_fn=spy).score("EXPECTED_TEXT", "ACTUAL_TEXT")
    assert "EXPECTED_TEXT" in seen["prompt"]
    assert "ACTUAL_TEXT" in seen["prompt"]


def test_judge_without_backend_raises_helpful_error(monkeypatch):
    try:
        import anthropic  # noqa: F401

        pytest.skip("anthropic is installed")
    except ImportError:
        pass
    with pytest.raises(ImportError, match="pip install"):
        LLMJudgeScorer().score("a", "b")


def test_judge_satisfies_scorer_protocol():
    assert isinstance(make_judge('{"score": 1.0}'), Scorer)
