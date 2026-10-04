"""Bring an existing function, executable, coding agent, or HTTP endpoint.

Every adapter implements ``run(input: str) -> AgentOutput``. Commands are
executed directly except Windows batch shims, which use a restricted cmd.exe
invocation and require stdin prompts. Each invocation is independent, so the
adapters themselves may be shared by the evaluator's worker threads.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import ntpath
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from harness.runner import AgentOutput, validate_json_value

DEFAULT_MAX_OUTPUT_BYTES = 1_048_576
_IS_WINDOWS = os.name == "nt"


class AdapterError(RuntimeError):
    """An agent failed, timed out, or returned an invalid response."""


def _nonnegative_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _string_object_keys(value: Any) -> bool:
    if isinstance(value, dict):
        return all(isinstance(key, str) and _string_object_keys(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_string_object_keys(item) for item in value)
    return True


def agent_output_from_payload(payload: Any, *, output_key: str = "output") -> AgentOutput:
    """Validate the common JSON response envelope without coercing bad output.

    ``output_key`` names a top-level field. Optional fields are ``trajectory``
    (a list of tool names), ``usage`` (nonnegative numeric counters), ``cost_usd`` (a finite,
    nonnegative number), ``metadata`` (an object), and ``events`` (JSON objects).
    """
    if not isinstance(payload, dict):
        raise AdapterError("agent response must be a JSON object")
    output = payload.get(output_key)
    if not isinstance(output, str):
        raise AdapterError(f"agent response field {output_key!r} must be a string")
    trajectory = payload.get("trajectory")
    if trajectory is not None and (
        not isinstance(trajectory, list) or any(not isinstance(step, str) for step in trajectory)
    ):
        raise AdapterError("agent response trajectory must be a list of strings or null")
    usage = payload.get("usage", {})
    metadata = payload.get("metadata", {})
    if not isinstance(usage, dict) or not isinstance(metadata, dict):
        raise AdapterError("agent response usage and metadata must be objects")
    if any(not isinstance(key, str) or not key or not _nonnegative_number(value) for key, value in usage.items()):
        raise AdapterError("agent response usage must map nonempty names to finite nonnegative numbers")
    cost = payload.get("cost_usd")
    events = payload.get("events", [])
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise AdapterError("agent response events must be a list of JSON objects")
    if cost is not None and not _nonnegative_number(cost):
        raise AdapterError("agent response cost_usd must be a finite nonnegative number or null")
    try:
        if not _string_object_keys(metadata):
            raise ValueError("metadata object keys must be strings")
        validate_json_value(events, "events")
        json.dumps({"usage": usage, "metadata": metadata}, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise AdapterError("agent response usage, metadata and events must contain valid JSON values") from None
    return AgentOutput(
        output=output,
        trajectory=trajectory,
        usage=usage,
        cost_usd=float(cost) if cost is not None else None,
        metadata=metadata,
        events=events,
    )


def _json(text: str, source: str) -> Any:
    try:
        return json.loads(text, parse_constant=lambda _: _invalid_constant())
    except (ValueError, RecursionError):
        # Never include raw agent output, prompts, URLs, or environment values
        # in exceptions: case errors are persisted in evaluation reports.
        raise AdapterError(f"{source} returned invalid JSON") from None


def _invalid_constant() -> None:
    raise ValueError("nonfinite JSON number")


def _settings(timeout: float, max_output_bytes: int) -> None:
    if not _nonnegative_number(timeout) or timeout == 0:
        raise ValueError("timeout must be a finite positive number")
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes < 1:
        raise ValueError("max_output_bytes must be a positive integer")


def _argv(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)) or not isinstance(command, Sequence) or not command:
        raise ValueError("command must be a nonempty sequence of arguments, not a shell string")
    if any(not isinstance(arg, str) or "\0" in arg for arg in command) or not command[0]:
        raise ValueError("command arguments must be strings without NUL bytes")
    return tuple(command)


def _environment(overrides: Mapping[str, str] | None) -> dict[str, str] | None:
    if not _IS_WINDOWS:
        return {**os.environ, **overrides} if overrides is not None else None
    # Windows environment names are case-insensitive. Avoid passing both
    # inherited PATH and an override named Path to CreateProcess.
    environment = {key.upper(): value for key, value in os.environ.items()}
    if overrides is not None:
        environment.update({key.upper(): value for key, value in overrides.items()})
    return environment


def _resolve_windows_executable(command: str, cwd: str | Path | None, environment: Mapping[str, str]) -> str:
    """Resolve Windows argv[0] before CreateProcess, including npm .cmd shims.

    Relative paths and PATH entries are relative to the invocation's cwd.
    Extensionless npm POSIX scripts are not preferred over PATHEXT matches.
    """
    base = ntpath.abspath(os.fspath(cwd) if cwd is not None else os.getcwd())
    directory, name = ntpath.split(command)
    if directory:
        directories = [ntpath.abspath(ntpath.join(base, directory))]
    else:
        directories = [] if "NODEFAULTCURRENTDIRECTORYINEXEPATH" in environment else [base]
        directories.extend(
            ntpath.abspath(ntpath.join(base, part.strip('"')))
            for part in environment.get("PATH", "").split(";") if part
        )
    extensions = [ext for ext in environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if ext]
    names = [name] if ntpath.splitext(name.rstrip(" ."))[1] else [name + ext for ext in extensions]
    seen = set()
    for directory in directories:
        for filename in names:
            candidate = ntpath.normpath(ntpath.join(directory, filename))
            folded = ntpath.normcase(candidate)
            if folded not in seen and os.path.isfile(candidate):
                return candidate
            seen.add(folded)
    raise AdapterError("could not start agent command; check executable, working directory, and PATH/PATHEXT")


def _windows_cmd_executable() -> str:
    # Do not use PATH or a user-overridden COMSPEC to locate the interpreter.
    # The system directory comes from Windows itself.
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise AdapterError("could not locate the Windows command interpreter")
    return ntpath.join(buffer.value, "cmd.exe")


def _windows_command(
    command: Sequence[str], *, cwd: str | Path | None,
    environment: Mapping[str, str], has_input_placeholder: bool = False,
) -> tuple[list[str] | str, str]:
    """Prepare native argv or a restricted command line for a batch shim."""
    executable = _resolve_windows_executable(command[0], cwd, environment)
    resolved = [executable, *command[1:]]
    # Win32 may ignore trailing dots/spaces in filenames. Do not allow that
    # spelling to bypass the batch-file safeguards.
    if ntpath.splitext(executable.rstrip(" ."))[1].lower() not in {".bat", ".cmd"}:
        return resolved, executable
    if has_input_placeholder:
        raise AdapterError("Windows batch agents do not support {input} arguments; send the prompt on stdin or use a native executable")
    forbidden = '"%&|<>^!()'
    if any(any(char in forbidden or ord(char) < 32 or ord(char) == 127 for char in arg) for arg in resolved):
        raise AdapterError("Windows batch agent paths and arguments cannot contain shell metacharacters or control characters; use stdin or a native executable")
    interpreter = _windows_cmd_executable()
    # cmd /s removes the outer quote pair after /c. Quote the batch path even
    # when it has no spaces, and use CRT-compatible quoting for the remaining
    # safe arguments so common npm shims can forward them through %*.
    inner = f'"{executable}"'
    if len(resolved) > 1:
        inner += " " + subprocess.list2cmdline(resolved[1:])
    command_line = f'"{interpreter}" /d /v:off /s /c "{inner}"'
    return command_line, interpreter


def _kill(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            # A child can outlive its parent while still holding stdout open.
            # Kill the whole invocation's group even if the parent has exited.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.kill()


def _execute(
    command: Sequence[str],
    input: str,
    *,
    cwd: str | Path | None,
    timeout: float,
    max_output_bytes: int,
    env: Mapping[str, str] | None = None,
    has_input_placeholder: bool = False,
) -> str:
    """Capture pipes with bounded memory and a deadline, including pipe drain.

    Readers drain concurrently so a full stderr pipe cannot deadlock stdout.
    The writer is separate too: an agent that never reads stdin still times
    out. On POSIX each invocation owns a process group for descendant cleanup.
    """
    if not isinstance(input, str):
        raise TypeError("agent input must be a string")
    try:
        input_bytes = input.encode("utf-8")
    except UnicodeEncodeError:
        raise AdapterError("agent input must be valid UTF-8 text") from None
    environment = _environment(env)
    executable = None
    if _IS_WINDOWS:
        command, executable = _windows_command(
            command, cwd=cwd, environment=environment,
            has_input_placeholder=has_input_placeholder,
        )
    try:
        process = subprocess.Popen(
            command,
            executable=executable,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=environment,
            start_new_session=os.name == "posix",
            bufsize=0,
        )
    except (OSError, ValueError):
        raise AdapterError("could not start agent command; check executable, working directory, and environment") from None

    buffers = [bytearray(), bytearray()]
    exceeded = threading.Event()
    reader_failed = threading.Event()

    def read_pipe(stream: Any, buffer: bytearray) -> None:
        try:
            while chunk := stream.read(65536):
                available = max_output_bytes - len(buffer)
                buffer.extend(chunk[:available])
                if len(chunk) > available:
                    exceeded.set()
                    break
        except (OSError, ValueError):
            reader_failed.set()
        finally:
            stream.close()

    def write_input() -> None:
        try:
            offset = 0
            while offset < len(input_bytes):
                written = process.stdin.write(input_bytes[offset : offset + 65536])
                if written is None:
                    break
                offset += written
        except (BrokenPipeError, OSError, ValueError):
            pass  # The process may intentionally exit without reading input.
        finally:
            process.stdin.close()

    readers = [
        threading.Thread(target=read_pipe, args=(process.stdout, buffers[0]), daemon=True),
        threading.Thread(target=read_pipe, args=(process.stderr, buffers[1]), daemon=True),
    ]
    writer = threading.Thread(target=write_input, daemon=True)
    deadline = time.monotonic() + timeout
    for thread in [*readers, writer]:
        thread.start()
    try:
        while True:
            if exceeded.is_set():
                raise AdapterError(f"agent command exceeded the {max_output_bytes}-byte output limit")
            if reader_failed.is_set():
                raise AdapterError("could not read agent command output")
            if process.poll() is not None and not any(thread.is_alive() for thread in readers):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterError(f"agent command timed out after {timeout:g}s")
            exceeded.wait(min(remaining, 0.01))
        # A reader can set either flag and finish between the checks above
        # and the completion check. Never accept a truncated successful run.
        if exceeded.is_set():
            raise AdapterError(f"agent command exceeded the {max_output_bytes}-byte output limit")
        if reader_failed.is_set():
            raise AdapterError("could not read agent command output")
        if process.returncode:
            raise AdapterError(f"agent command exited with status {process.returncode}; command output omitted")
        try:
            return buffers[0].decode("utf-8")
        except UnicodeDecodeError:
            raise AdapterError("agent command stdout must be UTF-8 text") from None
    finally:
        # Also reap descendants after success: a command must not leave agents
        # running between eval cases after closing its output pipes.
        original_error = sys.exception()
        cleanup_failed = False
        try:
            _kill(process)
        except OSError:
            cleanup_failed = True
        # If termination failed, waiting unboundedly would defeat the run's
        # timeout. Surface cleanup failure without hiding the primary error.
        try:
            process.wait(timeout=0.2 if cleanup_failed else None)
        except subprocess.TimeoutExpired:
            cleanup_failed = True
        for thread in [*readers, writer]:
            thread.join(timeout=0.2)
        if cleanup_failed:
            detail = "agent process cleanup failed; a process may remain running"
            if isinstance(original_error, Exception):
                raise AdapterError(f"{original_error}; {detail}") from original_error
            if original_error is None:
                raise AdapterError(detail)
            original_error.add_note(detail)


class FunctionRunner:
    """Wrap a sync or async callable returning text, AgentOutput, or an envelope.

    The callable must be safe to share when evaluation concurrency exceeds one.
    Async callables run in a fresh event loop per invocation. Call ``run`` from
    outside an already-running event loop (or from a worker thread).
    """

    def __init__(self, function: Callable[[str], Any]):
        if not callable(function):
            raise ValueError("function must be callable")
        self.function = function

    def run(self, input: str) -> AgentOutput:
        if not isinstance(input, str):
            raise TypeError("agent input must be a string")
        result = self.function(input)
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                async def await_result() -> Any:
                    return await result

                result = asyncio.run(await_result())
            else:
                if inspect.iscoroutine(result):
                    result.close()
                raise AdapterError("run async FunctionRunner outside an active event loop or in a worker thread")
        if isinstance(result, AgentOutput):
            return agent_output_from_payload({
                "output": result.output,
                "trajectory": result.trajectory,
                "usage": result.usage,
                "cost_usd": result.cost_usd,
                "metadata": result.metadata,
                "events": result.events,
            })
        if isinstance(result, str):
            return AgentOutput(output=result)
        return agent_output_from_payload(result)


class CommandRunner:
    """Run argv directly. Send input on stdin unless argv contains ``{input}``.

    A placeholder is replaced literally within a native executable's argument.
    Windows .bat/.cmd shims require stdin input and restricted configuration
    arguments. Text output retains whitespace; JSON output uses the common
    response envelope. stdout and stderr are each limited to max_output_bytes.
    Environment overrides augment the current environment and are never logged.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout: float = 120,
        output_format: str = "text",
        output_key: str = "output",
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        env: Mapping[str, str] | None = None,
    ):
        self.command = _argv(command)
        _settings(timeout, max_output_bytes)
        if output_format not in {"text", "json"}:
            raise ValueError("output_format must be 'text' or 'json'")
        if not isinstance(output_key, str) or not output_key:
            raise ValueError("output_key must be a nonempty string")
        if env is not None and (
            not isinstance(env, Mapping)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items())
        ):
            raise ValueError("env must map environment names to strings")
        self.cwd, self.timeout = cwd, timeout
        self.output_format, self.output_key = output_format, output_key
        self.max_output_bytes = max_output_bytes
        self.env = dict(env) if env is not None else None

    def run(self, input: str) -> AgentOutput:
        if not isinstance(input, str):
            raise TypeError("agent input must be a string")
        has_placeholder = any("{input}" in arg for arg in self.command)
        command = [arg.replace("{input}", input) for arg in self.command]
        stdout = _execute(
            command, "" if has_placeholder else input,
            cwd=self.cwd, timeout=self.timeout,
            max_output_bytes=self.max_output_bytes, env=self.env,
            has_input_placeholder=has_placeholder,
        )
        if self.output_format == "json":
            return agent_output_from_payload(_json(stdout, "agent command"), output_key=self.output_key)
        return AgentOutput(output=stdout)


