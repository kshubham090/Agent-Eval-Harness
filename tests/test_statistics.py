"""Numerical checks and protocol controls for trustworthy saved results."""
from copy import deepcopy

import pytest

from harness.results import CaseResult, aggregate, flatten_metrics, summarize_runs


def case(case_id, score=1.0, **kwargs):
    return CaseResult(case_id, f"input {case_id}", "expected", "answer", {"quality": score}, **kwargs)


def test_wilson_reference_interval_matches_known_values_and_states_assumptions():
    result = aggregate([case(str(i), float(i < 80)) for i in range(100)])
    interval = result.summary["pass_rate_ci95"]
    assert interval["lower"] == pytest.approx(0.7111708344)
    assert interval["upper"] == pytest.approx(0.8666330667)
    assert interval["method"] == "wilson"
    assert interval["confidence"] == 0.95
    assert "independent representative" in interval["assumptions"]
    assert "does not measure stochastic-agent uncertainty" in interval["assumptions"]


@pytest.mark.parametrize("score", [0.0, 1.0])
def test_perfect_and_zero_pass_rates_do_not_have_zero_width_intervals(score):
    interval = aggregate([case("one", score)]).summary["pass_rate_ci95"]
    assert 0 <= interval["lower"] < interval["upper"] <= 1
    assert interval["upper"] - interval["lower"] > 0.79


def test_counts_include_errors_and_zero_threshold_never_passes_an_error():
    source = case("error", 1.0, error="scorer failed", trajectory_score=1.0)
    result = aggregate([case("ok", 0.0), source], pass_threshold=0.0)
    assert result.pass_rate == 0.5
    assert result.summary["case_count"] == 2
    assert result.summary["passed_count"] == 1
    assert result.summary["failed_count"] == 1
    assert result.summary["error_count"] == 1
    assert result.summary["error_rate"] == 0.5
    assert result.scores["quality"].mean == 0
    assert result.trajectory_score.mean == 0
    assert result.case_results[1].scores["quality"] == 0
    assert source.scores["quality"] == 1  # don't mutate caller-owned records


def test_latency_percentiles_are_interpolated_and_missing_values_are_explicit():
    cases = [case(str(i), latency_ms=value, agent_latency_ms=value / 2)
             for i, value in enumerate([10, 20, 30, 40])]
    cases.append(case("missing"))
    summary = aggregate(cases).summary
    latency = summary["latency_ms"]
    assert latency["count"] == 4
    assert latency["missing_count"] == 1
    assert latency["mean"] == 25
    assert latency["p50"] == 25
    assert latency["p95"] == pytest.approx(38.5)
    assert latency["p99"] == pytest.approx(39.7)
    assert summary["agent_latency_ms"]["p95"] == pytest.approx(19.25)


def test_reported_cost_and_tokens_do_not_treat_missing_as_free_or_double_count():
    cases = [
        case("a", usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}, cost_usd=0.03),
        case("b", usage={"input_tokens": 7, "output_tokens": 3}, cost_usd=0),
        case("c", usage={"input_tokens": 4}),
        case("d"),
        case("e", usage={"prompt_tokens": 2, "completion_tokens": 1}),
    ]
    summary = aggregate(cases).summary
    assert summary["cost_usd"] == {"reported_total": 0.03, "reported_count": 2, "missing_count": 3}
    assert summary["tokens"] == {"reported_total": 28, "reported_count": 3, "missing_count": 2}
    assert summary["usage"]["input_tokens"] == {
        "reported_total": 21, "reported_count": 3, "missing_count": 2,
    }
    missing = aggregate([case("no telemetry")]).summary
    assert missing["tokens"]["reported_total"] is None
    assert missing["cost_usd"]["reported_total"] is None
    assert missing["latency_ms"]["p95"] is None


def test_tag_summaries_include_overlapping_cases_once_per_tag():
    result = aggregate([
        case("a", tags=["math", "easy", "math"]),
        case("b", 0.0, tags=["math"], error="failed"),
        case("c", tags=["easy"]),
    ])
    by_tag = result.summary["by_tag"]
    assert by_tag["math"]["case_count"] == 2
    assert by_tag["math"]["pass_rate"] == 0.5
    assert by_tag["math"]["error_count"] == 1
    assert by_tag["easy"]["case_count"] == 2
    assert by_tag["easy"]["scores"]["quality"] == 1


def test_scoring_protocol_is_recorded_without_mutating_supplied_metadata():
    metadata = {"agent": "my-agent", "pass_threshold": 0.99}
    result = aggregate([case("a")], pass_threshold=0.7, metadata=metadata)
    assert result.metadata["pass_threshold"] == 0.7
    assert result.metadata["pass_rule"] == "mean_of_scorers_and_optional_trajectory"
    assert metadata == {"agent": "my-agent", "pass_threshold": 0.99}
    assert result.to_dict()["schema_version"] == 3


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), -0.1, 1.1, True, "0.5"])
def test_nonfinite_or_invalid_scores_cannot_enter_aggregates_or_saved_metrics(value):
    with pytest.raises(ValueError):
        aggregate([case("bad", value)])
    with pytest.raises(ValueError):
        flatten_metrics({"scores": {}, "pass_rate": value})


