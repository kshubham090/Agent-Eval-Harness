import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from click.testing import CliRunner

from harness.cli import cli


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/action_runner.py"


def run_action(tmp_path, **settings):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AE_", "GITHUB_"))}
    env.update(GITHUB_WORKSPACE=str(tmp_path), GITHUB_OUTPUT=str(tmp_path / "outputs"),
               GITHUB_STEP_SUMMARY=str(tmp_path / "summary.md"))
    env.update(settings)
    return subprocess.run([sys.executable, str(SCRIPT)], env=env, cwd=tmp_path,
                          text=True, capture_output=True, timeout=60)


def test_action_evaluates_gates_and_writes_outputs_with_installed_code(tmp_path):
    assert CliRunner().invoke(cli, ["init", str(tmp_path)]).exit_code == 0
    # A user's project can itself contain a package named harness.
    shadow = tmp_path / "harness"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("")
    (shadow / "__main__.py").write_text("raise RuntimeError('wrong harness')")
    result = run_action(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "agent-eval-results/gates.json").read_text())["passed"]
    assert "Agent evaluation: PASS" in (tmp_path / "summary.md").read_text()
    assert "result=" in (tmp_path / "outputs").read_text()


def test_action_cost_gate_fails_closed_and_keeps_report(tmp_path):
    assert CliRunner().invoke(cli, ["init", str(tmp_path)]).exit_code == 0
    result = run_action(tmp_path, AE_MAX_COST_USD="100")
    assert result.returncode == 1
    assert (tmp_path / "agent-eval-results/report.html").exists()
    assert "missing measurements" in result.stdout


def test_action_can_explicitly_allow_error_fraction(tmp_path):
    (tmp_path / "cases.jsonl").write_text(json.dumps({"id": "a", "input": "p", "expected_output": "x"}) + "\n")
    (tmp_path / "agent.py").write_text("def answer(prompt):\n    raise RuntimeError('fixture')\n")
    (tmp_path / "agent-eval.toml").write_text('[agent]\ntype="python"\npath="agent.py::answer"\n'
                                              '[eval]\ndataset="cases.jsonl"\n')
    result = run_action(tmp_path, AE_MIN_PASS_RATE="0", AE_MAX_ERROR_RATE="1")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("directory", ["..", ".", "../outside", "bad\npath"])
def test_action_rejects_unsafe_output_before_execution(tmp_path, directory):
    result = run_action(tmp_path, AE_OUTPUT_DIRECTORY=directory)
    assert result.returncode == 2
    assert not (tmp_path / "outputs").exists()
