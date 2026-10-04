"""Docker-backed repository tasks with separate agent and grader containers.

The Docker daemon and image are trusted. A container is useful process/resource
isolation, not a security boundary proven safe for hostile multi-tenant code.
"""
from __future__ import annotations

import difflib
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from harness.results import CaseResult, EvalResult, aggregate, validate_probability

MAX_LOG_BYTES = 1_048_576
MAX_FILE_BYTES = 8_388_608
MAX_WORKSPACE_BYTES = 33_554_432
MAX_ARCHIVE_BYTES = 50_331_648
MAX_ENTRIES = 4096
MAX_PATCH_BYTES = 1_048_576
LIMITS = {
    "memory_bytes": 536_870_912, "cpus": 1, "pids": 64,
    "workspace_tmpfs_bytes": 67_108_864, "temporary_tmpfs_bytes": 67_108_864,
    "home_tmpfs_bytes": 67_108_864,
    "log_bytes_per_stream": MAX_LOG_BYTES, "candidate_bytes": MAX_WORKSPACE_BYTES,
    "file_bytes": MAX_FILE_BYTES, "entries": MAX_ENTRIES,
}
_LABEL = "org.agent-eval-harness.run"


class CodingError(RuntimeError):
    """A bounded execution or workspace validation failed."""

    def __init__(self, message: str, *, phase: str = "infrastructure", stdout: bytes = b"", stderr: bytes = b""):
        super().__init__(message)
        self.phase, self.stdout, self.stderr = phase, stdout, stderr


@dataclass(frozen=True)
class _File:
    data: bytes
    executable: bool = False


@dataclass(frozen=True)
class _Snapshot:
    files: Mapping[str, _File] = field(default_factory=dict)
    directories: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        # Snapshots may be shared by concurrent tasks; retain no mutable inputs.
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))
        directories = set(self.directories)
        for name in self.files.keys() | directories:
            directories.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != ".")
        object.__setattr__(self, "directories", frozenset(directories))

    def manifest(self) -> list[dict]:
        entries = [{"path": path, "type": "directory"} for path in self.directories]
        entries.extend(
            {"path": path, "type": "file", "bytes": len(file.data), "sha256": hashlib.sha256(file.data).hexdigest(),
             "executable": file.executable}
            for path, file in self.files.items()
        )
        return sorted(entries, key=lambda entry: entry["path"])

    def archive(self, prefix: str = "", *, trusted: bool = False) -> bytes:
        result = io.BytesIO()
        with tarfile.open(fileobj=result, mode="w", format=tarfile.PAX_FORMAT) as archive:
            directories = set(self.directories)
            if prefix:
                directories.add("")
            for name in sorted(directories):
                member = tarfile.TarInfo(f"{prefix}/{name}".strip("/"))
                member.type, member.mode = tarfile.DIRTYPE, 0o555 if trusted else 0o755
                member.uid = member.gid = 0 if trusted else 65534
                archive.addfile(member)
            for name, file in sorted(self.files.items()):
                member = tarfile.TarInfo(f"{prefix}/{name}".strip("/"))
                member.size, member.mode = len(file.data), (0o555 if file.executable else 0o444) if trusted else (0o755 if file.executable else 0o644)
                member.uid = member.gid = 0 if trusted else 65534
                archive.addfile(member, io.BytesIO(file.data))
        return result.getvalue()


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or len(name.encode("utf-8")) > 240 or "\\" in name or "\0" in name:
        raise CodingError("workspace contains an unsupported path", phase="validation")
    raw = name.removeprefix("./").rstrip("/")
    if raw in ("", "."):
        return ""
    if raw.startswith("/") or any(part in ("", ".", "..") for part in raw.split("/")):
        raise CodingError("workspace contains an unsafe archive path", phase="validation")
    # Avoid Windows drive/alternate-stream paths when archives are inspected there.
    if ":" in raw or any(ord(char) < 32 for char in raw):
        raise CodingError("workspace contains an unsupported path", phase="validation")
    return raw


