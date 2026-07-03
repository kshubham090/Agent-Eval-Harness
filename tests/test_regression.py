import json

import pytest

from harness.baseline import compare_to_baseline, load_baseline, save_baseline


def result_dict(exact_mean=0.8, pass_rate=0.8, trajectory_mean=None):
    d = {
        "run_id": "test",
        "timestamp": "2026-07-03T00:00:00+00:00",
        "scores": {"exact_match": {"mean": exact_mean, "per_case": {}}},
        "pass_rate": pass_rate,
    }
    if trajectory_mean is not None:
        d["trajectory_score"] = {"mean": trajectory_mean, "per_case": {}}
    return d


def test_save_and_load_roundtrip(tmp_path):
    original = result_dict()
    save_baseline("v1", original, baselines_dir=tmp_path)
    assert load_baseline("v1", baselines_dir=tmp_path) == original


def test_load_missing_baseline_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="no baseline named"):
        load_baseline("nope", baselines_dir=tmp_path)


def test_identical_results_pass():
    comparison = compare_to_baseline(result_dict(), result_dict())
    assert comparison.passed
    assert comparison.regressions == []


def test_small_drop_within_threshold_passes():
    comparison = compare_to_baseline(
        current=result_dict(exact_mean=0.77, pass_rate=0.77),
        baseline=result_dict(exact_mean=0.8, pass_rate=0.8),
        threshold=0.05,
    )
    assert comparison.passed


def test_drop_beyond_threshold_is_regression():
    comparison = compare_to_baseline(
        current=result_dict(exact_mean=0.7, pass_rate=0.8),
        baseline=result_dict(exact_mean=0.8, pass_rate=0.8),
        threshold=0.05,
    )
    assert not comparison.passed
    assert [r.metric for r in comparison.regressions] == ["scorer:exact_match"]


def test_improvement_is_not_a_regression():
    comparison = compare_to_baseline(
        current=result_dict(exact_mean=0.95, pass_rate=1.0),
        baseline=result_dict(exact_mean=0.8, pass_rate=0.8),
    )
    assert comparison.passed


def test_trajectory_mean_is_compared():
    comparison = compare_to_baseline(
        current=result_dict(trajectory_mean=0.5),
        baseline=result_dict(trajectory_mean=0.9),
    )
    assert [r.metric for r in comparison.regressions] == ["trajectory"]


def test_missing_metric_in_current_raises():
    baseline = result_dict()
    baseline["scores"]["llm_judge"] = {"mean": 0.9, "per_case": {}}
    with pytest.raises(ValueError, match="missing from current run"):
        compare_to_baseline(result_dict(), baseline)


# --- dataset fingerprint ---


def test_dataset_change_blocks_comparison():
    current, baseline = result_dict(), result_dict()
    current["dataset_sha"], baseline["dataset_sha"] = "aaa", "bbb"
    with pytest.raises(ValueError, match="dataset changed"):
        compare_to_baseline(current, baseline)


def test_dataset_change_can_be_overridden():
    current, baseline = result_dict(), result_dict()
    current["dataset_sha"], baseline["dataset_sha"] = "aaa", "bbb"
    comparison = compare_to_baseline(current, baseline, ignore_dataset_mismatch=True)
    assert comparison.passed


def test_missing_sha_on_either_side_skips_the_check():
    current, baseline = result_dict(), result_dict()
    current["dataset_sha"] = "aaa"  # baseline predates fingerprinting
    assert compare_to_baseline(current, baseline).passed


# --- multi-run statistics ---


def test_summarize_runs_computes_mean_and_std():
    from harness.results import summarize_runs

    runs = [result_dict(exact_mean=0.7, pass_rate=0.7), result_dict(exact_mean=0.9, pass_rate=0.9)]
    summary = summarize_runs(runs)

    assert summary["type"] == "multi_run"
    assert summary["run_count"] == 2
    assert summary["mean"]["scorer:exact_match"] == pytest.approx(0.8)
    assert summary["std"]["scorer:exact_match"] == pytest.approx(0.1414, abs=1e-3)


def test_identical_runs_have_zero_std():
    from harness.results import summarize_runs

    summary = summarize_runs([result_dict(), result_dict(), result_dict()])
    assert all(v == 0.0 for v in summary["std"].values())


def test_multi_run_summary_compares_against_single_run_baseline():
    from harness.results import summarize_runs

    summary = summarize_runs([result_dict(exact_mean=0.6, pass_rate=0.6),
                              result_dict(exact_mean=0.6, pass_rate=0.6)])
    comparison = compare_to_baseline(summary, result_dict(exact_mean=0.8, pass_rate=0.8))
    assert not comparison.passed
    assert {r.metric for r in comparison.regressions} == {"scorer:exact_match", "pass_rate"}


# --- CLI end-to-end: the README verification scenario ---


DEGRADED_AGENT = '''
from harness.runner import AgentOutput

class DegradedAgent:
    def run(self, input: str) -> AgentOutput:
        return AgentOutput(output="London")

def get_agent():
    return DegradedAgent()
'''


def test_cli_gate_blocks_degraded_agent(tmp_path):
    """Stub agent -> baseline -> degraded agent -> gate exits 1."""
    from click.testing import CliRunner

    from harness.cli import cli

    runner = CliRunner()
    result_file = tmp_path / "run.json"
    baselines = tmp_path / "baselines"

    # 1. Eval the stub agent and save its result as the baseline
    run1 = runner.invoke(cli, [
        "eval", "--dataset", "datasets/examples/simple_qa.jsonl",
        "--agent", "examples/stub_agent.py",
        "--scorers", "exact", "--output", str(result_file),
    ])
    assert run1.exit_code == 0, run1.output

    save = runner.invoke(cli, [
        "baseline", "save", "--name", "v1",
        "--result", str(result_file), "--baselines-dir", str(baselines),
    ])
    assert save.exit_code == 0, save.output

    # 2. Same agent vs baseline: gate passes
    run2 = runner.invoke(cli, [
        "eval", "--dataset", "datasets/examples/simple_qa.jsonl",
        "--agent", "examples/stub_agent.py",
        "--scorers", "exact",
        "--compare-baseline", "v1", "--baselines-dir", str(baselines),
    ])
    assert run2.exit_code == 0, run2.output
    assert "PASS" in run2.output

    # 3. Degraded agent (never right) vs baseline: gate exits 1
    degraded = tmp_path / "degraded_agent.py"
    degraded.write_text(DEGRADED_AGENT, encoding="utf-8")
    run3 = runner.invoke(cli, [
        "eval", "--dataset", "datasets/examples/simple_qa.jsonl",
        "--agent", str(degraded),
        "--scorers", "exact",
        "--compare-baseline", "v1", "--baselines-dir", str(baselines),
    ])
    assert run3.exit_code == 1, run3.output
    assert "REGRESSION" in run3.output