@pytest.mark.parametrize("field,value", [
    ("latency_ms", -1), ("agent_latency_ms", float("nan")),
    ("cost_usd", float("inf")), ("usage", {"total_tokens": -1}),
])
def test_invalid_measurements_are_rejected(field, value):
    with pytest.raises(ValueError):
        aggregate([case("bad", **{field: value})])


@pytest.mark.parametrize("bad_cases", [[], [case("same"), case("same")], [case(" ")]])
def test_case_identity_validation(bad_cases):
    with pytest.raises(ValueError):
        aggregate(bad_cases)


def test_mismatched_scorer_sets_are_rejected_before_aggregation():
    bad = case("b")
    bad.scores = {"another": 1.0}
    with pytest.raises(ValueError, match="different scorer"):
        aggregate([case("a"), bad])


def run():
    return aggregate([case("a"), case("b", 0.0)], dataset_sha="same-dataset").to_dict()


@pytest.mark.parametrize("mutation,match", [
    (lambda result: result.update(dataset_sha="another-dataset"), "dataset fingerprints"),
    (lambda result: result.pop("dataset_sha"), "dataset fingerprints"),
    (lambda result: result["metadata"].update(pass_threshold=0.9), "scoring protocol"),
    (lambda result: result["metadata"].pop("pass_threshold"), "scoring protocol"),
    (lambda result: result["cases"][0].update(expected_output="different"), "case identities"),
    (lambda result: result["cases"][0].update(tags=["different"]), "case identities"),
    (lambda result: result["scores"].update(extra={"mean": 1, "per_case": {"a": 1, "b": 1}}), "metric sets"),
])
def test_different_protocols_or_cases_cannot_be_pooled(mutation, match):
    first = run()
    changed = deepcopy(first)
    mutation(changed)
    with pytest.raises(ValueError, match=match):
        summarize_runs([first, changed])


def test_expected_trajectory_changes_are_not_pooled():
    first = aggregate([case("a", expected_trajectory=["search"], trajectory_score=1.0)]).to_dict()
    changed = deepcopy(first)
    changed["cases"][0]["expected_trajectory"] = ["delete"]
    with pytest.raises(ValueError, match="case identities"):
        summarize_runs([first, changed])


def test_case_order_is_not_a_new_identity_and_runs_remain_separate_sampling_units():
    first = run()
    second = deepcopy(first)
    second["cases"].reverse()
    second["scores"]["quality"]["mean"] = 0.7
    summary = summarize_runs([first, second])
    assert summary["run_count"] == 2
    assert summary["mean"]["scorer:quality"] == pytest.approx(0.6)
    assert summary["std"]["scorer:quality"] == pytest.approx(0.1414213562)
    assert summary["statistics"]["unit"] == "full evaluation run"
    assert "not pooled" in summary["statistics"]["note"]


def test_nested_multi_run_summaries_are_rejected():
    with pytest.raises(ValueError, match="individual runs"):
        summarize_runs([summarize_runs([run()])])


def test_inconsistent_case_coverage_cannot_be_hidden_by_matching_means():
    changed = run()
    changed["scores"]["quality"]["per_case"].pop("a")
    with pytest.raises(ValueError, match="inconsistent per-case"):
        summarize_runs([changed])


def test_saved_multi_run_without_any_runs_is_not_comparable():
    from harness.baseline import compare_to_baseline

    fake = {"type": "multi_run", "run_count": 0, "runs": [],
            "mean": {"pass_rate": 1.0, "scorer:quality": 1.0},
            "std": {"pass_rate": 0.0, "scorer:quality": 0.0}}
    with pytest.raises(ValueError, match="positive run_count"):
        compare_to_baseline(fake, run())


@pytest.mark.parametrize("mutation,match", [
    (lambda summary: summary.update(run_count=2.0), "run_count"),
    (lambda summary: summary.update(run_count=3), "run_count"),
    (lambda summary: summary["mean"].update(pass_rate=1.0), "mean"),
    (lambda summary: summary["std"].update(pass_rate=float("nan")), "std"),
    (lambda summary: summary["std"].update(pass_rate=0.2), "std"),
    (lambda summary: summary["std"].pop("pass_rate"), "std"),
    (lambda summary: summary.update(dataset_sha="wrong"), "fingerprint"),
    (lambda summary: summary.update(metadata={"pass_threshold": 0.01}), "protocol"),
    (lambda summary: summary["runs"][1].update(dataset_sha="wrong"), "fingerprints"),
])
def test_saved_multi_run_metadata_and_statistics_must_match_its_runs(mutation, match):
    summary = summarize_runs([run(), run()])
    mutation(summary)
    with pytest.raises(ValueError, match=match):
        flatten_metrics(summary)


def test_legacy_trajectory_case_selection_cannot_change_between_repeated_runs():
    base = {"scores": {"quality": {"mean": 0.5, "per_case": {"a": 0.0, "b": 1.0}}}, "pass_rate": 0.5}
    first = {**base, "trajectory_score": {"mean": 1.0, "per_case": {"a": 1.0}}}
    second = {**base, "trajectory_score": {"mean": 1.0, "per_case": {"b": 1.0}}}
    with pytest.raises(ValueError, match="case identities"):
        summarize_runs([first, second])


def test_out_of_range_arbitrary_precision_score_is_a_value_error():
    with pytest.raises(ValueError, match="outside"):
        flatten_metrics({"pass_rate": 10 ** 1000})
