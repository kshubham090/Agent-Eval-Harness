import json
import sys

import pytest
from click.testing import CliRunner

from harness.cli import cli, load_agent
from harness.config import load_config
from harness.scorers.structured import ContainsScorer, JSONMatchScorer


@pytest.fixture
def starter(tmp_path):
    result = CliRunner().invoke(cli, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path


def test_new_user_can_init_evaluate_and_render_from_any_directory(starter, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path.parent)
    runner = CliRunner()
    evaluated = runner.invoke(cli, ["eval", "--config", str(starter / "agent-eval.toml"), "--runs", "2"])
    assert evaluated.exit_code == 0, evaluated.output
    data = json.loads((starter / "results/run.json").read_text())
    assert data["mean"]["pass_rate"] == 1.0
    assert data["run_count"] == 2
    html = (starter / "results/report.html").read_text()
    assert "Across repeated runs" in html
    assert "Run 1" in html and "Run 2" in html
    offline = runner.invoke(cli, ["report", "--result", str(starter / "results/run.json"),
                                  "--html", str(starter / "offline.html")])
    assert offline.exit_code == 0, offline.output


def test_init_preserves_existing_files(starter):
    original = (starter / "cases.jsonl").read_text()
    result = CliRunner().invoke(cli, ["init", str(starter), "--agent", "codex"])
    assert result.exit_code == 1
    assert "refusing to overwrite" in result.output
    assert (starter / "cases.jsonl").read_text() == original


def test_command_adapter_cli_and_config_override(starter):
    command = json.dumps([sys.executable, "-c", 'import sys; sys.stdin.read(); print("wrong")'])
    result = CliRunner().invoke(cli, ["eval", "--config", str(starter / "agent-eval.toml"),
                                      "--command", command])
    assert result.exit_code == 1, result.output
    assert "pass rate 0.000" in result.output
    data = json.loads((starter / "results/run.json").read_text())
    assert data["error_count"] == 0
    assert data["metadata"]["agent"] == "command"


def test_command_errors_fail_even_without_baseline_and_write_artifacts(starter):
    command = json.dumps([sys.executable, "-c", 'raise SystemExit(3)'])
    result = CliRunner().invoke(cli, ["eval", "--dataset", str(starter / "cases.jsonl"),
                                      "--command", command, "--output", str(starter / "failed.json")])
    assert result.exit_code == 1, result.output
    assert "agent/scorer errors" in result.output
    assert json.loads((starter / "failed.json").read_text())["error_count"] == 3


def test_existing_callable_can_be_evaluated(starter):
    agent = starter / "function.py"
    agent.write_text('async def answer(prompt):\n    return "12"\n')
    result = CliRunner().invoke(cli, ["eval", "--dataset", str(starter / "cases.jsonl"),
                                      "--agent", str(agent) + "::answer"])
    assert result.exit_code == 0, result.output
    assert "0.667" in result.output


@pytest.mark.parametrize("flags", [["--runs", "0"], ["--concurrency", "0"], ["--pass-threshold", "1.2"],
                                    ["--threshold", "-1"], ["--timeout", "0"], ["--min-pass-rate", "nan"]])
def test_invalid_run_options_fail_before_agent_execution(starter, flags):
    result = CliRunner().invoke(cli, ["eval", "--config", str(starter / "agent-eval.toml"), *flags])
    assert result.exit_code != 0
    assert not (starter / "results/run.json").exists()


def test_dataset_validation_does_not_invoke_agents(starter):
    result = CliRunner().invoke(cli, ["validate", "--dataset", str(starter / "cases.jsonl")])
    assert result.exit_code == 0 and "3 cases" in result.output


def test_doctor_json():
    result = CliRunner().invoke(cli, ["doctor", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["authentication"].startswith("not checked")


@pytest.mark.parametrize("content, message", [
    ('[eval]\nconcurency = 8', "unknown config"),
    ('[agent]\ntype = "codex"\ncommand = ["x"]', "requires agent.type"),
    ('[agent]\ntype = "command"\ncommand = "x"', "array of strings"),
    ('[eval]\nscorers = "exact"', "array of strings"),
    ('[output]\njson = 2', "path string"),
])
def test_strict_config(tmp_path, content, message):
    path = tmp_path / "config.toml"
    path.write_text(content)
    with pytest.raises(ValueError, match=message):
        load_config(path)


def test_cli_requires_one_adapter(starter):
    result = CliRunner().invoke(cli, ["eval", "--dataset", str(starter / "cases.jsonl"),
                                      "--agent", "codex", "--url", "http://localhost:8000"])
    assert result.exit_code == 2 and "exactly one" in result.output


@pytest.mark.parametrize("expected, actual, score", [
    ('{"x":1,"y":[2,3]}', '{"y":[2,3],"x":1.0}', 1),
    ('{"ok":true}', '{"ok":1}', 0),
    ('[1,2]', '[2,1]', 0),
    ('{"x":1}', '{"x":2,"x":1}', 0),
    ('{"x":1}', '```json\n{"x":1}\n```', 0),
    ('null', 'NaN', 0),
])
def test_json_match_semantics(expected, actual, score):
    assert JSONMatchScorer().score(expected, actual) == score


def test_invalid_json_reference_is_an_error():
    with pytest.raises(ValueError, match="expected_output"):
        JSONMatchScorer().score("NaN", "null")


def test_contains_is_case_sensitive():
    assert ContainsScorer().score("Paris", "The answer is Paris.") == 1
    assert ContainsScorer().score("Paris", "paris") == 0


def test_python_agent_loads_sibling_import_and_restores_import_path(starter):
    (starter / "eval_test_sibling_helper.py").write_text('def answer(prompt):\n    return "12"\n')
    (starter / "external_agent.py").write_text("from eval_test_sibling_helper import answer\n")
    previous_path = sys.path[:]
    try:
        agent = load_agent(str(starter / "external_agent.py") + "::answer")
        assert agent.run("input").output == "12"
        assert sys.path == previous_path
    finally:
        sys.modules.pop("eval_test_sibling_helper", None)


def test_import_path_is_restored_when_agent_import_raises(tmp_path):
    agent = tmp_path / "broken.py"
    agent.write_text('raise RuntimeError("broken import")\n')
    previous_path = sys.path[:]
    with pytest.raises(RuntimeError, match="broken import"):
        load_agent(str(agent))
    assert sys.path == previous_path


def test_command_override_discards_configured_cli_model_and_arguments(starter):
    config = starter / "codex.toml"
    config.write_text('[agent]\ntype="codex"\nmodel="configured-model"\nextra_args=["--ephemeral"]\n'
                      '[eval]\ndataset="cases.jsonl"\n')
    command = json.dumps([sys.executable, "-c", 'print("12")'])
    result = CliRunner().invoke(cli, ["eval", "--config", str(config), "--command", command])
    assert result.exit_code == 0, result.output
    assert "0.667" in result.output
    # Explicit conflicting flags still surface the user's mistake.
    result = CliRunner().invoke(cli, ["eval", "--config", str(config), "--command", command,
                                      "--model", "explicit-model"])
    assert result.exit_code == 2
    assert "require --agent codex or claude-code" in result.output


@pytest.mark.parametrize("selection, explicit_model, expected_model, expected_args", [
    ("codex", None, "configured-model", ("--ephemeral",)),
    ("claude-code", None, None, ()),
    ("claude-code", "explicit-model", "explicit-model", ()),
])
def test_cli_adapter_override_keeps_only_relevant_configuration(starter, monkeypatch, selection,
                                                               explicit_model, expected_model, expected_args):
    from harness.runner import AgentOutput

    config = starter / "codex.toml"
    config.write_text('[agent]\ntype="codex"\nmodel="configured-model"\nextra_args=["--ephemeral"]\n'
                      '[eval]\ndataset="cases.jsonl"\n')
    captured = {}

    class FakeCLI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, input):
            return AgentOutput("12")

    monkeypatch.setattr("harness.adapters.CodexRunner", FakeCLI)
    monkeypatch.setattr("harness.adapters.ClaudeCodeRunner", FakeCLI)
    args = ["eval", "--config", str(config), "--agent", selection]
    if explicit_model:
        args.extend(["--model", explicit_model])
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert captured["model"] == expected_model
    assert captured["extra_args"] == expected_args


@pytest.mark.parametrize("section,key,value,expected", [
    ("agent", "type", '["codex"]', "string"),
    ("agent", "type", '{name = "codex"}', "string"),
    ("agent", "model", "7", "string"),
    ("agent", "output_key", "true", "string"),
    ("agent", "timeout", "true", "number"),
    ("agent", "timeout", '"120"', "number"),
    ("eval", "runs", "2.9", "integer"),
    ("eval", "concurrency", "true", "integer"),
    ("eval", "concurrency", '"4"', "integer"),
    ("eval", "pass_threshold", "false", "number"),
    ("eval", "min_pass_rate", '"0.8"', "number"),
    ("eval", "allow_errors", '"true"', "boolean"),
    ("baseline", "threshold", "false", "number"),
    ("baseline", "allow_dataset_change", "1", "boolean"),
    ("baseline", "name", '["v1"]', "string"),
])
def test_config_rejects_scalar_coercion_before_constructing_agent(starter, monkeypatch, section, key, value, expected):
    config = starter / "invalid.toml"
    config.write_text(f"[{section}]\n{key} = {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=expected):
        load_config(config)

    def unexpected_runner(options):
        pytest.fail("an invalid config must fail before constructing or calling an agent")

    monkeypatch.setattr("harness.cli._make_runner", unexpected_runner)
    result = CliRunner().invoke(cli, ["eval", "--config", str(config),
                                      "--dataset", str(starter / "cases.jsonl"),
                                      "--agent", "codex"])
    assert result.exit_code == 1
    assert expected in result.output


@pytest.mark.parametrize("filename", ["codex", "claude-code"])
def test_python_config_path_cannot_be_reinterpreted_as_cli_adapter(tmp_path, filename):
    config = tmp_path / "agent-eval.toml"
    config.write_text(f'[agent]\ntype = "python"\npath = "{filename}"\n', encoding="utf-8")
    assert load_config(config)["agent_path"] == str(tmp_path / filename)