class CodexRunner:
    """Use an installed, authenticated Codex CLI with a fresh read-only run.

    Uses ``codex exec --json --ephemeral --sandbox read-only -``. The final
    agent message and completed turn are required; intermediate text is not
    mistaken for an answer. Unknown future event types are ignored.
    """

    def __init__(
        self, *, cwd: str | Path | None = None, timeout: float = 300,
        model: str | None = None, executable: str = "codex",
        extra_args: Sequence[str] = (), max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ):
        _settings(timeout, max_output_bytes)
        if isinstance(extra_args, (str, bytes)):
            raise ValueError("extra_args must be a sequence of arguments, not a shell string")
        self.command = _argv([
            executable, "exec", "--json", "--ephemeral", "--sandbox", "read-only",
            *(["--model", model] if model else []), *extra_args, "-",
        ])
        self.cwd, self.timeout, self.max_output_bytes = cwd, timeout, max_output_bytes

    def run(self, input: str) -> AgentOutput:
        stdout = _execute(self.command, input, cwd=self.cwd, timeout=self.timeout, max_output_bytes=self.max_output_bytes)
        final = None
        completed = False
        usage: dict = {}
        trajectory: list[str] = []
        events: list[dict] = []
        metadata = {"adapter": "codex"}
        provider_errors = 0
        for line in stdout.splitlines():
            if not line.strip():
                continue
            event = _json(line, "Codex")
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise AdapterError("Codex JSONL events must be objects with a type")
            kind = event["type"]
            if kind == "turn.failed":
                raise AdapterError("Codex reported a failed turn; provider details omitted")
            if kind == "error":
                events.append({"type": "error", "details_omitted": True})
                # Codex also emits this event for retryable stream errors.
                # Completion after the error establishes recovery. A final
                # message from this unfinished turn remains valid, but an
                # already-completed turn's answer cannot be reused.
                provider_errors += 1
                if completed:
                    final = None
                completed, usage = False, {}
            if kind == "turn.started":
                final, completed, usage = None, False, {}
            if kind == "thread.started" and isinstance(event.get("thread_id"), str):
                metadata["thread_id"] = event["thread_id"]
            if kind == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict):
                    raise AdapterError("Codex item.completed must contain an item object")
                item_type = item.get("type")
                if not isinstance(item_type, str) or not item_type:
                    raise AdapterError("Codex completed item must contain a string type")
                if item_type == "agent_message":
                    if not isinstance(item.get("text"), str):
                        raise AdapterError("Codex agent_message must contain text")
                    final = item["text"]
                elif item_type == "mcp_tool_call":
                    tool = item.get("tool")
                    trajectory.append(tool if isinstance(tool, str) else "mcp_tool_call")
                    events.append(event)
                elif item_type in {"command_execution", "web_search", "file_change"}:
                    trajectory.append(item_type)
                    events.append(event)
            if kind == "turn.completed":
                completed = True
                usage = event.get("usage", {})
        if final is None or not completed:
            if provider_errors:
                raise AdapterError("Codex reported a failed turn without a subsequent completed response; provider details omitted")
            raise AdapterError("Codex response is incomplete: final agent message and completed turn required")
        if provider_errors:
            metadata["provider_error_events"] = provider_errors
        return agent_output_from_payload({
            "output": final, "trajectory": trajectory, "usage": usage, "metadata": metadata, "events": events,
        })