def _snapshot_directory(path: Path) -> _Snapshot:
    if path.is_symlink() or not path.is_dir():
        raise CodingError("workspace and grader must be real directories", phase="validation")
    files: dict[str, _File] = {}
    directories: set[str] = set()
    total, count = 0, 0
    pending = [path]
    while pending:
        directory = pending.pop()
        for entry in sorted(directory.iterdir()):
            name = _safe_name(entry.relative_to(path).as_posix())
            info = entry.lstat()
            mode = info.st_mode
            count += 1
            if count > MAX_ENTRIES:
                raise CodingError("workspace exceeds the entry limit", phase="validation")
            if stat.S_ISDIR(mode):
                directories.add(name)
                pending.append(entry)
            elif stat.S_ISREG(mode):
                if info.st_nlink > 1:
                    raise CodingError("workspace hard links are rejected", phase="validation")
                if info.st_size > MAX_FILE_BYTES:
                    raise CodingError("workspace file exceeds the byte limit", phase="validation")
                with entry.open("rb") as stream:
                    content = stream.read(MAX_FILE_BYTES + 1)
                total += len(content)
                if len(content) > MAX_FILE_BYTES or total > MAX_WORKSPACE_BYTES:
                    raise CodingError("workspace exceeds the byte limit", phase="validation")
                files[name] = _File(content, bool(mode & 0o111))
            else:
                raise CodingError("workspace may contain only regular files and directories; links are rejected", phase="validation")
    return _Snapshot(files, frozenset(directories))


def _snapshot_archive(data: bytes) -> _Snapshot:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise CodingError("candidate archive exceeds the byte limit", phase="validation")
    files: dict[str, _File] = {}
    directories: set[str] = set()
    seen: set[str] = set()
    total, count, root_seen = 0, 0, False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                name = _safe_name(member.name)
                if not name and member.isdir():
                    if root_seen:
                        raise CodingError("candidate contains duplicate root entries", phase="validation")
                    root_seen = True
                    continue
                count += 1
                if count > MAX_ENTRIES:
                    raise CodingError("candidate exceeds the entry limit", phase="validation")
                if not name or name in seen:
                    raise CodingError("candidate contains duplicate or empty paths", phase="validation")
                seen.add(name)
                if member.isdir():
                    directories.add(name)
                elif member.isfile() and not member.issparse():
                    total += member.size
                    if member.size < 0 or member.size > MAX_FILE_BYTES or total > MAX_WORKSPACE_BYTES:
                        raise CodingError("candidate exceeds file or workspace byte limits", phase="validation")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise CodingError("candidate archive cannot be read", phase="validation")
                    content = stream.read(MAX_FILE_BYTES + 1)
                    if len(content) != member.size:
                        raise CodingError("candidate archive contains an incomplete file", phase="validation")
                    files[name] = _File(content, bool(member.mode & 0o111))
                else:
                    raise CodingError("candidate links, devices, sparse files, and special entries are rejected", phase="validation")
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise CodingError("candidate archive is invalid", phase="validation") from exc
    for name in seen:
        if any(str(parent) in files for parent in PurePosixPath(name).parents):
            raise CodingError("candidate file conflicts with a directory path", phase="validation")
    snapshot = _Snapshot(files, frozenset(directories))
    if len(snapshot.files) + len(snapshot.directories) > MAX_ENTRIES:
        raise CodingError("candidate exceeds the entry limit", phase="validation")
    return snapshot


def _patch(before: _Snapshot, after: _Snapshot) -> tuple[bytes, bool]:
    output = bytearray()

    def append(value: str) -> bool:
        encoded = value.encode("utf-8")
        room = max(0, MAX_PATCH_BYTES - len(output))
        output.extend(encoded[:room])
        return len(encoded) > room

    for name in sorted(before.files.keys() | after.files.keys()):
        old, new = before.files.get(name), after.files.get(name)
        if old == new:
            continue
        if old is not None and new is not None and old.executable != new.executable:
            if append(f"File mode changed: {name}\n"):
                return bytes(output), True
        try:
            a = old.data.decode("utf-8").splitlines(keepends=True) if old else []
            b = new.data.decode("utf-8").splitlines(keepends=True) if new else []
        except UnicodeDecodeError:
            if append(f"Binary file changed: {name}\n"):
                return bytes(output), True
            continue
        # Avoid pathological quadratic diff behavior on a very large text file.
        if len(a) + len(b) > 4_000 or (old and len(old.data) > MAX_PATCH_BYTES) or (new and len(new.data) > MAX_PATCH_BYTES):
            if append(f"Large file changed; inspect candidate archive: {name}\n"):
                return bytes(output), True
            continue
        for line in difflib.unified_diff(a, b, fromfile=f"before/{name}" if old else "/dev/null", tofile=f"after/{name}" if new else "/dev/null"):
            if append(line):
                return bytes(output), True
    return bytes(output), False


