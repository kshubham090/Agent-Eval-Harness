import json

from harness.baseline import compare_to_baseline
from harness.dataset import load_dataset
from harness.eval_runner import run_eval
from harness.report import render_report, write_report
from harness.runner import AgentOutput
from harness.scorers import ExactMatchScorer


class StubAgent:
    def run(self, input: str) -> AgentOutput:
        return AgentOutput(output="Paris")


def make_result():
    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    return run_eval(cases, StubAgent(), [ExactMatchScorer()], dataset_sha="abc123def456")


def test_report_contains_metrics_and_all_cases():
    html = render_report(make_result().to_dict())
    assert "exact_match" in html
    assert "pass_rate" in html
    for case_id in ("q1", "q2", "q3", "q4", "q5"):
        assert case_id in html
    assert "abc123def456" in html


def test_report_escapes_html_in_agent_output():
    class XssAgent:
        def run(self, input: str) -> AgentOutput:
            return AgentOutput(output="<script>alert('pwned')</script>")

    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    result = run_eval(cases, XssAgent(), [ExactMatchScorer()])
    html = render_report(result.to_dict())
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_report_includes_baseline_comparison():
    result = make_result().to_dict()
    baseline = json.loads(json.dumps(result))
    baseline["scores"]["exact_match"]["mean"] = 0.9  # force a regression
    baseline["pass_rate"] = 0.9
    comparison = compare_to_baseline(result, baseline, threshold=0.05)

    html = render_report(result, comparison)
    assert "REGRESSION" in html
    assert "FAIL" in html


def test_write_report_produces_full_document(tmp_path):
    path = write_report(tmp_path / "report.html", make_result().to_dict())
    text = path.read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>")
    assert "<title>" in text


def test_report_shows_errors():
    class Exploder:
        def run(self, input: str) -> AgentOutput:
            raise RuntimeError("kaboom")

    cases = load_dataset("datasets/examples/simple_qa.jsonl")
    result = run_eval(cases, Exploder(), [ExactMatchScorer()])
    html = render_report(result.to_dict())
    assert "kaboom" in html
