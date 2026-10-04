"""Trace events survive adapters and recordings without inventing provider data."""
from copy import deepcopy
import json

import pytest

import harness.adapters as adapters
from harness.adapters import AdapterError, CodexRunner, FunctionRunner
from harness.dataset import EvalCase
from harness.eval_runner import run_eval
from harness.replay import load_recording, rescore_result
from harness.runner import AgentOutput
from harness.scorers.exact import ExactMatchScorer


TRACE = [{"type": "tool_result", "name": "lookup", "result": {"answer": 42, "sources": ["fixture"]}}]


@pytest.mark.parametrize("kind", ["envelope", "dataclass", "async"])
def test_function_events_survive_recording_rescore_and_external_mutation(tmp_path, kind):
    events = deepcopy(TRACE)
    envelope = {"output": "42", "trajectory": ["lookup"], "events": events}
    if kind == "dataclass":
        runner = FunctionRunner(lambda _: AgentOutput(**envelope))
    elif kind == "async":
        async def answer(_):
            return envelope
        runner = FunctionRunner(answer)
    else:
        runner = FunctionRunner(lambda _: envelope)
    result = run_eval([EvalCase("case", "prompt", "42")], runner, [ExactMatchScorer()])
    assert result.error_count == 0
    events[0]["result"]["answer"] = "changed after evaluation"
    path = tmp_path / "events.json"
    result.save(path)
    source = load_recording(path)
    assert source["cases"][0]["events"] == TRACE
    assert rescore_result(source, [ExactMatchScorer()])["cases"][0]["events"] == TRACE


@pytest.mark.parametrize("kind", ["envelope", "dataclass"])
@pytest.mark.parametrize("events", [None, {}, ["tool"], [None], [{"value": float("inf")}],
                                     [{"value": float("nan")}], [{"nested": {1: "bad key"}}],
                                     [{"value": (1, 2)}], [{"value": object()}]])
def test_function_adapter_rejects_malformed_event_payloads(kind, events):
    payload = {"output": "answer", "events": events}
    runner = FunctionRunner(lambda _: AgentOutput(**payload) if kind == "dataclass" else payload)
    with pytest.raises(AdapterError):
        runner.run("prompt")


def codex_stream(monkeypatch, events):
    stream = "\n".join(json.dumps(event) for event in events)
    monkeypatch.setattr(adapters, "_execute", lambda *args, **kwargs: stream)
    return CodexRunner()


def complete(events):
    return [*events, {"type": "item.completed", "item": {"type": "agent_message", "text": "42"}},
            {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 1}}]


def test_codex_records_only_available_completed_tool_events_in_order(monkeypatch, tmp_path):
    tool_events = [
        {"type": "item.completed", "item": {"id": "cmd", "type": "command_execution",
                                               "command": "python solve.py", "aggregated_output": "42", "exit_code": 0}},
        {"type": "item.completed", "item": {"id": "mcp", "type": "mcp_tool_call", "tool": "lookup",
                                               "result": {"structured_content": {"answer": 42}}}},
        {"type": "item.completed", "item": {"id": "web", "type": "web_search", "query": "meaning of 42"}},
        {"type": "item.completed", "item": {"id": "file", "type": "file_change",
                                               "changes": [{"path": "answer.txt", "kind": "add"}]}},
    ]
    ignored = [
        {"type": "thread.started", "thread_id": "thread-fixture"},
        {"type": "item.started", "item": {"type": "command_execution", "command": "not completed"}},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "unrecorded reasoning"}},
        {"type": "future.event", "content": "unrecorded unknown event"},
    ]
    runner = codex_stream(monkeypatch, complete([*ignored, *tool_events]))
    result = run_eval([EvalCase("case", "prompt", "42")], runner, [ExactMatchScorer()])
    assert result.error_count == 0
    case = result.case_results[0]
    assert case.events == tool_events
    assert case.trajectory == ["command_execution", "lookup", "web_search", "file_change"]
    serialized = json.dumps(result.to_dict())
    assert "unrecorded reasoning" not in serialized
    assert "not completed" not in serialized
    result.save(tmp_path / "codex.json")
    assert load_recording(tmp_path / "codex.json")["cases"][0]["events"] == tool_events


def test_codex_recovered_provider_errors_are_sanitized_in_saved_recording(monkeypatch):
    secret = "test-credential-that-must-not-be-recorded"
    runner = codex_stream(monkeypatch, complete([
        {"type": "error", "message": secret, "details": {"headers": {"Authorization": secret}}},
    ]))
    result = run_eval([EvalCase("case", "prompt", "42")], runner, [ExactMatchScorer()]).to_dict()
    assert result["cases"][0]["events"] == [{"type": "error", "details_omitted": True}]
    assert result["cases"][0]["metadata"]["provider_error_events"] == 1
    assert secret not in json.dumps(result)


@pytest.mark.parametrize("event", [
    {"type": "turn.failed", "error": {"message": "private-provider-message"}},
    {"type": "error", "message": "private-provider-message"},
])
def test_codex_terminal_errors_omit_provider_details(monkeypatch, event):
    runner = codex_stream(monkeypatch, [event])
    with pytest.raises(AdapterError) as caught:
        runner.run("prompt")
    assert "private-provider-message" not in str(caught.value)
    assert "provider details omitted" in str(caught.value)


@pytest.mark.parametrize("bad_event", [
    [], {"type": 2}, {"type": "item.completed", "item": []},
    {"type": "item.completed", "item": {"type": "agent_message", "text": 3}},
    {"type": "item.completed", "item": {"type": "command_execution", "exit_code": float("inf")}},
])
def test_codex_rejects_malformed_event_streams(monkeypatch, bad_event):
    runner = codex_stream(monkeypatch, complete([bad_event]))
    with pytest.raises(AdapterError):
        runner.run("prompt")


@pytest.mark.parametrize("item_type", [None, [], {}, 42, True, ""])
def test_codex_completed_item_requires_a_string_type(monkeypatch, item_type):
    runner = codex_stream(monkeypatch, complete([
        {"type": "item.completed", "item": {"type": item_type}},
    ]))
    with pytest.raises(AdapterError):
        runner.run("prompt")