@dataclass(frozen=True)
class _Completed:
    returncode: int
    stdout: bytes
    stderr: bytes


class _Docker:
    def __init__(self, executable: str):
        self.executable = executable
        self.cancelled = threading.Event()

    def call(self, args: Sequence[str], *, data: bytes = b"", timeout: float = 30,
             limit: int = MAX_LOG_BYTES, check: bool = True, phase: str = "infrastructure",
             cancellable: bool = True) -> _Completed:
        try:
            process = subprocess.Popen([self.executable, *args], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except (OSError, ValueError) as exc:
            raise CodingError("could not start Docker; check the executable and daemon", phase=phase) from exc
        buffers = [bytearray(), bytearray()]
        exceeded, failed = threading.Event(), threading.Event()

        def read(stream: Any, buffer: bytearray, bound: int) -> None:
            try:
                while chunk := stream.read(65_536):
                    room = bound - len(buffer)
                    buffer.extend(chunk[:room])
                    if len(chunk) > room:
                        exceeded.set()
                        break
            except (OSError, ValueError):
                failed.set()
            finally:
                stream.close()

        def write() -> None:
            try:
                offset = 0
                while offset < len(data):
                    written = process.stdin.write(data[offset:offset + 65_536])
                    if not written:
                        break
                    offset += written
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                process.stdin.close()

        readers = [threading.Thread(target=read, args=(process.stdout, buffers[0], limit), daemon=True),
                   threading.Thread(target=read, args=(process.stderr, buffers[1], MAX_LOG_BYTES), daemon=True)]
        writer = threading.Thread(target=write, daemon=True)
        deadline = time.monotonic() + timeout
        for thread in [*readers, writer]:
            thread.start()
        reason = None
        try:
            while True:
                if cancellable and self.cancelled.is_set():
                    reason = "coding evaluation interrupted"
                    break
                if exceeded.is_set():
                    reason = "Docker operation exceeded the output byte limit"
                    break
                if failed.is_set():
                    reason = "could not read Docker operation output"
                    break
                if process.poll() is not None and not any(thread.is_alive() for thread in readers):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = f"Docker operation timed out after {timeout:g}s"
                    break
                exceeded.wait(min(0.01, remaining))
            if exceeded.is_set():
                reason = "Docker operation exceeded the output byte limit"
            if failed.is_set():
                reason = "could not read Docker operation output"
            if reason is not None:
                raise CodingError(reason, phase=phase, stdout=bytes(buffers[0]), stderr=bytes(buffers[1]))
            completed = _Completed(process.returncode, bytes(buffers[0]), bytes(buffers[1]))
            if check and completed.returncode:
                raise CodingError(f"Docker operation failed with status {completed.returncode}; inspect local logs", phase=phase,
                                  stdout=completed.stdout, stderr=completed.stderr)
            return completed
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise CodingError("Docker client cleanup failed; check the daemon", phase="infrastructure") from exc
            for thread in [*readers, writer]:
                thread.join(timeout=0.2)

    def image(self, reference: str) -> dict:
        response = self.call(["image", "inspect", "--format", "{{json .}}", reference])
        try:
            image = json.loads(response.stdout)
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image["Id"]):
                raise ValueError("invalid image id")
            if image.get("Os") != "linux":
                raise CodingError("coding evaluation requires a Linux container image")
            if image.get("Config", {}).get("Volumes"):
                raise CodingError("images declaring Docker VOLUME mounts are unsupported; use a volume-free image")
            return {"id": image["Id"], "repo_digests": image.get("RepoDigests") or [],
                    "os": image["Os"], "architecture": image.get("Architecture")}
        except (ValueError, TypeError, KeyError) as exc:
            raise CodingError("Docker returned invalid image metadata") from exc

    def create(self, name: str, image_id: str, network: str, run_id: str, *, grader: bool = False) -> None:
        volumes = [(name + "-workspace", "size=64m,nosuid,nodev,mode=0770,uid=65534,gid=65534")]
        if grader:
            volumes.append((name + "-grader", "size=64m,nosuid,nodev,mode=0755,uid=0,gid=0"))
        for volume, options in volumes:
            self.call(["volume", "create", "--driver", "local", "--label", f"{_LABEL}={run_id}",
                       "--opt", "type=tmpfs", "--opt", "device=tmpfs", "--opt", f"o={options}", volume])
        args = ["create", "--name", name, "--label", f"{_LABEL}={run_id}", "--network", network,
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "64", "--memory", "512m", "--memory-swap", "512m", "--cpus", "1",
                "--user", "65534:65534", "--no-healthcheck", "--env", "HOME=/home/agent",
                "--tmpfs", "/home/agent:rw,nosuid,nodev,size=64m,mode=0700,uid=65534,gid=65534",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                "--mount", f"type=volume,src={name}-workspace,dst=/workspace,volume-nocopy",
                "--workdir", "/workspace", "--entrypoint", "/bin/sh"]
        if grader:
            args.extend(["--mount", f"type=volume,src={name}-grader,dst=/grader,volume-nocopy"])
        args.extend([image_id, "-c", "while :; do sleep 3600; done"])
        self.call(args)
        self.call(["start", name])

    def copy_in(self, name: str, snapshot: _Snapshot, target: str, *, trusted: bool = False) -> None:
        self.call(["cp", "--archive", "-", f"{name}:{target}/"], data=snapshot.archive(trusted=trusted))

    def remove(self, name: str) -> None:
        failures = []
        try:
            result = self.call(["rm", "--force", "--volumes", name], check=False, timeout=10, cancellable=False)
            if result.returncode and b"No such container" not in result.stderr:
                failures.append(name)
        except CodingError:
            failures.append(name)
        # Explicitly remove our named tmpfs volumes, including partial creates.
        # Do not prune or touch resources that do not belong to this task.
        for volume in (name + "-workspace", name + "-grader"):
            try:
                result = self.call(["volume", "rm", volume], check=False, timeout=10, cancellable=False)
                if result.returncode and b"no such volume" not in result.stderr.lower():
                    failures.append(volume)
            except CodingError:
                failures.append(volume)
        if failures:
            raise CodingError("container or volume cleanup failed; check Docker resources: " + ", ".join(failures))


