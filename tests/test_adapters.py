"""Offline adapter contracts: real child processes and a local HTTP server."""

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.adapters import (
    AdapterError,
    ClaudeCodeRunner,
    CodexRunner,
    CommandRunner,
    FunctionRunner,
    HTTPRunner,
    agent_output_from_payload,
)
from harness.runner import AgentOutput


def command(source, **kwargs):
    return CommandRunner([sys.executable, "-c", source], **kwargs)


@pytest.fixture
def executable(tmp_path):
    def make(source):
        if os.name != "posix":
            pytest.skip("executable CLI fixtures require POSIX shebang support")
        script = tmp_path / "mock-agent"
        interpreter = str(Path(sys.executable).resolve())
        script.write_text(f"#!{interpreter}\n{source}\n", encoding="utf-8")
        script.chmod(0o755)
        return str(script)
    return make


@pytest.fixture
def http_agent():
    state = {"response": {"output": "hello"}, "status": 200, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = {
                "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                "authorization": self.headers.get("Authorization"),
                "custom": self.headers.get("X-Custom"),
                "content_type": self.headers.get("Content-Type"),
            }
            state["requests"].append(request)
            time.sleep(state.get("delay", 0))
            body = state["response"]
            if not isinstance(body, bytes):
                body = json.dumps(body).encode("utf-8")
            self.send_response(state["status"])
            for key, value in state.get("headers", {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/agent", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_function_runner_accepts_text_envelope_and_output():
    for answer in ["hello", {"output": "hello"}, AgentOutput("hello")]:
        assert FunctionRunner(lambda _: answer).run("prompt").output == "hello"


def test_function_runner_accepts_async_callable():
    async def answer(prompt):
        await asyncio.sleep(0)
        return {"output": prompt.upper(), "usage": {"input_tokens": 3}}

    result = FunctionRunner(answer).run("hello")
    assert result.output == "HELLO"
    assert result.usage == {"input_tokens": 3}


def test_async_function_runner_explains_active_event_loop():
    async def answer(prompt):
        return prompt

    async def invoke():
        with pytest.raises(AdapterError, match="active event loop"):
            FunctionRunner(answer).run("hello")

    asyncio.run(invoke())


@pytest.mark.parametrize("payload", [
    None, [], {"output": None}, {"output": 7}, {"output": "x", "trajectory": "tool"},
    {"output": "x", "trajectory": [1]}, {"output": "x", "usage": []},
    {"output": "x", "usage": {"input_tokens": -1}},
    {"output": "x", "usage": {"input_tokens": True}},
    {"output": "x", "usage": {"nested": {"input_tokens": 2}}},
    {"output": "x", "usage": {"input_tokens": float("nan")}},
    {"output": "x", "cost_usd": True}, {"output": "x", "cost_usd": -0.5},
    {"output": "x", "cost_usd": float("inf")}, {"output": "x", "cost_usd": 10 ** 400},
    {"output": "x", "metadata": []}, {"output": "x", "metadata": {"object": object()}},
    {"output": "x", "metadata": {"nested": {1: "bad-key", "str": "other"}}},
])
def test_invalid_response_envelopes_are_rejected(payload):
    with pytest.raises(AdapterError):
        agent_output_from_payload(payload)


def test_command_stdin_unicode_and_whitespace_are_preserved():
    text = "  answer café 🙂\n"
    # Text-mode Python stdout translates LF to CRLF on Windows. Echo bytes so
    # this verifies adapter preservation independently of the fixture's OS.
    runner = command("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())")
    assert runner.run(text).output == text


def test_command_placeholder_is_one_literal_argument_without_shell(tmp_path):
    marker = tmp_path / "SHOULD_NOT_EXIST"
    prompt = f'$(touch "{marker}"); echo unsafe `touch "{marker}"`'
    runner = CommandRunner([
        sys.executable, "-c", "import json,sys; print(json.dumps({'output':sys.argv[1], 'metadata':{'stdin':sys.stdin.read()}}))",
        "prefix:{input}:suffix",
    ], output_format="json")
    result = runner.run(prompt)
    assert result.output == "prefix:" + prompt + ":suffix"
    assert result.metadata["stdin"] == ""
    assert not marker.exists()


def test_command_json_envelope_cwd_and_environment(tmp_path):
    runner = command(
        "import json,os; print(json.dumps({'answer':'ok','trajectory':['search'], 'usage':{'input_tokens':2}, 'cost_usd':0.01, 'metadata':{'cwd':os.getcwd(),'env':os.environ['ADAPTER_TEST_VALUE']}}))",
        cwd=tmp_path, env={"ADAPTER_TEST_VALUE": "test-value"},
        output_format="json", output_key="answer",
    )
    result = runner.run("prompt")
    assert result.output == "ok"
    assert result.trajectory == ["search"]
    assert result.usage == {"input_tokens": 2}
    assert result.cost_usd == 0.01
    assert result.metadata == {"cwd": str(tmp_path), "env": "test-value"}


def test_nonzero_exit_omits_sensitive_stdout_and_stderr():
    runner = command("import sys; print('secret-stdout'); print('secret-stderr',file=sys.stderr); sys.exit(7)")
    with pytest.raises(AdapterError, match="status 7") as error:
        runner.run("private prompt")
    assert "secret" not in str(error.value)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("source,match", [
    ("print('not json')", "invalid JSON"),
    ("print('[]')", "JSON object"),
    ("print('{\"output\":3}')", "must be a string"),
    ("print('{\"output\":\"x\",\"cost_usd\":NaN}')", "invalid JSON"),
])
def test_command_invalid_json_is_actionable(source, match):
    with pytest.raises(AdapterError, match=match):
        command(source, output_format="json").run("hello")


def test_command_rejects_invalid_utf8_output():
    with pytest.raises(AdapterError, match="UTF-8"):
        command("import sys; sys.stdout.buffer.write(b'\\xff')").run("hello")


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_command_enforces_bounded_output_for_both_streams(stream):
    with pytest.raises(AdapterError, match="output limit"):
        command(f"import sys; sys.{stream}.write('x'*2000000)", max_output_bytes=1024).run("hello")


def test_command_drains_stdout_and_stderr_without_deadlock():
    result = command("import sys; sys.stderr.write('x'*200000); sys.stdout.write('answer')").run("hello")
    assert result.output == "answer"


def test_command_times_out_when_child_does_not_consume_stdin():
    start = time.monotonic()
    with pytest.raises(AdapterError, match="timed out"):
        command("import time; time.sleep(30)", timeout=0.15).run("x" * 1_000_000)
    assert time.monotonic() - start < 3


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-specific")
@pytest.mark.parametrize("parent_exits", [False, True])
def test_timeout_kills_descendant_processes(tmp_path, parent_exits):
    marker = tmp_path / "descendant-pid"
    child_code = "import time; time.sleep(30)"
    source = (
        "import pathlib,subprocess,sys,time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n"
        + ("" if parent_exits else "time.sleep(30)\n")
    )
    try:
        with pytest.raises(AdapterError, match="timed out"):
            command(source, timeout=0.4).run("hello")
        pid = int(marker.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            # Linux may retain the killed orphan as a zombie until init reaps
            # it. A zombie is dead and cannot continue agent work.
            stat = Path(f"/proc/{pid}/stat")
            if stat.exists() and stat.read_text().split()[2] == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("agent descendant survived timeout cleanup")
    finally:
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("kwargs", [
    {"command": "echo hello"}, {"command": []}, {"command": ["echo", 3]},
    {"command": ["echo"], "timeout": 0}, {"command": ["echo"], "timeout": float("nan")},
    {"command": ["echo"], "max_output_bytes": 0}, {"command": ["echo"], "output_format": "xml"},
])
def test_command_configuration_is_validated(kwargs):
    with pytest.raises(ValueError):
        CommandRunner(**kwargs)


def test_missing_executable_and_cwd_are_safe_errors(tmp_path):
    with pytest.raises(AdapterError, match="could not start"):
        CommandRunner([str(tmp_path / "absent")]).run("private prompt")
    with pytest.raises(AdapterError, match="working directory"):
        command("print('x')", cwd=tmp_path / "absent").run("private prompt")


def emit_events(events):
    return "import json\n" + "\n".join(f"print(json.dumps({event!r}))" for event in events)


def test_codex_jsonl_final_answer_usage_and_safe_arguments(executable, tmp_path):
    events = [
        {"type": "thread.started", "thread_id": "thread-123"},
        {"type": "turn.started"},
        {"type": "future.event", "anything": "ignored"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "private reasoning"}},
        {"type": "item.completed", "item": {"type": "command_execution"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "search"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "intermediate"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
        {"type": "turn.completed", "usage": {"input_tokens": 12, "cached_input_tokens": 4, "output_tokens": 3}},
    ]
    record = tmp_path / "request.json"
    source = (
        "import json,sys,os,pathlib\n"
        f"pathlib.Path({str(record)!r}).write_text(json.dumps({{'argv':sys.argv[1:], 'input':sys.stdin.read(), 'cwd':os.getcwd()}}))\n"
        + emit_events(events)
    )
    result = CodexRunner(executable=executable(source), cwd=tmp_path, model="example-model").run("my prompt")
    assert result.output == "answer"
    assert result.trajectory == ["command_execution", "search"]
    assert result.usage == {"input_tokens": 12, "cached_input_tokens": 4, "output_tokens": 3}
    assert result.metadata == {"adapter": "codex", "thread_id": "thread-123"}
    assert result.cost_usd is None
    request = json.loads(record.read_text())
    assert request == {
        "argv": ["exec", "--json", "--ephemeral", "--sandbox", "read-only", "--model", "example-model", "-"],
        "input": "my prompt", "cwd": str(tmp_path),
    }


@pytest.mark.parametrize("events,match", [
    ([{"type": "turn.failed", "error": {"message": "secret-value"}}], "failed turn"),
    ([{"type": "error", "message": "secret-value"}], "failed turn"),
    ([{"type": "turn.completed", "usage": {}}], "incomplete"),
    ([{"type": "item.completed", "item": {"type": "agent_message", "text": "x"}}], "incomplete"),
    ([{"type": "item.completed", "item": None}], "item object"),
    ([{"type": "item.completed", "item": {"type": "agent_message", "text": 3}}], "contain text"),
    ([[]], "events must be objects"),
])
def test_codex_rejects_failed_or_incomplete_events(executable, events, match):
    with pytest.raises(AdapterError, match=match) as error:
        CodexRunner(executable=executable(emit_events(events))).run("prompt")
    assert "secret-value" not in str(error.value)


def test_codex_rejects_malformed_jsonl(executable):
    with pytest.raises(AdapterError, match="invalid JSON"):
        CodexRunner(executable=executable("print('not json')")).run("prompt")


def test_claude_code_result_usage_cost_and_arguments(executable, tmp_path):
    provider_usage = {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 7, "server_tool_use": {"web_search_requests": 0}, "service_tier": "standard"}
    payload = {"type": "result", "subtype": "success", "is_error": False, "result": "answer", "usage": provider_usage, "total_cost_usd": 0.012, "session_id": "session-123"}
    record = tmp_path / "request.json"
    source = (
        "import json,sys,pathlib\n"
        f"pathlib.Path({str(record)!r}).write_text(json.dumps({{'argv':sys.argv[1:], 'input':sys.stdin.read()}}))\n"
        f"print(json.dumps({payload!r}))"
    )
    result = ClaudeCodeRunner(executable=executable(source), model="example-model", extra_args=["--max-turns", "2"]).run("my prompt")
    assert result.output == "answer"
    assert result.usage == {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 7, "total_tokens": 19}
    assert result.cost_usd == 0.012
    assert result.metadata["provider_usage"] == provider_usage
    assert result.metadata["session_id"] == "session-123"
    assert json.loads(record.read_text()) == {
        "argv": ["-p", "--output-format", "json", "--permission-mode", "dontAsk", "--model", "example-model", "--max-turns", "2"],
        "input": "my prompt",
    }


@pytest.mark.parametrize("payload,match", [
    ([], "result object"),
    ({"type": "assistant"}, "result object"),
    ({"type": "result", "is_error": True, "subtype": "error_max_turns", "result": "secret-value"}, "unsuccessful"),
    ({"type": "result", "is_error": False, "subtype": "success", "result": 3}, "must be a string"),
    ({"type": "result", "is_error": False, "subtype": "success", "result": "answer", "usage": {"input_tokens": -1}}, "nonnegative"),
    ({"type": "result", "is_error": False, "subtype": "success", "result": "answer", "usage": {"input_tokens": "unknown"}}, "nonnegative"),
])
def test_claude_code_validates_result(executable, payload, match):
    source = f"import json; print(json.dumps({payload!r}))"
    with pytest.raises(AdapterError, match=match) as error:
        ClaudeCodeRunner(executable=executable(source)).run("my prompt")
    assert "secret-value" not in str(error.value)


def test_http_json_input_authentication_and_envelope(http_agent, monkeypatch):
    url, state = http_agent
    monkeypatch.setenv("ADAPTER_TEST_TOKEN", "test-secret")
    state["response"] = {"answer": "bonjour", "trajectory": ["translate"], "usage": {"input_tokens": 3}, "cost_usd": 0.03}
    result = HTTPRunner(url, token_env="ADAPTER_TEST_TOKEN", output_key="answer", headers={"X-Custom": "value"}).run("café 🙂")
    assert result.output == "bonjour"
    assert result.trajectory == ["translate"]
    assert result.usage == {"input_tokens": 3}
    assert result.cost_usd == 0.03
    assert state["requests"] == [{"body": {"input": "café 🙂"}, "authorization": "Bearer test-secret", "custom": "value", "content_type": "application/json"}]


def test_http_missing_auth_fails_before_request(http_agent, monkeypatch):
    url, state = http_agent
    monkeypatch.delenv("ADAPTER_MISSING_TOKEN", raising=False)
    with pytest.raises(AdapterError, match="environment variable"):
        HTTPRunner(url, token_env="ADAPTER_MISSING_TOKEN").run("hello")
    assert state["requests"] == []


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_errors_omit_server_body_and_token(http_agent, monkeypatch, status):
    url, state = http_agent
    state.update(status=status, response=b"secret-server-body")
    monkeypatch.setenv("ADAPTER_TEST_TOKEN", "secret-token")
    with pytest.raises(AdapterError, match=f"status {status}") as error:
        HTTPRunner(url, token_env="ADAPTER_TEST_TOKEN").run("hello")
    assert "secret" not in str(error.value)


def test_http_refuses_redirects_without_replaying_credentials(http_agent, monkeypatch):
    url, state = http_agent
    monkeypatch.setenv("ADAPTER_TEST_TOKEN", "secret-token")
    state.update(status=307, headers={"Location": url + "/redirected"})
    with pytest.raises(AdapterError, match="redirects are disabled"):
        HTTPRunner(url, token_env="ADAPTER_TEST_TOKEN").run("hello")
    assert len(state["requests"]) == 1


@pytest.mark.parametrize("response,match", [
    (b"not JSON", "invalid JSON"),
    (b"\xff", "UTF-8"),
    ([], "JSON object"),
    ({"output": 7}, "must be a string"),
])
def test_http_validates_responses(http_agent, response, match):
    url, state = http_agent
    state["response"] = response
    with pytest.raises(AdapterError, match=match):
        HTTPRunner(url).run("hello")


def test_http_response_size_is_bounded(http_agent):
    url, state = http_agent
    state["response"] = {"output": "x" * 2000}
    with pytest.raises(AdapterError, match="response limit"):
        HTTPRunner(url, max_output_bytes=1024).run("hello")


def test_http_timeout(http_agent):
    url, state = http_agent
    state["delay"] = 0.3
    with pytest.raises(AdapterError, match="timed out"):
        HTTPRunner(url, timeout=0.05).run("hello")


@pytest.mark.parametrize("url", ["file:///tmp/agent", "ftp://example.com", "http://user:password@localhost", "invalid"])
def test_http_rejects_unsupported_urls_without_echoing_credentials(url):
    with pytest.raises(ValueError, match="HTTP\\(S\\)") as error:
        HTTPRunner(url)
    assert "password" not in str(error.value)


def test_http_invalid_request_does_not_expose_url_secrets():
    with pytest.raises(AdapterError, match="request failed") as error:
        HTTPRunner("http://127.0.0.1/secret-value has-space").run("hello")
    assert "secret-value" not in str(error.value)
