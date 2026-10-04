"""Verify benchmark claims and evidence shape without speed assertions."""

import json
from pathlib import Path

import pytest

from scripts.benchmark import (
    ArithmeticRunner,
    BenchmarkConfig,
    cases_sha,
    distribution,
    generate_cases,
    main,
    percentile_nearest_rank,
    render_markdown,
    run_benchmark,
    verify_integrity,
)


def test_seeded_cases_are_balanced_and_runner_solves_unseen_prompt():
    cases = generate_cases(20, 1729)
    assert cases == generate_cases(20, 1729)
    assert cases_sha(cases) != cases_sha(generate_cases(20, 1730))
    assert all(ArithmeticRunner().run(case.input).output == case.expected_output for case in cases)
    # Not drawn from the fixture; the runner parses and calculates its input.
    assert ArithmeticRunner().run(
        "Evaluate the integer expression -1001 // 3. Use floor division for //. Reply with only the integer."
    ).output == "-334"
    for operation in ("+", "-", "*", "//"):
        assert sum(f"operation:{operation}" in case.tags for case in cases) == 5


def test_perturbations_exercise_gate_and_failure_isolation():
    integrity = verify_integrity(generate_cases(12, 7))
    assert all(integrity["checks"].values())
    assert integrity["correct_pass_rate"] == 1.0
    assert integrity["degraded_pass_rate"] == 0.75
    assert integrity["injected_error_count"] == 3
    assert integrity["injected_error_pass_rate"] == 0.75
    assert {delta["metric"] for delta in integrity["regressions"]} >= {"pass_rate", "scorer:exact_match"}


def test_empirical_summary_uses_nearest_rank_and_sample_spread():
    summary = distribution([5, 1, 4, 2, 3])
    assert summary["median"] == 3
    assert summary["p95"] == 5
    assert summary["min"] == 1
    assert summary["max"] == 5
    assert summary["stdev"] == pytest.approx(1.5811388300841898)
    assert distribution([4])["stdev"] == 0
    assert percentile_nearest_rank(list(range(1, 101)), 95) == 95
    with pytest.raises(ValueError, match="empty"):
        distribution([])


def test_small_benchmark_records_raw_samples_provenance_and_verification():
    config = BenchmarkConfig(cases=8, overhead_cases=16, latency_ms=0, repetitions=2, warmups=1)
    result = run_benchmark(config, command="python scripts/benchmark.py --test")
    assert result["passed"] is True
    assert result["benchmark_kind"] == "synthetic-harness-operations"
    assert len(result["suite_sha256"]) == 64
    assert len(result["provenance"]["source"]["sha256"]) == 64
    assert result["provenance"]["command"] == "python scripts/benchmark.py --test"
    assert "started_at_utc" in result["provenance"]
    assert "python_version" in result["provenance"]
    assert "dirty" in result["provenance"]["git"]
    assert result["concurrency"][0]["speedup_vs_serial"] == 1.0
    for row in result["concurrency"]:
        assert len(row["samples"]) == 2
        assert len(row["warmup_samples_excluded"]) == 1
        assert row["wall_ms"]["sample_count"] == 2
        assert row["wall_ms"]["median"] == pytest.approx(sum(s["wall_ms"] for s in row["samples"]) / 2)
        assert all(len(sample["case_latency_ms"]) == 8 and sample["verified"] for sample in row["samples"])
    overhead = result["overhead"]
    assert len(overhead["samples"]) == 2
    assert len(overhead["warmup_samples_excluded"]) == 1
    for sample in overhead["samples"]:
        assert sample["incremental_us_per_case"] == pytest.approx(
            (sample["harness_ms"] - sample["direct_loop_ms"]) * 1000 / 16
        )
    markdown = render_markdown(result)
    assert "not a model leaderboard" in markdown
    assert "Overall verification: **PASS**" in markdown
    assert "not estimates of model accuracy or CI reliability" in markdown
    json.dumps(result, allow_nan=False)


def test_script_writes_evidence_and_markdown(tmp_path):
    output = tmp_path / "results" / "run.json"
    markdown = tmp_path / "results" / "run.md"
    exit_code = main([
        "--output", str(output), "--markdown", str(markdown), "--cases", "4",
        "--overhead-cases", "4", "--latency-ms", "0", "--repetitions", "1", "--warmups", "0",
    ])
    assert exit_code == 0
    assert json.loads(output.read_text())["passed"] is True
    assert markdown.read_text().startswith("# Reproducible harness benchmark")


@pytest.mark.parametrize("kwargs", [
    {"cases": 0}, {"cases": 5}, {"overhead_cases": 1}, {"latency_ms": -1},
    {"latency_ms": float("nan")}, {"latency_ms": float("inf")},
    {"repetitions": 0}, {"warmups": -1}, {"concurrency": (2, 4)}, {"concurrency": (1, 1)},
])
def test_invalid_benchmark_settings_rejected(kwargs):
    with pytest.raises(ValueError):
        BenchmarkConfig(**kwargs).validate()


def test_authored_smoke_references_reproduce_and_cover_four_categories():
    from benchmarks.generate_smoke import build_cases
    from harness.dataset import load_dataset

    generated = build_cases()
    published = load_dataset(Path(__file__).resolve().parents[1] / "benchmarks" / "agent_smoke.jsonl")
    assert len(generated) == len(published) == 16
    assert {case.tags[-1] for case in published} == {"code-tracing", "data-transform", "algorithms", "constraints"}
    for record, case in zip(generated, published):
        assert record["id"] == case.id
        assert record["input"] == case.input
        assert record["expected_output"] == case.expected_output
        json.loads(case.expected_output)
    answers = {case.id: json.loads(case.expected_output) for case in published}
    assert answers["trace-alias"] == [[1, 7], [2]]
    assert answers["algorithm-shortest-path"] == ["A", "B", "C", "F"]
    assert answers["algorithm-min-coins"] == 2
    assert answers["constraint-utc"] == "2027-01-01T01:20:00Z"
