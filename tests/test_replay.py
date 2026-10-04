"""Saved evidence must not become a fresh inference or hide a failed attempt."""
from copy import deepcopy
import hashlib
import json

import pytest

from harness.dataset import EvalCase
from harness.eval_runner import describe_scorers, run_eval
from harness.replay import load_recording, rescore_result, validate_recording
from harness.results import summarize_runs
from harness.runner import AgentOutput
from harness.scorers.exact import ExactMatchScorer
from harness.scorers.regex_scorer import RegexScorer
from harness.scorers.structured import ContainsScorer


class SavedAgent:
    calls = 0

    def run(self, prompt):
        self.calls += 1
        return AgentOutput(
            output="Answer: Paris", trajectory=["search"],
            usage={"input_tokens": 13, "output_tokens": 4}, cost_usd=0.012,
            metadata={"model": "fixture", "details": {"turn": 1}},
            events=[{"type": "tool", "name": "search", "result": {"city": "Paris"}}],
        )


def recording(agent=None):
    return run_eval(
        [EvalCase("city", "same prompt", "Paris", expected_trajectory=("search",), tags=("qa",)),
         EvalCase("country", "same prompt", "France", tags=("qa",))],
        agent or SavedAgent(), [ExactMatchScorer()], pass_threshold=0.8,
    ).to_dict()


def test_rescore_preserves_saved_evidence_and_never_calls_agent():
    agent = SavedAgent()
    original = recording(agent)
    snapshot = deepcopy(original)
    rescored = rescore_result(original, [ContainsScorer()], concurrency=2)
    assert agent.calls == 2
    assert rescored["run_id"] != original["run_id"]
    assert original == snapshot
    assert rescored["schema_version"] == 3
    assert rescored["scores"]["contains"]["per_case"] == {"city": 1, "country": 0}
    assert rescored["pass_rate"] == 0.5
    assert rescored["metadata"]["pass_threshold"] == 0.8
    assert rescored["metadata"]["evaluation_mode"] == "rescore"
    assert rescored["metadata"]["rescore"]["agent_calls"] == 0
    assert rescored["metadata"]["rescore"]["harness_version"]
    assert rescored["metadata"]["rescore"]["python"]
    expected_hash = hashlib.sha256(json.dumps(original, sort_keys=True, ensure_ascii=False,
                                              separators=(",", ":")).encode()).hexdigest()
    assert rescored["metadata"]["rescore"]["source_sha256"] == expected_hash
    for before, after in zip(original["cases"], rescored["cases"], strict=True):
        for key in ("id", "input", "expected_output", "output", "trajectory", "expected_trajectory",
                    "events", "usage", "cost_usd", "latency_ms", "agent_latency_ms", "tags"):
            assert after[key] == before[key]
        assert after["rescore_latency_ms"] >= 0
    assert rescored["summary"]["rescore_latency_ms"]["count"] == 2
    assert rescored["summary"]["cost_usd"] == original["summary"]["cost_usd"]
    validate_recording(rescored)


def test_multi_run_rescore_keeps_runs_and_case_identity():
    source = summarize_runs([recording(), recording()])
    result = rescore_result(source, [ContainsScorer()], pass_threshold=0.9)
    assert result["type"] == "multi_run"
    assert result["run_count"] == 2
    assert result["mean"]["pass_rate"] == 0.5
    for before, after in zip(source["runs"], result["runs"], strict=True):
        assert after["metadata"]["rescore"]["source_run_id"] == before["run_id"]
        assert after["metadata"]["pass_threshold"] == 0.9
        assert [case["id"] for case in after["cases"]] == ["city", "country"]
    validate_recording(result)


def test_second_rescore_retains_history():
    first = rescore_result(recording(), [ContainsScorer()])
    second = rescore_result(first, [ExactMatchScorer()])
    assert second["metadata"]["rescore_history"] == [first["metadata"]["rescore"]]
    assert second["metadata"]["rescore"]["source_run_id"] == first["run_id"]
    assert second["cases"][0]["latency_ms"] == first["cases"][0]["latency_ms"]


