import json
import sys

from click.testing import CliRunner

from harness.cli import cli


def test_record_replay_rescore_and_gate(tmp_path):
    runner = CliRunner()
    assert runner.invoke(cli, ["init", str(tmp_path)]).exit_code == 0
    assert runner.invoke(cli, ["eval", "--config", str(tmp_path / "agent-eval.toml")]).exit_code == 0
    source = tmp_path / "results/run.json"
    original = source.read_bytes()
    replay = runner.invoke(cli, ["replay", "--result", str(source), "--html", str(tmp_path / "replay.html")])
    assert replay.exit_code == 0, replay.output
    output = tmp_path / "rescore.json"
    rescore = runner.invoke(cli, ["rescore", "--result", str(source), "--scorers", "contains",
                                  "--output", str(output), "--min-pass-rate", "1"])
    assert rescore.exit_code == 0, rescore.output
    assert source.read_bytes() == original
    assert json.loads(output.read_text())["pass_rate"] == 1
    unknown = runner.invoke(cli, ["gate", "--result", str(output), "--max-cost-usd", "1",
                                  "--output", str(tmp_path / "gate.json")])
    assert unknown.exit_code == 1, unknown.output
    assert "missing measurements" in unknown.output
    assert not json.loads((tmp_path / "gate.json").read_text())["passed"]


def test_output_pack_cli(tmp_path):
    runner = CliRunner()
    listed = runner.invoke(cli, ["pack", "list"])
    assert listed.exit_code == 0, listed.output
    assert "json-contracts@1.0.0" in listed.output
    inspected = runner.invoke(cli, ["pack", "inspect", "json-contracts@1.0.0"])
    assert inspected.exit_code == 0, inspected.output
    assert len(json.loads(inspected.output)["fingerprint"]) == 64
    result = runner.invoke(cli, ["eval", "--pack", "json-contracts@1.0.0", "--command",
                                  json.dumps([sys.executable, "-c", "print('null')"]),
                                  "--output", str(tmp_path / "result.json")])
    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "result.json").read_text())
    assert "json_match" in data["scores"]
    assert data["metadata"]["pack"]["id"] == "json-contracts"


def test_coding_rejects_missing_explicit_env_before_execution(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_TEST_MISSING_ENV", raising=False)
    result = CliRunner().invoke(cli, ["coding", "--pack", "coding-starter", "--command", '["true"]',
                                      "--env", "HARNESS_TEST_MISSING_ENV"])
    assert result.exit_code == 1
    assert "is missing" in result.output


def test_rescore_preserves_source_path(tmp_path):
    runner = CliRunner()
    assert runner.invoke(cli, ["init", str(tmp_path)]).exit_code == 0
    assert runner.invoke(cli, ["eval", "--config", str(tmp_path / "agent-eval.toml")]).exit_code == 0
    source = tmp_path / "results/run.json"
    original = source.read_bytes()
    result = runner.invoke(cli, ["rescore", "--result", str(source), "--output", str(source), "--scorers", "exact"])
    assert result.exit_code == 1
    assert source.read_bytes() == original
