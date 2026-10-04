"""Review regressions: evidence paths and CLI source overrides fail safely."""
import json
import os
import sys

from click.testing import CliRunner
import pytest

from harness.adapters import FunctionRunner
from harness.cli import cli
from harness.dataset import EvalCase
from harness.eval_runner import run_eval
from harness.scorers.exact import ExactMatchScorer


def saved_recording(path):
    result = run_eval([EvalCase("case", "prompt", "ok")],
                      FunctionRunner(lambda _: "ok"), [ExactMatchScorer()])
    result.save(path)
    return path.read_bytes()


@pytest.mark.parametrize("command", ["replay", "gate", "rescore-json", "rescore-html"])
@pytest.mark.parametrize("alias_kind", ["same-path", "hardlink", "symlink"])
def test_source_recordings_cannot_be_overwritten_through_path_aliases(tmp_path, monkeypatch,
                                                                   command, alias_kind):
    source = tmp_path / "source.json"
    original = saved_recording(source)
    target = source if alias_kind == "same-path" else tmp_path / "alias.json"
    if alias_kind == "hardlink":
        try:
            os.link(source, target)
        except OSError as exc:
            pytest.skip(f"filesystem does not support test hardlinks: {exc}")
    elif alias_kind == "symlink":
        try:
            target.symlink_to(source)
        except OSError as exc:
            pytest.skip(f"filesystem does not support test symlinks: {exc}")

    def forbidden(*args, **kwargs):
        pytest.fail("path collision must be rejected before any grading")

    monkeypatch.setattr("harness.replay.rescore_result", forbidden)
    if command == "replay":
        args = ["replay", "--html", str(target)]
    elif command == "gate":
        args = ["gate", "--output", str(target)]
    elif command == "rescore-json":
        args = ["rescore", "--scorers", "exact", "--output", str(target)]
    else:
        args = ["rescore", "--scorers", "exact", "--output", str(tmp_path / "new.json"),
                "--html", str(target)]
    outcome = CliRunner().invoke(cli, [*args, "--result", str(source)])
    assert outcome.exit_code == 1, outcome.output
    assert "distinct" in outcome.output
    assert source.read_bytes() == original
    assert target.read_bytes() == original
    assert not (tmp_path / "new.json").exists()


@pytest.mark.parametrize("alias_kind", ["same-path", "hardlink"])
def test_rescore_json_and_html_cannot_alias_each_other(tmp_path, monkeypatch, alias_kind):
    source = tmp_path / "source.json"
    saved_recording(source)
    output = tmp_path / "new.json"
    output.write_text("previous evidence")
    html = output
    if alias_kind == "hardlink":
        html = tmp_path / "new.html"
        try:
            os.link(output, html)
        except OSError as exc:
            pytest.skip(f"filesystem does not support test hardlinks: {exc}")

    def forbidden(*args, **kwargs):
        pytest.fail("output aliases must fail before grading")

    monkeypatch.setattr("harness.replay.rescore_result", forbidden)
    outcome = CliRunner().invoke(cli, ["rescore", "--result", str(source), "--scorers", "exact",
                                      "--output", str(output), "--html", str(html)])
    assert outcome.exit_code == 1
    assert "distinct" in outcome.output
    assert output.read_text() == "previous evidence"


@pytest.mark.parametrize("selected", ["pack", "dataset"])
@pytest.mark.parametrize("configured", ['dataset="cases.jsonl"', 'dataset="missing.jsonl"',
                                        'pack="json-contracts@1.0.0"', 'pack="missing-pack@1.0.0"'])
def test_explicit_cli_dataset_or_pack_overrides_configured_source(tmp_path, selected, configured):
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"id": "own-case", "input": "prompt", "expected_output": "null"}) + "\n")
    config = tmp_path / "agent-eval.toml"
    config.write_text(f"[eval]\n{configured}\n")
    output = tmp_path / "run.json"
    selection = "json-contracts@1.0.0" if selected == "pack" else str(cases)
    outcome = CliRunner().invoke(cli, ["eval", "--config", str(config), f"--{selected}", selection,
                                      "--command", json.dumps([sys.executable, "-c", "print('null')"]),
                                      "--output", str(output)])
    assert outcome.exit_code == 0, outcome.output
    data = json.loads(output.read_text())
    if selected == "pack":
        assert data["metadata"]["pack"]["id"] == "json-contracts"
        assert "json_match" in data["scores"]
        assert "own-case" not in {case["id"] for case in data["cases"]}
    else:
        assert [case["id"] for case in data["cases"]] == ["own-case"]
        assert "pack" not in data["metadata"]