class ClaudeCodeRunner:
    """Use installed Claude Code in print mode without interactive approvals.

    Uses ``claude -p --output-format json --permission-mode dontAsk``. Existing
    Claude Code tool permissions still apply; no tools are broadly enabled.
    Claude's input_tokens excludes cached input, so total_tokens includes
    input, cache creation, cache reads, and output exactly once. The original
    provider usage object is preserved in metadata.provider_usage.
    """

    def __init__(
        self, *, cwd: str | Path | None = None, timeout: float = 300,
        model: str | None = None, executable: str = "claude",
        extra_args: Sequence[str] = (), max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ):
        _settings(timeout, max_output_bytes)
        if isinstance(extra_args, (str, bytes)):
            raise ValueError("extra_args must be a sequence of arguments, not a shell string")
        self.command = _argv([
            executable, "-p", "--output-format", "json", "--permission-mode", "dontAsk",
            *(["--model", model] if model else []), *extra_args,
        ])
        self.cwd, self.timeout, self.max_output_bytes = cwd, timeout, max_output_bytes

    def run(self, input: str) -> AgentOutput:
        stdout = _execute(self.command, input, cwd=self.cwd, timeout=self.timeout, max_output_bytes=self.max_output_bytes)
        payload = _json(stdout, "Claude Code")
        if not isinstance(payload, dict) or payload.get("type") != "result":
            raise AdapterError("Claude Code response must be a result object")
        if payload.get("is_error") is not False or payload.get("subtype") != "success":
            raise AdapterError("Claude Code reported an unsuccessful result; provider details omitted")
        metadata = {"adapter": "claude-code"}
        if isinstance(payload.get("session_id"), str):
            metadata["session_id"] = payload["session_id"]
        provider_usage = payload.get("usage", {})
        if not isinstance(provider_usage, dict):
            raise AdapterError("Claude Code usage must be an object")
        usage = {}
        token_counters = {"input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "total_tokens"}
        for key, value in provider_usage.items():
            if key in token_counters and not _nonnegative_number(value):
                raise AdapterError("Claude Code usage counters must be finite nonnegative numbers")
            if isinstance(value, (float, int)):
                if not _nonnegative_number(value):
                    raise AdapterError("Claude Code usage counters must be finite nonnegative numbers")
                usage[key] = value
        if "total_tokens" not in usage and "input_tokens" in usage and "output_tokens" in usage:
            usage["total_tokens"] = sum(usage.get(key, 0) for key in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens",
            ))
        metadata["provider_usage"] = provider_usage
        return agent_output_from_payload({
            "output": payload.get("result"),
            "usage": usage,
            "cost_usd": payload.get("total_cost_usd"),
            "metadata": metadata,
        })


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib can forward Authorization when following redirects. A caller
        # should configure the final endpoint explicitly instead.
        raise AdapterError("HTTP agent redirects are disabled; use the final endpoint URL")