def test_custom_text_scorer_named_tests_supports_successive_rescores():
    class TextTestsScorer:
        name = "tests"

        def score(self, expected, actual):
            return float(expected in actual)

    first = rescore_result(recording(), [TextTestsScorer()])
    second = rescore_result(first, [ExactMatchScorer()])
    assert first["scores"]["tests"]["per_case"] == {"city": 1, "country": 0}
    assert second["metadata"]["rescore"]["source_run_id"] == first["run_id"]
    assert second["metadata"]["rescore_history"] == [first["metadata"]["rescore"]]


class BrokenScorer:
    name = "broken"

    def score(self, expected, actual):
        raise RuntimeError("grader unavailable")


def test_only_scoring_errors_can_be_retried():
    agent = SavedAgent()
    original = run_eval([EvalCase("city", "prompt", "Paris")], agent, [BrokenScorer()]).to_dict()
    assert original["cases"][0]["error_stage"] == "scorer"
    assert original["cases"][0]["events"][0]["name"] == "search"
    rescored = rescore_result(original, [ContainsScorer()])
    assert rescored["pass_rate"] == 1
    assert rescored["error_count"] == 0
    assert rescored["cases"][0]["metadata"]["previous_scoring_error"]["stage"] == "scorer"
    assert agent.calls == 1


@pytest.mark.parametrize("stage", ["agent", "validation", None])
def test_non_scoring_and_unknown_legacy_errors_stay_failed_even_at_zero_threshold(stage):
    source = run_eval([EvalCase("city", "prompt", "Paris")], SavedAgent(), [BrokenScorer()]).to_dict()
    source["cases"][0]["error_stage"] = stage

    class ForbiddenScorer:
        name = "never_called"

        def score(self, expected, actual):
            pytest.fail("an unsuccessful agent attempt cannot be rescued by rescoring")

    result = rescore_result(source, [ForbiddenScorer()], pass_threshold=0)
    assert result["pass_rate"] == 0
    assert result["error_count"] == 1
    assert result["cases"][0]["error_stage"] == stage
    assert result["cases"][0]["rescore_latency_ms"] is None


def test_new_scoring_failure_stays_visible_and_preserves_telemetry():
    source = recording()
    result = rescore_result(source, [BrokenScorer()])
    assert result["error_count"] == 2
    for before, case in zip(source["cases"], result["cases"], strict=True):
        assert case["error_stage"] == "scorer"
        assert case["scores"] == {"broken": 0}
        assert case["events"] == before["events"]
        assert case["cost_usd"] == before["cost_usd"]


@pytest.mark.parametrize("mode,expected_stage", [("raise", "agent"), ("bad_output", "validation"),
                                                 ("bad_events", "validation")])
def test_eval_records_failure_stage(mode, expected_stage):
    class Agent:
        def run(self, prompt):
            if mode == "raise":
                raise RuntimeError("cannot run")
            if mode == "bad_output":
                return AgentOutput(output=object())
            return AgentOutput(output="good", events=[{"nested": {1: "bad key"}}])

    result = run_eval([EvalCase("case", "input", "expected")], Agent(), [ExactMatchScorer()])
    assert result.case_results[0].error_stage == expected_stage
    validate_recording(result.to_dict())


@pytest.mark.parametrize("events", [{}, ["tool"], [{"score": float("inf")}], [{"extra": (1, 2)}]])
def test_invalid_events_are_validation_errors(events):
    class Agent:
        def run(self, prompt):
            return AgentOutput(output="ok", events=events)

    result = run_eval([EvalCase("case", "prompt", "ok")], Agent(), [ExactMatchScorer()])
    assert result.case_results[0].error_stage == "validation"
    assert result.case_results[0].events == []