@pytest.mark.parametrize("configured, message", [
    ('dataset=123', "eval.dataset must be a path string"),
    ('dataset="missing.jsonl"\nunknown=1', "unknown config [eval] keys"),
    ('dataset="missing.jsonl"\nruns="two"', "eval.runs must be an integer"),
])
def test_cli_source_override_keeps_strict_config_validation(tmp_path, configured, message):
    config = tmp_path / "agent-eval.toml"
    config.write_text(f"[eval]\n{configured}\n")
    outcome = CliRunner().invoke(cli, ["eval", "--config", str(config),
                                      "--pack", "json-contracts@1.0.0"])
    assert outcome.exit_code == 1
    assert message in outcome.output


def test_both_explicit_cli_sources_still_fail_as_a_usage_error(tmp_path):
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"id": "case", "input": "prompt", "expected_output": "null"}) + "\n")
    config = tmp_path / "agent-eval.toml"
    config.write_text('[eval]\ndataset="missing.jsonl"\n')
    outcome = CliRunner().invoke(cli, ["eval", "--config", str(config), "--dataset", str(cases),
                                      "--pack", "json-contracts@1.0.0"])
    assert outcome.exit_code == 2
    assert "choose --dataset or --pack, not both" in outcome.output


@pytest.mark.parametrize("field", ["--output", "--html"])
def test_output_alias_cannot_overwrite_a_protected_pack_file(tmp_path, monkeypatch, field):
    pack = tmp_path / "pack"
    assert CliRunner().invoke(cli, ["pack", "init", str(pack), "--kind", "output"]).exit_code == 0
    protected = pack / "pack.toml"
    original = protected.read_bytes()
    alias = tmp_path / "result"
    try:
        os.link(protected, alias)
    except OSError as exc:
        pytest.skip(f"filesystem does not support test hardlinks: {exc}")

    def forbidden(*args, **kwargs):
        pytest.fail("hard-linked outputs must be rejected before agent calls")

    monkeypatch.setattr("harness.cli._make_runner", forbidden)
    outcome = CliRunner().invoke(cli, ["eval", "--pack", str(pack), field, str(alias)])
    assert outcome.exit_code == 1, outcome.output
    assert "hard links" in outcome.output
    assert protected.read_bytes() == original
    assert alias.read_bytes() == original


def test_existing_artifact_directory_is_not_mistaken_for_a_hard_link(tmp_path):
    from harness.workflows import validate_output_paths

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "previous-run").mkdir()
    validate_output_paths(outputs=[artifacts])


@pytest.mark.parametrize("field", ["--output", "--html"])
@pytest.mark.parametrize("nested_path", ["result.json", "cases/case/evidence.json", "run-1/cases/case/grader.log"])
def test_coding_rejects_outputs_inside_artifact_directory_before_starting_containers(
        tmp_path, monkeypatch, field, nested_path):
    artifacts = tmp_path / "artifacts"
    output, html = tmp_path / "run.json", tmp_path / "run.html"
    if field == "--output":
        output = artifacts / nested_path
    else:
        html = artifacts / nested_path

    def forbidden(*args, **kwargs):
        pytest.fail("invalid artifact destinations must be rejected before Docker use")

    monkeypatch.setattr("harness.coding.run_coding_eval", forbidden)
    outcome = CliRunner().invoke(cli, ["coding", "--pack", "coding-starter@1.0.0",
                                      "--command", '["true"]', "--artifacts", str(artifacts),
                                      "--output", str(output), "--html", str(html)])
    assert outcome.exit_code == 1, outcome.output
    assert "outside" in outcome.output
    assert not artifacts.exists()
    assert not output.exists()
    assert not html.exists()