def _argv(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)) or not isinstance(command, Sequence) or not command:
        raise ValueError("command must be a nonempty argv sequence")
    if any(not isinstance(arg, str) or "\0" in arg for arg in command) or not command[0]:
        raise ValueError("command arguments must be strings without NUL bytes")
    return tuple(command)


def _positive(value: float, name: str) -> float:
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def run_coding_eval(pack: Any, command: Sequence[str], *, image: str | None = None,
                    timeout: float = 300, concurrency: int = 1, network: str = "none",
                    env: Mapping[str, str] | None = None, artifacts_dir: str | Path,
                    pass_threshold: float = 1.0) -> EvalResult:
    """Evaluate repository edits against independent, freshly provisioned graders.

    ``command`` executes in /workspace and reads the task prompt on stdin. Only
    explicitly selected ``env`` values reach the agent, never the grader. A
    nonzero grader test exit gives tests=0; timeouts, Docker failures, unsafe
    candidate files, and unavailable executables are evaluation errors.
    """
    command = _argv(command)
    timeout = _positive(timeout, "timeout")
    pass_threshold = validate_probability(pass_threshold, "pass_threshold")
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    if network not in ("none", "bridge"):
        raise ValueError("network must be none or bridge")
    if getattr(pack, "kind", None) != "coding" or not pack.tasks:
        raise ValueError("a nonempty coding pack is required")
    reference = image or pack.image
    if not isinstance(reference, str) or not reference or reference.startswith("-") or any(char.isspace() for char in reference) or "\0" in reference:
        raise ValueError("a valid local Docker image reference is required")
    if env is not None and not isinstance(env, Mapping):
        raise ValueError("environment must be a mapping of variable names to values")
    env = dict(env or {})
    if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
           or not isinstance(value, str) or any(char in value for char in "\r\n\0") for key, value in env.items()):
        raise ValueError("environment requires variable names and single-line string values")
    tasks = list(pack.tasks)
    if len({task.id for task in tasks}) != len(tasks) or any(not isinstance(task.id, str) or not task.id.strip() for task in tasks):
        raise ValueError("coding task ids must be unique nonempty strings")
    snapshots: dict[Path, _Snapshot] = {}

    def prepare_snapshot(path: Path) -> _Snapshot:
        # Preserve root-link rejection even when its target is already cached.
        if path.is_symlink():
            raise CodingError("workspace and grader must be real directories", phase="validation")
        canonical = path.resolve()
        if canonical not in snapshots:
            snapshots[canonical] = _snapshot_directory(path)
        return snapshots[canonical]

    prepared = []
    for task in tasks:
        if not isinstance(task.input, str) or not task.input or len(task.input.encode("utf-8")) > MAX_LOG_BYTES:
            raise ValueError("task prompts must be nonempty and at most 1 MiB")
        _argv(task.command)
        _positive(task.timeout, "grader timeout")
        prepared.append((task, prepare_snapshot(Path(task.workspace)), prepare_snapshot(Path(task.grader))))
    executable = shutil.which("docker")
    if executable is None:
        raise CodingError("Docker is not installed; install Docker and start its daemon")
    docker = _Docker(executable)
    image_info = docker.image(reference)  # Fail before creating any task artifacts/containers.
    run_token = uuid.uuid4().hex
    destination = Path(artifacts_dir).resolve() / run_token
    destination.mkdir(parents=True, exist_ok=False)
    os.chmod(destination, 0o700)

    def run_one(item: tuple[int, tuple[Any, _Snapshot, _Snapshot]]) -> CaseResult:
        index, (task, initial, grader) = item
        started, agent_started, agent_ms = time.perf_counter(), None, None
        grader_started = None
        case_dir = destination / f"{index:04d}-{hashlib.sha256(task.id.encode()).hexdigest()[:10]}"
        case_dir.mkdir(mode=0o700)
        names: list[str] = []
        logs: dict[str, bytes] = {}
        output, error, error_stage, score = "", None, None, 0.0
        phase = "infrastructure"
        metadata = {"evaluation_kind": "coding", "artifacts": str(case_dir), "image_id": image_info["id"],
                    "workspace_manifest": initial.manifest(), "grader_manifest": grader.manifest(),
                    "grader_command": list(task.command), "grader_timeout_seconds": task.timeout,
                    "agent_timeout_seconds": timeout, "agent_network": network, "grader_network": "none"}
        try:
            agent_name = f"agent-eval-{run_token[:16]}-{index}-agent"
            names.append(agent_name)  # register before create; failed create/start is cleaned too
            docker.create(agent_name, image_info["id"], network, run_token)
            docker.copy_in(agent_name, initial, "/workspace")
            phase, agent_started = "agent", time.perf_counter()
            with tempfile.TemporaryDirectory(prefix="agent-eval-env-") as temporary:
                args = ["exec", "--interactive"]
                if env:
                    env_path = Path(temporary) / "environment"
                    descriptor = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                        stream.write("".join(f"{key}={value}\n" for key, value in env.items()))
                    args.extend(["--env-file", str(env_path)])
                response = docker.call([*args, agent_name, *command], data=task.input.encode("utf-8"), timeout=timeout, check=False, phase="agent")
            agent_ms = (time.perf_counter() - agent_started) * 1000
            logs.update({"agent.stdout.log": response.stdout, "agent.stderr.log": response.stderr})
            output = response.stdout.decode("utf-8", errors="replace")
            metadata["agent_exit_code"] = response.returncode
            if response.returncode:
                raise CodingError(f"agent command exited with status {response.returncode}", phase="agent")
            phase = "infrastructure"
            docker.call(["pause", agent_name])  # freeze descendants before snapshotting the result
            transferred = docker.call(["cp", f"{agent_name}:/workspace/.", "-"], limit=MAX_ARCHIVE_BYTES)
            candidate = _snapshot_archive(transferred.stdout)
            docker.remove(agent_name)
            names.remove(agent_name)
            candidate_archive = candidate.archive()
            (case_dir / "candidate.tar").write_bytes(candidate_archive)
            (case_dir / "candidate-manifest.json").write_text(json.dumps(candidate.manifest(), indent=2), encoding="utf-8")
            patch, truncated = _patch(initial, candidate)
            (case_dir / "patch.diff").write_bytes(patch)
            metadata.update(candidate_sha256=hashlib.sha256(candidate_archive).hexdigest(),
                            candidate_manifest=candidate.manifest(), patch_truncated=truncated)
            grader_name = f"agent-eval-{run_token[:16]}-{index}-grader"
            names.append(grader_name)
            docker.create(grader_name, image_info["id"], "none", run_token, grader=True)
            docker.copy_in(grader_name, candidate, "/workspace")
            docker.copy_in(grader_name, grader, "/grader", trusted=True)
            phase, grader_started = "grader", time.perf_counter()
            response = docker.call(["exec", grader_name, *task.command], timeout=task.timeout, check=False, phase="grader")
            logs.update({"grader.stdout.log": response.stdout, "grader.stderr.log": response.stderr})
            metadata["grader_exit_code"] = response.returncode
            metadata["grader_latency_ms"] = (time.perf_counter() - grader_started) * 1000
            if response.returncode in (125, 126, 127, 137):
                raise CodingError(f"grader could not complete (status {response.returncode})", phase="grader")
            score = float(response.returncode == 0)
        except (CodingError, OSError, ValueError) as exc:
            error = str(exc) if isinstance(exc, CodingError) else f"coding evaluation failed during {phase}"
            failure_phase = exc.phase if isinstance(exc, CodingError) else phase
            error_stage = "agent" if failure_phase == "agent" else "validation" if failure_phase == "validation" else "scorer"
            metadata["coding_error_phase"] = failure_phase
            if isinstance(exc, CodingError) and (exc.stdout or exc.stderr):
                stream_prefix = "agent" if failure_phase == "agent" else "grader" if failure_phase == "grader" else "infrastructure"
                logs[f"{stream_prefix}.stdout.log"] = exc.stdout
                logs[f"{stream_prefix}.stderr.log"] = exc.stderr
                if failure_phase == "agent":
                    output = exc.stdout.decode("utf-8", errors="replace")
            if agent_started is not None and agent_ms is None:
                agent_ms = (time.perf_counter() - agent_started) * 1000
            if grader_started is not None and "grader_latency_ms" not in metadata:
                metadata["grader_latency_ms"] = (time.perf_counter() - grader_started) * 1000
        finally:
            cleanup_errors = []
            for name in reversed(names):
                try:
                    docker.remove(name)
                except CodingError as exc:
                    cleanup_errors.append(str(exc))
            if cleanup_errors:
                error = "; ".join([*([error] if error else []), *cleanup_errors])
                error_stage, metadata["coding_error_phase"] = "scorer", "infrastructure"
            for filename, content in logs.items():
                (case_dir / filename).write_bytes(content)
            (case_dir / "evidence.json").write_text(json.dumps(metadata, indent=2, allow_nan=False), encoding="utf-8")
        return CaseResult(case_id=task.id, input=task.input, expected_output="all independent tests pass",
                          output=output, scores={"tests": score}, error=error, error_stage=error_stage,
                          latency_ms=(time.perf_counter() - started) * 1000, agent_latency_ms=agent_ms,
                          tags=list(task.tags), metadata=metadata,
                          events=[{"type": "coding_evaluation", "artifacts": str(case_dir),
                                   "agent_exit_code": metadata.get("agent_exit_code"),
                                   "grader_exit_code": metadata.get("grader_exit_code")}])

    executor = ThreadPoolExecutor(max_workers=concurrency)
    try:
        futures = [executor.submit(run_one, item) for item in enumerate(prepared)]
        cases = [future.result() for future in futures]
    except BaseException:
        docker.cancelled.set()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    finally:
        executor.shutdown(wait=True)
    metadata = {"evaluation_kind": "coding", "pack_id": pack.id, "pack_version": pack.version,
                "pack_fingerprint": pack.fingerprint, "image": image_info, "concurrency": concurrency,
                "agent_network": network, "grader_network": "none", "environment_names": sorted(env),
                "command_sha256": hashlib.sha256(json.dumps(list(command), ensure_ascii=True, separators=(",", ":")).encode()).hexdigest(),
                "command_argument_count": len(command),
                "artifacts": str(destination), "container_run_label": run_token, "limits": dict(LIMITS),
                "grader_config": [{"name": "tests", "type": "harness.coding.held_out_tests",
                                   "settings": {"pack_fingerprint": pack.fingerprint, "image_id": image_info["id"],
                                                "grading": "exit_status", "limits": dict(LIMITS)},
                                   "reproducibility": "Retain exact pack, image, harness source, and invocation; grader command exit status defines correctness."}],
                "grading": "binary grader command exit status; zero passes; nonzero fails",
                "timing": "agent_latency_ms includes docker exec transport; latency_ms includes provisioning, grading, and cleanup",
                "telemetry": "generic coding commands do not report token use or cost; missing remains unknown"}
    result = aggregate(cases, pass_threshold=pass_threshold, dataset_sha=pack.fingerprint, metadata=metadata)
    result.save(destination / "result.json")
    return result