def test_legacy_v2_recordings_replay_and_rescore(tmp_path):
    source = recording()
    source["schema_version"] = 2
    for case in source["cases"]:
        for field in ("events", "error_stage", "rescore_latency_ms"):
            del case[field]
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(source))
    loaded = load_recording(path)
    assert loaded == source
    result = rescore_result(loaded, [ContainsScorer()])
    assert result["schema_version"] == 3
    assert result["cases"][0]["events"] == []


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(pass_rate=1),
    lambda r: r.update(error_count=True),
    lambda r: r.update(error_count=0.0),
    lambda r: r["summary"]["cost_usd"].update(reported_total=0),
    lambda r: r["summary"]["agent_latency_ms"].update(p95=0),
    lambda r: r["summary"]["scores"].update(exact_match=1),
    lambda r: r["scores"]["exact_match"]["per_case"].update(city=1),
    lambda r: r["cases"][0]["scores"].update(exact_match=True),
    lambda r: r["cases"][0].update(output=2),
    lambda r: r["cases"][0].update(input=None),
    lambda r: r["cases"][0].update(error_stage="agent"),
    lambda r: r["cases"][0].update(events=[{"result": float("nan")}]),
    lambda r: r["cases"][0]["usage"].update(total_tokens=-1),
    lambda r: r["cases"][0].update(cost_usd=float("inf")),
    lambda r: r["cases"][0].update(agent_latency_ms=True),
    lambda r: r["cases"][0].update(tags=["qa", "qa"]),
    lambda r: r["cases"][1].update(id="city"),
    lambda r: r["cases"][0].update(expected_trajectory=None),
    lambda r: r["metadata"].update(pass_threshold=True),
    lambda r: r["metadata"].update(pass_rule="arbitrary_rule"),
    lambda r: r["metadata"].pop("pass_threshold"),
    lambda r: r.update(schema_version=4),
    lambda r: r.update(cases=[]),
])
def test_replay_rejects_inconsistent_and_malformed_evidence(mutation):
    source = recording()
    mutation(source)
    with pytest.raises(ValueError):
        validate_recording(source)


def test_replay_rejects_nonzero_raw_error_scores_even_with_normalized_headline():
    source = run_eval([EvalCase("city", "prompt", "Paris")], SavedAgent(), [BrokenScorer()]).to_dict()
    source["cases"][0]["scores"]["broken"] = 1
    with pytest.raises(ValueError, match="scores"):
        validate_recording(source)


@pytest.mark.parametrize("fragment", ['"extra":NaN', '"extra":Infinity', '"extra":1e999',
                                     '"extra":{"a":1,"a":2}', '"schema_version":2'])
def test_load_rejects_nonfinite_and_duplicate_json_fields(tmp_path, fragment):
    source = json.dumps(recording())
    path = tmp_path / "invalid.json"
    path.write_text(source[:-1] + "," + fragment + "}")
    with pytest.raises(ValueError):
        load_recording(path)


def test_multi_run_validation_checks_raw_children_and_top_summary():
    source = summarize_runs([recording(), recording()])
    source["runs"][1]["cases"][0]["cost_usd"] = -1
    with pytest.raises(ValueError):
        validate_recording(source)
    source = summarize_runs([recording(), recording()])
    source["mean"]["pass_rate"] = 1
    with pytest.raises(ValueError):
        validate_recording(source)


@pytest.mark.parametrize("marker", ["evaluation_kind", "grader_type"])
def test_coding_recordings_can_replay_but_cannot_text_rescore(marker):
    source = recording()
    if marker == "evaluation_kind":
        source["metadata"]["evaluation_kind"] = "coding"
    else:
        source["metadata"]["grader_config"][0]["type"] = "harness.coding.held_out_tests"
    assert validate_recording(source) == source
    with pytest.raises(ValueError, match="held-out tests"):
        rescore_result(source, [ContainsScorer()])


def test_grader_config_records_settings_without_serializing_secrets():
    class Custom:
        name = "custom"
        secret = "do-not-persist"

        def evaluation_config(self):
            return {"rubric_version": 7}

        def score(self, expected, actual):
            return 1

    descriptions = describe_scorers([ExactMatchScorer(False), RegexScorer(True), Custom()])
    assert descriptions[0]["settings"] == {"case_sensitive": False}
    assert descriptions[1]["settings"] == {"full_match": True}
    assert descriptions[2]["settings"] == {"rubric_version": 7}
    assert "do-not-persist" not in json.dumps(descriptions)
    assert descriptions[0]["type"] == "harness.scorers.exact.ExactMatchScorer"


