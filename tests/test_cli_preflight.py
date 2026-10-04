import json

import pytest
from click.testing import CliRunner

from harness.cli import cli
from harness.report import render_report
from harness.scorers.structured import JSONMatchScorer


@pytest.mark.parametrize("baseline, expected", [
    (None, "no baseline named"),
    ({"scores": {"exact_match": {"mean": 1}}, "pass_rate": 1, "dataset_sha": "other"}, "dataset changed"),
    ({"scores": {"exact_match": {"mean": 1}}, "pass_rate": 1, "metadata": {"pass_threshold": 0.9}}, "scoring protocol changed"),
    ({"scores": {"missing": {"mean": 1}}, "pass_rate": 1}, "baseline metrics missing"),
])
def test_invalid_baseline_never_starts_agent(tmp_path, monkeypatch, baseline, expected):
    import harness.cli as module

    def no_agent(*args):
        pytest.fail("agent loaded before baseline validation")

    monkeypatch.setattr(module, "_make_runner", no_agent)
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text('{"id":"1","input":"x","expected_output":"x"}\n')
    if baseline is not None:
        (tmp_path / "gate.json").write_text(json.dumps(baseline))
    result = CliRunner().invoke(cli, ["eval", "--dataset", str(dataset), "--agent", "codex",
                                      "--compare-baseline", "gate", "--baselines-dir", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert expected in result.output


def test_report_escapes_untrusted_summary_fields():
    html = render_report({"scores": {}, "pass_rate": 1, "error_count": '<img src=x onerror="alert(1)">'})
    assert "<img" not in html
    assert "&lt;img" in html


def test_json_scorer_preserves_numeric_precision():
    scorer = JSONMatchScorer()
    assert scorer.score("9007199254740992.0", "9007199254740993.0") == 0
    assert scorer.score("1e999", "2e999") == 0
    assert scorer.score("1.0", "1") == 1


def test_command_starter_uses_installed_interpreter(tmp_path):
    import sys
    import tomllib

    runner = CliRunner()
    initialized = runner.invoke(cli, ["init", str(tmp_path), "--agent", "command"])
    assert initialized.exit_code == 0, initialized.output
    config = tomllib.loads((tmp_path / "agent-eval.toml").read_text())
    assert config["agent"]["command"][0] == sys.executable
    result = runner.invoke(cli, ["eval", "--config", str(tmp_path / "agent-eval.toml")])
    assert result.exit_code == 0, result.output


def test_error_policy_can_be_overridden_from_cli(tmp_path):
    import sys

    (tmp_path / "cases.jsonl").write_text('{"id":"1","input":"x","expected_output":"x"}\n')
    (tmp_path / "config.toml").write_text(
        '[agent]\ntype="command"\ncommand=' + json.dumps([sys.executable, "-c", "raise SystemExit(1)"]) +
        '\n[eval]\ndataset="cases.jsonl"\nallow_errors=true\n'
    )
    result = CliRunner().invoke(cli, ["eval", "--config", str(tmp_path / "config.toml"), "--no-allow-errors"])
    assert result.exit_code == 1 and "agent/scorer errors" in result.output


@pytest.mark.parametrize("scorer, reference, message", [
    ("json", "not JSON", "not valid JSON"),
    ("regex", "[", "invalid regex reference"),
])
def test_invalid_references_fail_before_agent_calls(tmp_path, monkeypatch, scorer, reference, message):
    import harness.cli as module

    def no_agent(*args):
        pytest.fail("agent loaded before reference validation")

    monkeypatch.setattr(module, "_make_runner", no_agent)
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps({"id": "bad", "input": "x", "expected_output": reference}) + "\n")
    result = CliRunner().invoke(cli, ["eval", "--dataset", str(path), "--agent", "codex", "--scorers", scorer])
    assert result.exit_code == 1 and message in result.output


def test_missing_optional_dependency_fails_before_agent_calls(tmp_path, monkeypatch):
    import harness.cli as module

    def no_agent(*args):
        pytest.fail("agent loaded before dependency validation")

    monkeypatch.setattr(module, "_make_runner", no_agent)
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: None)
    path = tmp_path / "cases.jsonl"
    path.write_text('{"id":"a","input":"x","expected_output":"x"}\n')
    result = CliRunner().invoke(cli, ["eval", "--dataset", str(path), "--agent", "codex", "--scorers", "llm_judge"])
    assert result.exit_code == 1 and "optional dependencies" in result.output


@pytest.mark.parametrize("floor, exit_code", [("0.90", 0), ("0.900000001", 1)])
def test_repeated_run_floor_allows_only_rounding_error(tmp_path, monkeypatch, floor, exit_code):
    import harness.cli as module
    from harness.runner import AgentOutput

    class Agent:
        count = 0

        def run(self, prompt):
            run_index, case_index = divmod(self.count, 20)
            self.count += 1
            return AgentOutput("yes" if case_index < (17, 19)[run_index] else "no")

    monkeypatch.setattr(module, "_make_runner", lambda _: Agent())
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text("".join(json.dumps({"id": str(i), "input": "answer", "expected_output": "yes"}) + "\n"
                               for i in range(20)))
    result = CliRunner().invoke(cli, ["eval", "--dataset", str(dataset), "--agent", "codex",
                                      "--runs", "2", "--min-pass-rate", floor])
    assert result.exit_code == exit_code, result.output
    assert "0.900" in result.output
