"""Archive integrity tests plus opt-in, real Docker end-to-end evaluations."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import harness.coding as coding
from harness.coding import CodingError, _Docker, _File, _Snapshot, _patch, _snapshot_archive, _snapshot_directory, run_coding_eval
from harness.packs import load_pack


def _pack(tmp_path: Path, *, grader_source: str | None = None, tasks: int = 1):
    workspace, grader = tmp_path / "workspace", tmp_path / "grader"
    workspace.mkdir()
    grader.mkdir()
    (workspace / "solution.py").write_text("answer = 0\n", encoding="utf-8")
    (grader / "test.py").write_text(grader_source or (
        "import os, runpy\n"
        "assert 'AGENT_EVAL_TEST_SECRET' not in os.environ\n"
        "assert os.environ['HOME'] == '/home/agent'\n"
        "assert not os.path.exists('/home/agent/agent-config')\n"
        "assert runpy.run_path('/workspace/solution.py')['answer'] == 42\n"
        "try:\n"
        " open('/grader/test.py', 'w').write('bad')\n"
        " raise AssertionError('grader was writable')\n"
        "except PermissionError:\n"
        " pass\n"
        "print('independent grader passed')\n"
    ), encoding="utf-8")
    cases = tuple(SimpleNamespace(id=f"case-{i}", input="Set answer to 42", workspace=workspace, grader=grader,
                                  command=("python", "-I", "-B", "/grader/test.py"), tags=("smoke",), timeout=5.0)
                  for i in range(tasks))
    return SimpleNamespace(id="test-pack", version="1.0.0", kind="coding", tasks=cases, root=tmp_path,
                           image="python:3.12-slim", fingerprint="f" * 64)


def _tar(members: list[tuple[str, bytes | None, bytes | None]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content, kind in members:
            member = tarfile.TarInfo(name)
            if kind is not None:
                member.type = kind
            if member.issym() or member.islnk():
                member.linkname = "/etc/passwd"
            if content is not None:
                member.size = len(content)
            archive.addfile(member, io.BytesIO(content) if content is not None else None)
    return output.getvalue()


def test_snapshot_roundtrip_retains_bytes_empty_directories_and_exec(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "empty").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"hello\n")
    (tmp_path / "script").write_bytes(b"#!/bin/sh\n")
    (tmp_path / "script").chmod(0o755)
    original = _snapshot_directory(tmp_path)
    restored = _snapshot_archive(original.archive())
    assert restored == original
    manifest = {entry["path"]: entry for entry in restored.manifest()}
    assert manifest["src/a.py"]["sha256"]
    assert manifest["src/a.py"]["type"] == "file"
    assert manifest["src"] == {"path": "src", "type": "directory"}
    assert manifest["empty"] == {"path": "empty", "type": "directory"}
    assert original.archive() == original.archive()


def test_snapshot_is_immutable_and_does_not_retain_mutable_inputs():
    files, directories = {"a": _File(b"original")}, {"empty"}
    snapshot = _Snapshot(files, directories)
    files["a"] = _File(b"changed")
    directories.clear()
    assert snapshot.files["a"].data == b"original"
    assert snapshot.directories == {"empty"}
    with pytest.raises(TypeError):
        snapshot.files["a"] = _File(b"changed")
    with pytest.raises(AttributeError):
        snapshot.directories.add("another")


def test_snapshot_manifest_includes_implicit_parent_directories():
    snapshot = _Snapshot({"src/lib/a.py": _File(b"pass\n")}, {"fixtures/empty"})
    assert snapshot.directories == {"src", "src/lib", "fixtures", "fixtures/empty"}
    restored = _snapshot_archive(snapshot.archive())
    assert restored == snapshot
    assert restored.manifest() == snapshot.manifest()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../../x", "a\\x", "C:/x", "a:b", "a//b", "a/./b", "a\nx"])
def test_archive_rejects_escaping_and_unsupported_paths(name):
    with pytest.raises(CodingError):
        _snapshot_archive(_tar([(name, b"bad", None)]))


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_archive_rejects_links_and_special_files(kind):
    with pytest.raises(CodingError, match="links|special"):
        _snapshot_archive(_tar([("unsafe", None, kind)]))


def test_archive_rejects_duplicates_and_directory_collisions():
    for entries in [
        [("a", b"one", None), ("a", b"two", None)],
        [("a", b"one", None), ("a/b", b"two", None)],
        [("a/b", b"one", None), ("a", b"two", None)],
    ]:
        with pytest.raises(CodingError, match="duplicate|conflicts"):
            _snapshot_archive(_tar(entries))


def test_archive_rejects_truncation_and_excess_size(monkeypatch):
    with pytest.raises(CodingError, match="invalid"):
        _snapshot_archive(b"not a tar archive")
    monkeypatch.setattr(coding, "MAX_FILE_BYTES", 2)
    with pytest.raises(CodingError, match="limits"):
        _snapshot_archive(_tar([("a", b"123", None)]))
    monkeypatch.setattr(coding, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(CodingError, match="archive exceeds"):
        _snapshot_archive(b" " * 11)


def test_archive_counts_all_entries_and_total_bytes(monkeypatch):
    monkeypatch.setattr(coding, "MAX_ENTRIES", 1)
    with pytest.raises(CodingError, match="duplicate root"):
        _snapshot_archive(_tar([(".", None, tarfile.DIRTYPE), (".", None, tarfile.DIRTYPE)]))
    monkeypatch.setattr(coding, "MAX_ENTRIES", 10)
    monkeypatch.setattr(coding, "MAX_WORKSPACE_BYTES", 3)
    with pytest.raises(CodingError, match="limits"):
        _snapshot_archive(_tar([("a", b"12", None), ("b", b"34", None)]))


def test_archive_allows_one_root_beside_exact_workspace_entry_limit(monkeypatch):
    monkeypatch.setattr(coding, "MAX_ENTRIES", 2)
    entries = [(".", None, tarfile.DIRTYPE), ("empty", None, tarfile.DIRTYPE), ("a", b"data", None)]
    snapshot = _snapshot_archive(_tar(entries))
    assert snapshot.directories == {"empty"} and snapshot.files["a"].data == b"data"
    with pytest.raises(CodingError, match="entry limit"):
        _snapshot_archive(_tar([*entries, ("extra", b"", None)]))
    with pytest.raises(CodingError, match="duplicate root"):
        _snapshot_archive(_tar([entries[0], ("./", None, tarfile.DIRTYPE)]))


def test_archive_counts_implicit_parent_directories_toward_entry_limit(monkeypatch):
    monkeypatch.setattr(coding, "MAX_ENTRIES", 2)
    snapshot = _snapshot_archive(_tar([("src/a.py", b"pass\n", None)]))
    assert snapshot.directories == {"src"}
    with pytest.raises(CodingError, match="entry limit"):
        _snapshot_archive(_tar([("src/lib/a.py", b"pass\n", None)]))


def test_local_snapshot_rejects_symlinks(tmp_path):
    target = tmp_path / "actual"
    target.write_text("payload")
    try:
        (tmp_path / "link").symlink_to(target)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this host")
    with pytest.raises(CodingError, match="links"):
        _snapshot_directory(tmp_path)


def test_local_snapshot_size_and_entry_limits(tmp_path, monkeypatch):
    (tmp_path / "a").write_bytes(b"123")
    monkeypatch.setattr(coding, "MAX_FILE_BYTES", 2)
    with pytest.raises(CodingError, match="byte limit"):
        _snapshot_directory(tmp_path)
    monkeypatch.setattr(coding, "MAX_FILE_BYTES", 10)
    monkeypatch.setattr(coding, "MAX_ENTRIES", 0)
    with pytest.raises(CodingError, match="entry limit"):
        _snapshot_directory(tmp_path)


def test_trusted_archive_is_not_writable_by_agent():
    snapshot = _Snapshot({"tests/a.py": _File(b"assert True"), "check": _File(b"#!/bin/sh", True)}, {"tests"})
    with tarfile.open(fileobj=io.BytesIO(snapshot.archive(trusted=True))) as archive:
        for member in archive:
            assert member.uid == member.gid == 0
            assert member.mode & 0o222 == 0


def test_patch_marks_binary_mode_large_and_truncated(monkeypatch):
    old = _Snapshot({"a": _File(b"old\n"), "binary": _File(b"\xff"), "mode": _File(b"same\n")})
    new = _Snapshot({"a": _File(b"new\n"), "binary": _File(b"\xfe"), "mode": _File(b"same\n", True)})
    patch, truncated = _patch(old, new)
    assert b"-old" in patch and b"+new" in patch and b"Binary file changed" in patch and b"mode changed" in patch
    assert not truncated
    monkeypatch.setattr(coding, "MAX_PATCH_BYTES", 20)
    patch, truncated = _patch(old, new)
    assert len(patch) <= 20 and truncated


@pytest.mark.parametrize("kwargs", [
    {"command": "python"}, {"command": []}, {"command": ["python", "a\0b"]},
    {"timeout": 0}, {"timeout": True}, {"timeout": float("nan")}, {"timeout": 10**1000},
    {"concurrency": True}, {"concurrency": 0}, {"network": "host"},
    {"env": {"SECRET": "a\nEVIL=1"}}, {"env": {"BAD NAME": "x"}}, {"env": []},
    {"image": "--privileged"}, {"pass_threshold": -1},
])
def test_invalid_options_fail_before_docker(tmp_path, monkeypatch, kwargs):
    pack = _pack(tmp_path)
    monkeypatch.setattr(coding.shutil, "which", lambda _: pytest.fail("Docker discovery ran before validation"))
    options = dict(command=["python"], artifacts_dir=tmp_path / "artifacts")
    options.update(kwargs)
    with pytest.raises(ValueError):
        run_coding_eval(pack, **options)


def test_docker_missing_and_invalid_image_fail_preflight(tmp_path, monkeypatch):
    pack = _pack(tmp_path)
    monkeypatch.setattr(coding.shutil, "which", lambda _: None)
    with pytest.raises(CodingError, match="not installed"):
        run_coding_eval(pack, ["python"], artifacts_dir=tmp_path / "artifacts")
    assert not (tmp_path / "artifacts").exists()


def test_preflight_snapshots_shared_canonical_trees_only_once(tmp_path, monkeypatch):
    pack = _pack(tmp_path, tasks=32)
    pack.tasks[1].workspace = pack.tasks[1].workspace / ".." / "workspace"
    observed = []

    def capture_snapshot(path):
        observed.append(path.resolve())
        return _snapshot_directory(path)

    def stop_before_execution(*args):
        raise CodingError("preflight complete")

    monkeypatch.setattr(coding, "_snapshot_directory", capture_snapshot)
    monkeypatch.setattr(coding.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(_Docker, "image", stop_before_execution)
    with pytest.raises(CodingError, match="preflight complete"):
        run_coding_eval(pack, ["python"], concurrency=1, artifacts_dir=tmp_path / "artifacts")
    assert observed == [(tmp_path / "workspace").resolve(), (tmp_path / "grader").resolve()]
    assert not (tmp_path / "artifacts").exists()


def test_cached_snapshot_does_not_allow_a_linked_task_root(tmp_path, monkeypatch):
    pack = _pack(tmp_path, tasks=2)
    link = tmp_path / "workspace-link"
    try:
        link.symlink_to(pack.tasks[0].workspace, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this host")
    pack.tasks[1].workspace = link
    monkeypatch.setattr(coding.shutil, "which", lambda _: pytest.fail("Docker discovery ran before validation"))
    with pytest.raises(CodingError, match="real directories"):
        run_coding_eval(pack, ["python"], artifacts_dir=tmp_path / "artifacts")


def test_cached_trees_still_validate_every_task_before_execution(tmp_path, monkeypatch):
    pack = _pack(tmp_path, tasks=2)
    pack.tasks[1].command = ()
    monkeypatch.setattr(coding.shutil, "which", lambda _: pytest.fail("Docker discovery ran before validation"))
    with pytest.raises(ValueError, match="nonempty argv"):
        run_coding_eval(pack, ["python"], artifacts_dir=tmp_path / "artifacts")


def test_docker_process_bounds_and_nonzero():
    docker = _Docker(sys.executable)
    result = docker.call(["-c", "import sys; print('ok'); print('bad',file=sys.stderr); sys.exit(3)"], check=False)
    assert result.returncode == 3 and result.stdout.strip() == b"ok" and result.stderr.strip() == b"bad"
    with pytest.raises(CodingError, match="byte limit") as captured:
        docker.call(["-c", "print('x'*100000)"], limit=1024)
    assert len(captured.value.stdout) <= 1024
    began = time.monotonic()
    with pytest.raises(CodingError, match="timed out"):
        docker.call(["-c", "import time; time.sleep(30)"], timeout=0.05)
    assert time.monotonic() - began < 5


def test_docker_cancelled_operation_stops_and_cleanup_calls_still_run():
    docker = _Docker(sys.executable)
    docker.cancelled.set()
    with pytest.raises(CodingError, match="interrupted"):
        docker.call(["-c", "import time; time.sleep(30)"])
    assert docker.call(["-c", "print('clean')"], cancellable=False).stdout.strip() == b"clean"


@pytest.mark.parametrize("metadata,error", [
    ({"Id": "sha256:" + "a" * 64, "Os": "windows"}, "Linux"),
    ({"Id": "sha256:" + "a" * 64, "Os": "linux", "Config": {"Volumes": {"/data": {}}}}, "VOLUME"),
    ({"Id": "mutable", "Os": "linux"}, "invalid image"),
])
def test_image_validation(monkeypatch, metadata, error):
    docker = _Docker("docker")
    monkeypatch.setattr(docker, "call", lambda *a, **k: coding._Completed(0, json.dumps(metadata).encode(), b""))
    with pytest.raises(CodingError, match=error):
        docker.image("image")


@pytest.fixture
def docker_enabled():
    if os.environ.get("AGENT_EVAL_DOCKER_TESTS") != "1":
        pytest.skip("set AGENT_EVAL_DOCKER_TESTS=1 to run real Docker evaluation tests")
    # Explicit opt-in should fail if infrastructure is missing, not silently skip.
    subprocess.run(["docker", "image", "inspect", "python:3.12-slim"], check=True, capture_output=True)


def _assert_no_resources(result):
    selection = f"label={coding._LABEL}={result.metadata['container_run_label']}"
    for args in (["ps", "-aq", "--filter", selection], ["volume", "ls", "-q", "--filter", selection]):
        remaining = subprocess.check_output(["docker", *args], text=True).strip()
        assert not remaining, f"evaluation resources leaked: {remaining}"


def test_docker_e2e_correct_candidate_hidden_grader_env_and_clean_workspace(tmp_path, docker_enabled):
    pack = _pack(tmp_path, tasks=2)
    command = ["python", "-c", (
        "import os,sys; assert os.getuid()==65534; assert not os.path.exists('/grader/test.py'); "
        "assert os.environ['AGENT_EVAL_TEST_SECRET']=='explicit-test-value'; "
        "assert os.environ['HOME']=='/home/agent'; "
        "assert not os.path.exists('/home/agent/agent-config'); "
        "open('/home/agent/agent-config','w').write('private'); "
        "assert 'ambient-test-secret' not in os.environ.values(); "
        "assert open('solution.py').read()=='answer = 0\\n'; "
        "assert sys.stdin.read()=='Set answer to 42'; "
        "open('solution.py','w').write('answer = 42\\n'); print('fixed')"
    )]
    old = os.environ.get("AGENT_EVAL_AMBIENT_TEST_SECRET")
    os.environ["AGENT_EVAL_AMBIENT_TEST_SECRET"] = "ambient-test-secret"
    try:
        result = run_coding_eval(pack, command, env={"AGENT_EVAL_TEST_SECRET": "explicit-test-value"},
                                 artifacts_dir=tmp_path / "artifacts", concurrency=2)
    finally:
        if old is None:
            os.environ.pop("AGENT_EVAL_AMBIENT_TEST_SECRET", None)
        else:
            os.environ["AGENT_EVAL_AMBIENT_TEST_SECRET"] = old
    assert result.pass_rate == 1 and result.error_count == 0, [c.error for c in result.case_results]
    assert [c.case_id for c in result.case_results] == ["case-0", "case-1"]
    assert result.summary["cost_usd"]["reported_total"] is None
    assert "explicit-test-value" not in json.dumps(result.to_dict())
    assert (pack.tasks[0].workspace / "solution.py").read_text() == "answer = 0\n"
    for case in result.case_results:
        artifacts = Path(case.metadata["artifacts"])
        assert b"answer = 42" in (artifacts / "patch.diff").read_bytes()
        candidate = _snapshot_archive((artifacts / "candidate.tar").read_bytes())
        assert candidate.files["solution.py"].data == b"answer = 42\n"
        assert "agent-config" not in candidate.files
        assert (artifacts / "grader.stdout.log").read_text().strip() == "independent grader passed"
        assert case.agent_latency_ms is not None and case.agent_latency_ms < case.latency_ms
    _assert_no_resources(result)


def test_docker_e2e_failed_tests_are_zero_without_execution_error(tmp_path, docker_enabled):
    result = run_coding_eval(_pack(tmp_path), ["python", "-c", "print('no edit')"], artifacts_dir=tmp_path / "artifacts")
    assert result.pass_rate == 0 and result.error_count == 0
    assert result.case_results[0].metadata["grader_exit_code"] == 1
    _assert_no_resources(result)


def test_docker_e2e_workspace_at_entry_limit_roundtrips(tmp_path, docker_enabled):
    pack = _pack(tmp_path, grader_source="print('passed')\n")
    for index in range(coding.MAX_ENTRIES - 1):
        (pack.tasks[0].workspace / f"empty-{index:04d}").write_bytes(b"")
    result = run_coding_eval(pack, ["python", "-c", "pass"], artifacts_dir=tmp_path / "artifacts")
    assert result.pass_rate == 1 and result.error_count == 0, result.case_results[0].error
    case = result.case_results[0]
    candidate = _snapshot_archive((Path(case.metadata["artifacts"]) / "candidate.tar").read_bytes())
    assert candidate == _snapshot_directory(pack.tasks[0].workspace)
    assert len(case.metadata["candidate_manifest"]) == coding.MAX_ENTRIES
    _assert_no_resources(result)


def test_docker_e2e_bundled_grader_supports_local_modules_and_directory_evidence(tmp_path, docker_enabled):
    pack = load_pack("coding-starter@1.0.0")
    task = next(task for task in pack.tasks if task.id == "dedup")
    pack = replace(pack, tasks=(task,))
    command = ["python", "-c", (
        "from pathlib import Path; "
        "Path('helpers.py').write_text('def stable_unique(values):\\n    return list(dict.fromkeys(values))\\n'); "
        "Path('solution.py').write_text('from helpers import stable_unique\\n'); "
        "Path('empty-fixture').mkdir()"
    )]
    result = run_coding_eval(pack, command, artifacts_dir=tmp_path / "artifacts")
    assert result.pass_rate == 1 and result.error_count == 0, result.case_results[0].error
    case = result.case_results[0]
    manifest = case.metadata["candidate_manifest"]
    assert {"path": "empty-fixture", "type": "directory"} in manifest
    artifacts = Path(case.metadata["artifacts"])
    assert json.loads((artifacts / "candidate-manifest.json").read_text()) == manifest
    assert json.loads((artifacts / "evidence.json").read_text())["candidate_manifest"] == manifest
    assert "empty-fixture" in _snapshot_archive((artifacts / "candidate.tar").read_bytes()).directories
    _assert_no_resources(result)


@pytest.mark.parametrize("command,error_phase", [
    (["python", "-c", "import time; print('started',flush=True); time.sleep(30)"], "agent"),
    (["python", "-c", "import pathlib; pathlib.Path('unsafe').symlink_to('/etc/passwd')"], "validation"),
    (["python", "-c", "print('x'*2000000)"], "agent"),
    (["agent-that-does-not-exist"], "agent"),
])
def test_docker_e2e_agent_timeout_unsafe_candidate_and_limits_cleanup(tmp_path, docker_enabled, command, error_phase):
    result = run_coding_eval(_pack(tmp_path), command, timeout=0.5, artifacts_dir=tmp_path / "artifacts")
    assert result.error_count == 1 and result.pass_rate == 0
    assert result.case_results[0].metadata["coding_error_phase"] == error_phase
    _assert_no_resources(result)


def test_docker_e2e_grader_timeout_is_an_error_and_cleans_up(tmp_path, docker_enabled):
    pack = _pack(tmp_path, grader_source="import time\ntime.sleep(30)\n")
    pack.tasks[0].timeout = 0.1
    result = run_coding_eval(pack, ["python", "-c", "print('done')"], artifacts_dir=tmp_path / "artifacts")
    assert result.error_count == 1 and result.case_results[0].error_stage == "scorer"
    assert result.case_results[0].metadata["coding_error_phase"] == "grader"
    _assert_no_resources(result)


def test_docker_e2e_candidate_descendants_are_frozen_and_removed(tmp_path, docker_enabled):
    command = ["python", "-c", (
        "import subprocess; open('solution.py','w').write('answer = 42\\n'); "
        "subprocess.Popen(['python','-c',\"import time; time.sleep(1); open('/workspace/solution.py','w').write('answer = 0\\\\n')\"], "
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print('done')"
    )]
    result = run_coding_eval(_pack(tmp_path), command, artifacts_dir=tmp_path / "artifacts")
    assert result.pass_rate == 1 and result.error_count == 0, result.case_results[0].error
    _assert_no_resources(result)


def test_patch_binary_summary_before_text_never_exceeds_limit(monkeypatch):
    monkeypatch.setattr(coding, "MAX_PATCH_BYTES", 20)
    old = _Snapshot({"a": _File(b"\xff"), "z": _File(b"old\n")})
    new = _Snapshot({"a": _File(b"\xfe"), "z": _File(b"new\n")})
    patch, truncated = _patch(old, new)
    assert len(patch) == 20 and truncated


def test_local_snapshot_rejects_hardlinks(tmp_path):
    original = tmp_path / "one"
    original.write_bytes(b"same inode")
    try:
        (tmp_path / "two").hardlink_to(original)
    except OSError:
        pytest.skip("hardlinks are not supported on this host")
    with pytest.raises(CodingError, match="hard links"):
        _snapshot_directory(tmp_path)