class HTTPRunner:
    """POST ``{"input": ...}`` to an existing HTTP agent's JSON endpoint.

    ``token_env`` is the name of a variable containing a bearer token, read at
    invocation time. Responses use the common envelope; ``output_key`` allows
    a different top-level output field. Timeout is the HTTP socket timeout.
    Redirects are rejected so authorization is never forwarded to another URL.
    """

    def __init__(
        self, url: str, *, output_key: str = "output", timeout: float = 120,
        token_env: str | None = None, headers: Mapping[str, str] | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ):
        _settings(timeout, max_output_bytes)
        try:
            parts = urlsplit(url)
            valid_url = parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password
        except (ValueError, TypeError):
            valid_url = False
        if not valid_url:
            raise ValueError("url must be an HTTP(S) endpoint without embedded credentials")
        if not isinstance(output_key, str) or not output_key:
            raise ValueError("output_key must be a nonempty string")
        if token_env is not None and (not isinstance(token_env, str) or not token_env):
            raise ValueError("token_env must be a nonempty environment variable name")
        if headers is not None and (
            not isinstance(headers, Mapping)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items())
        ):
            raise ValueError("headers must map header names to strings")
        self.url, self.output_key, self.timeout = url, output_key, timeout
        self.token_env, self.headers = token_env, dict(headers or {})
        self.max_output_bytes = max_output_bytes

    def run(self, input: str) -> AgentOutput:
        if not isinstance(input, str):
            raise TypeError("agent input must be a string")
        headers = {"Content-Type": "application/json", "Accept": "application/json", **self.headers}
        if self.token_env:
            token = os.environ.get(self.token_env)
            if not token:
                raise AdapterError("HTTP agent bearer token environment variable is missing or empty")
            headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
            headers["Authorization"] = f"Bearer {token}"
        try:
            request = Request(self.url, data=json.dumps({"input": input}).encode("utf-8"), headers=headers, method="POST")
            with build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response:
                body = response.read(self.max_output_bytes + 1)
        except HTTPError as exc:
            exc.close()
            raise AdapterError(f"HTTP agent returned status {exc.code}; response body omitted") from None
        except (TimeoutError, URLError, OSError, ValueError, HTTPException):
            raise AdapterError("HTTP agent request failed or timed out; check endpoint, authentication, and timeout") from None
        if len(body) > self.max_output_bytes:
            raise AdapterError(f"HTTP agent exceeded the {self.max_output_bytes}-byte response limit")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise AdapterError("HTTP agent response must be UTF-8 JSON") from None
        return agent_output_from_payload(_json(text, "HTTP agent"), output_key=self.output_key)