def test_mutating_replayed_data_does_not_mutate_input_recording():
    source = recording()
    validated = validate_recording(source)
    validated["cases"][0]["events"][0]["result"]["city"] = "Rome"
    assert source["cases"][0]["events"][0]["result"]["city"] == "Paris"


@pytest.mark.parametrize("kwargs", [{"concurrency": True}, {"concurrency": 0},
                                     {"pass_threshold": float("nan")}, {"scorers": []}])
def test_rescore_options_are_validated(kwargs):
    options = {"scorers": [ContainsScorer()], **kwargs}
    with pytest.raises(ValueError):
        rescore_result(recording(), **options)


@pytest.mark.parametrize("change", ["settings", "missing"])
def test_repeated_runs_reject_different_or_missing_grader_configuration(change):
    first, second = recording(), recording()
    if change == "settings":
        second["metadata"]["grader_config"][0]["settings"]["case_sensitive"] = False
    else:
        second["metadata"].pop("grader_config")
    with pytest.raises(ValueError, match="grader_config"):
        summarize_runs([first, second])


def test_saved_multi_run_cannot_misstate_grader_settings():
    from harness.results import flatten_metrics
    source = summarize_runs([recording(), recording()])
    # summarize_runs intentionally retains the first run's metadata object;
    # replace it here to mutate only the top-level claim.
    source["metadata"] = deepcopy(source["metadata"])
    source["metadata"]["grader_config"][0]["settings"]["case_sensitive"] = False
    with pytest.raises(ValueError, match="grader_config"):
        flatten_metrics(source)


def test_baseline_requires_matching_recorded_grader_settings_but_accepts_legacy():
    from harness.baseline import compare_to_baseline
    first, second = recording(), recording()
    second["metadata"]["grader_config"][0]["settings"]["case_sensitive"] = False
    with pytest.raises(ValueError, match="grader_config"):
        compare_to_baseline(second, first)
    first["metadata"].pop("grader_config")
    first["schema_version"] = 2
    assert compare_to_baseline(second, first).passed
    second["metadata"].pop("grader_config")
    second["schema_version"] = 2
    assert summarize_runs([first, second])["run_count"] == 2


def test_lazy_default_judge_configuration_is_stable_across_runs(monkeypatch):
    import sys
    from types import SimpleNamespace
    from harness.scorers.llm_judge import LLMJudgeScorer

    class Client:
        def __init__(self, **kwargs):
            self.messages = self

        def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"score":1}')])

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=Client))
    scorer = LLMJudgeScorer()
    cases = [EvalCase("city", "prompt", "Paris")]
    first = run_eval(cases, SavedAgent(), [scorer]).to_dict()
    second = run_eval(cases, SavedAgent(), [scorer]).to_dict()
    assert first["metadata"]["grader_config"] == second["metadata"]["grader_config"]
    assert summarize_runs([first, second])["run_count"] == 2


def test_lazy_default_embedding_configuration_is_stable_across_runs(monkeypatch):
    import sys
    from types import SimpleNamespace
    from harness.scorers.embedding import EmbeddingScorer

    class Transformer:
        def __init__(self, model):
            pass

        def encode(self, texts):
            return SimpleNamespace(tolist=lambda: [[1, 2] for _ in texts])

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Transformer))
    scorer = EmbeddingScorer()
    cases = [EvalCase("city", "prompt", "Paris")]
    first = run_eval(cases, SavedAgent(), [scorer]).to_dict()
    second = run_eval(cases, SavedAgent(), [scorer]).to_dict()
    assert first["metadata"]["grader_config"] == second["metadata"]["grader_config"]
    assert summarize_runs([first, second])["run_count"] == 2
