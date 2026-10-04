"""Check published coding benchmark evidence is complete and reproducible."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_coding.py"
spec = importlib.util.spec_from_file_location("benchmark_coding_for_tests", MODULE_PATH)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_source_provenance_includes_pack_agent_recipe_and_script(tmp_path):
    paths = ["harness/coding.py", "harness/packs_data/demo/pack.toml", "harness/packs_data/demo/workspace/solution.py",
             "examples/coding_fixture_agent.py", "examples/Dockerfile.fixture", "scripts/benchmark_coding.py"]
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    first = benchmark.source_provenance(tmp_path)
    assert set(first["files"]) == set(paths)
    (tmp_path / paths[1]).write_text("changed manifest")
    assert benchmark.source_provenance(tmp_path)["sha256"] != first["sha256"]


def test_sanitization_removes_home_checkout_and_artifact_paths(tmp_path):
    root = tmp_path / "checkout"
    artifacts = root / "results"
    value = {"cases": [{"metadata": {"artifacts": str(artifacts / "uuid" / "case")},
                         "events": [{"path": str(root / "example.py")}]}], "home": str(Path.home() / ".thing")}
    clean = benchmark.sanitize_result(value, artifacts=artifacts, root=root)
    assert clean["cases"][0]["metadata"]["artifacts"] == "artifacts/uuid/case"
    assert clean["cases"][0]["events"][0]["path"] == "$CHECKOUT/example.py"
    assert clean["home"] == "$HOME/.thing"
    assert str(tmp_path) not in json.dumps(clean)
    assert value["cases"][0]["metadata"]["artifacts"].startswith(str(tmp_path))


def test_trial_validation_checks_cases_not_only_summary():
    good = {"pass_rate": 1.0, "error_count": 0, "cases": [
        {"error": None, "scores": {"tests": 1.0}, "metadata": {"grader_exit_code": 0}}]}
    assert benchmark._trial_valid(good, expected_pass_rate=1.0, case_count=1)
    good["cases"][0]["scores"]["tests"] = 0
    assert not benchmark._trial_valid(good, expected_pass_rate=1.0, case_count=1)


def test_distributions_use_nearest_rank_without_false_precision():
    assert benchmark.distribution([2, 1, 4])["p95"] == 4
    assert benchmark.distribution([2, 1, 4])["median"] == 2
    with pytest.raises(ValueError):
        benchmark.distribution([float("nan")])


def test_benchmark_retains_every_failed_trial_and_returns_failed_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "source_provenance", lambda: {"sha256": "a", "files": {}})
    monkeypatch.setattr(benchmark, "git_provenance", lambda: {"revision": "b", "dirty": False, "status": []})
    monkeypatch.setattr(benchmark, "docker_provenance", lambda: {"Version": "test"})
    monkeypatch.setattr(benchmark, "load_pack", lambda _: SimpleNamespace(tasks=[1, 2, 3], fingerprint="c"))
    def fail(*args, **kwargs):
        raise RuntimeError("Docker unavailable")
    monkeypatch.setattr(benchmark, "run_coding_eval", fail)
    result = benchmark.run_benchmark(image="image", repetitions=2, artifacts=tmp_path)
    assert not result["passed"]
    assert [trial["variant"] for trial in result["trials"]] == ["correct", "noop", "noop", "correct"]
    assert all(trial["error"] == "Docker unavailable" for trial in result["trials"])
    assert result["summary"]["correct"]["case_attempts"] == 0
    assert result["summary"]["correct"]["missing_agent_timing_count"] == 6
    assert "**FAIL**" in benchmark.render_markdown(result)


def test_main_writes_preflight_diagnostic_even_when_configuration_is_invalid(tmp_path):
    output, markdown = tmp_path / "result.json", tmp_path / "result.md"
    status = benchmark.main(["--repetitions", "0", "--output", str(output), "--markdown", str(markdown)])
    assert status == 1 and not json.loads(output.read_text())["passed"]
    assert "could not start" in markdown.read_text()
