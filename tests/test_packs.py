from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from harness.dataset import load_dataset
from harness.packs import BenchmarkPack, PackError, init_pack, list_packs, load_pack


def copy_pack(tmp_path, name="coding-starter"):
    source = load_pack(name)
    target = tmp_path / "pack"
    shutil.copytree(source.root, target)
    return target


def replace_manifest(root, before, after):
    path = root / "pack.toml"
    text = path.read_text(encoding="utf-8")
    assert before in text
    path.write_text(text.replace(before, after, 1), encoding="utf-8")


def test_bundled_packs_load_with_complete_provenance():
    packs = list_packs()
    assert [(pack.id, pack.version, pack.kind) for pack in packs] == [
        ("coding-starter", "1.0.0", "coding"), ("json-contracts", "1.0.0", "output"),
    ]
    for pack in packs:
        assert isinstance(pack, BenchmarkPack)
        assert pack == load_pack(f"{pack.id}@{pack.version}")
        assert pack == load_pack(pack.root / "pack.toml")
        assert len(pack.fingerprint) == 64
        assert pack.name and pack.description
        assert pack.license == "Apache-2.0"
        assert "Apache License" in (pack.root / "LICENSE").read_text()
        assert (pack.root / "provenance.toml").is_file()
    coding, output = packs
    assert [task.id for task in coding.tasks] == ["slug", "dedup", "intervals"]
    assert coding.dataset is None and coding.image == "python:3.12-slim"
    assert output.tasks == () and output.image is None
    assert output.scorers == ("json",)
    assert len(load_dataset(output.dataset)) == 16


def test_output_pack_preserves_original_authored_dataset():
    bundled = load_pack("json-contracts").dataset.read_bytes()
    assert bundled == Path("benchmarks/agent_smoke.jsonl").read_bytes()


def test_pack_fingerprint_is_portable_and_covers_all_files(tmp_path):
    source = load_pack("coding-starter")
    target = copy_pack(tmp_path)
    assert source.fingerprint == load_pack(target).fingerprint
    for relative in ["tasks/slug/workspace/solution.py", "tasks/slug/grader/test_solution.py", "README.md"]:
        path = target / relative
        previous = load_pack(target).fingerprint
        path.write_bytes(path.read_bytes() + b"\n")
        assert load_pack(target).fingerprint != previous
    previous = load_pack(target).fingerprint
    (target / "additional.txt").write_text("extra data")
    assert load_pack(target).fingerprint != previous
    previous = load_pack(target).fingerprint
    (target / "additional.txt").rename(target / "renamed.txt")
    assert load_pack(target).fingerprint != previous


@pytest.mark.parametrize("tree", ["workspace", "grader"])
def test_fingerprint_covers_empty_directories_and_entry_types(tmp_path, tree):
    root = copy_pack(tmp_path)
    original = load_pack(root).fingerprint
    path = root / "tasks" / "slug" / tree / "fixture"
    path.mkdir()
    directory_fingerprint = load_pack(root).fingerprint
    assert directory_fingerprint != original
    renamed = path.with_name("renamed-fixture")
    path.rename(renamed)
    assert load_pack(root).fingerprint != directory_fingerprint
    renamed.rename(path)
    assert load_pack(root).fingerprint == directory_fingerprint
    path.rmdir()
    path.write_bytes(b"")
    assert load_pack(root).fingerprint not in {original, directory_fingerprint}
    path.unlink()
    assert load_pack(root).fingerprint == original


@pytest.mark.skipif(os.name == "nt", reason="Windows has different executable-mode semantics")
def test_fingerprint_covers_executable_flag(tmp_path):
    root = copy_pack(tmp_path)
    before = load_pack(root).fingerprint
    script = root / "tasks/slug/workspace/solution.py"
    script.chmod(script.stat().st_mode | 0o100)
    assert load_pack(root).fingerprint != before


@pytest.mark.parametrize("before,after,match", [
    ('schema_version = 1', 'schema_version = true', 'schema_version'),
    ('schema_version = 1', 'schema_version = 2', 'schema_version'),
    ('id = "coding-starter"', 'id = "Wrong ID"', 'identifier'),
    ('version = "1.0.0"', 'version = "01.0.0"', 'semantic versioning'),
    ('version = "1.0.0"', 'version = "1.0"', 'semantic versioning'),
    ('version = "1.0.0"', 'version = "1.0.0-01"', 'semantic versioning'),
    ('kind = "coding"', 'kind = "custom"', 'kind'),
    ('kind = "coding"', 'kind = "coding"\nallow_network = true', 'unknown fields'),
    ('license = "Apache-2.0"', 'license = ""', 'license'),
    ('image = "python:3.12-slim"', 'image = "python:3.12-slim"\nextra = true', 'unknown fields'),
    ('image = "python:3.12-slim"', 'image = "--privileged"', 'image reference'),
    ('image = "python:3.12-slim"', 'image = "python image"', 'image reference'),
    ('id = "dedup"', 'id = "slug"', 'duplicate task'),
    ('timeout = 30', 'timeout = 30\nextra = 1', 'unknown fields'),
    ('timeout = 30', 'timeout = true', 'timeout'),
    ('timeout = 30', 'timeout = nan', 'timeout'),
    ('timeout = 30', 'timeout = inf', 'timeout'),
    ('timeout = 30', 'timeout = 0', 'timeout'),
    ('timeout = 30', 'timeout = 3601', 'timeout'),
    ('timeout = 30', 'timeout = ' + '9' * 320, 'timeout'),
    ('tags = ["python", "repository-edit", "text", "unicode"]', 'tags = ["same", "same"]', 'duplicate'),
    ('tags = ["python", "repository-edit", "text", "unicode"]', 'tags = [""]', 'nonempty'),
    ('command = ["python", "-I", "-B", "/grader/test_solution.py"]', 'command = "python /grader/test_solution.py"', 'argv'),
    ('command = ["python", "-I", "-B", "/grader/test_solution.py"]', 'command = ["python", 1]', 'string'),
    ('command = ["python", "-I", "-B", "/grader/test_solution.py"]', 'command = []', 'argv'),
    ('command = ["python", "-I", "-B", "/grader/test_solution.py"]', 'command = ["python", "bad\\nargument"]', 'control'),
])
def test_manifest_rejects_bad_configuration(tmp_path, before, after, match):
    root = copy_pack(tmp_path)
    replace_manifest(root, before, after)
    with pytest.raises(PackError, match=match):
        load_pack(root)


@pytest.mark.parametrize("version", ["0.0.0", "1.2.3-alpha.1", "1.2.3-rc.0+build.014", "999.0.0+sha"])
def test_valid_semantic_versions(tmp_path, version):
    root = copy_pack(tmp_path)
    replace_manifest(root, 'version = "1.0.0"', f'version = "{version}"')
    assert load_pack(root).version == version


@pytest.mark.parametrize("path", ["../outside", "/tmp/workspace", "C:/workspace", "tasks\\\\slug", "tasks/./slug", "tasks//slug"])
def test_rejects_unconfined_paths(tmp_path, path):
    root = copy_pack(tmp_path)
    replace_manifest(root, 'workspace = "tasks/slug/workspace"', f'workspace = "{path}"')
    with pytest.raises(PackError, match="confined relative"):
        load_pack(root)


def test_rejects_missing_and_empty_task_directories(tmp_path):
    root = copy_pack(tmp_path)
    replace_manifest(root, 'workspace = "tasks/slug/workspace"', 'workspace = "missing"')
    with pytest.raises(PackError, match="existing directory"):
        load_pack(root)
    (root / "missing").mkdir()
    with pytest.raises(PackError, match="empty"):
        load_pack(root)


def test_references_require_exact_case_on_every_platform(tmp_path):
    root = copy_pack(tmp_path)
    replace_manifest(root, 'workspace = "tasks/slug/workspace"', 'workspace = "tasks/SLUG/workspace"')
    with pytest.raises(PackError, match="exact path spelling"):
        load_pack(root)


@pytest.mark.parametrize("target", ["tasks/slug/workspace", "tasks/slug", "tasks/dedup/workspace"])
def test_grader_cannot_overlap_any_workspace(tmp_path, target):
    root = copy_pack(tmp_path)
    replace_manifest(root, 'grader = "tasks/slug/grader"', f'grader = "{target}"')
    with pytest.raises(PackError, match="must not overlap"):
        load_pack(root)


def test_rejects_symlinks_even_when_not_referenced(tmp_path):
    root = copy_pack(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("not pack data")
    try:
        (root / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(PackError, match="symlinks"):
        load_pack(root)


def test_rejects_linked_pack_root_and_manifest(tmp_path):
    root = copy_pack(tmp_path)
    link = tmp_path / "linked"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(PackError, match="symlink"):
        load_pack(link)
    (root / "pack.toml").rename(tmp_path / "real.toml")
    (root / "pack.toml").symlink_to(tmp_path / "real.toml")
    with pytest.raises(PackError, match="symlink"):
        load_pack(root)


def test_rejects_hard_links(tmp_path):
    root = copy_pack(tmp_path)
    try:
        os.link(root / "README.md", root / "hard-link.txt")
    except OSError:
        pytest.skip("hard-link creation unavailable")
    with pytest.raises(PackError, match="hard links"):
        load_pack(root)


def test_case_colliding_paths_rejected_when_filesystem_supports_them(tmp_path):
    root = copy_pack(tmp_path)
    first, second = root / "Collision", root / "collision"
    first.write_text("one")
    second.write_text("two")
    if first.samefile(second):
        pytest.skip("filesystem is case insensitive")
    with pytest.raises(PackError, match="case-insensitive"):
        load_pack(root)


@pytest.mark.parametrize("name", ["bad:name", "trailing.", "CON.txt"])
def test_rejects_nonportable_names(tmp_path, name):
    root = copy_pack(tmp_path)
    try:
        (root / name).write_text("bad")
    except OSError:
        pytest.skip("filesystem already prohibits this name")
    if name not in {entry.name for entry in root.iterdir()}:
        pytest.skip("filesystem normalizes this name")
    with pytest.raises(PackError, match="non-portable"):
        load_pack(root)


@pytest.mark.parametrize("constant,limit,match", [
    ("_MAX_FILE_BYTES", 10, "file exceeds"),
    ("_MAX_PACK_BYTES", 20, "pack exceeds"),
    ("_MAX_FILES", 2, "files and directories"),
    ("_MAX_MANIFEST_BYTES", 10, "pack.toml exceeds"),
    ("_MAX_TASKS", 1, "at most 1 tasks"),
])
def test_pack_resource_limits_are_enforced(tmp_path, monkeypatch, constant, limit, match):
    root = copy_pack(tmp_path)
    monkeypatch.setattr("harness.packs." + constant, limit)
    with pytest.raises(PackError, match=match):
        load_pack(root)


def test_output_pack_checks_dataset_and_scorers_before_running(tmp_path):
    root = copy_pack(tmp_path, "json-contracts")
    replace_manifest(root, 'scorers = ["json"]', 'scorers = ["unknown"]')
    with pytest.raises(PackError, match="unknown scorers"):
        load_pack(root)
    replace_manifest(root, 'scorers = ["unknown"]', 'scorers = ["json"]')
    (root / "dataset.jsonl").write_text('{"id":"missing-input"}\n')
    with pytest.raises(PackError, match="invalid pack dataset"):
        load_pack(root)


@pytest.mark.parametrize("kind", ["coding", "output"])
def test_scaffolds_editable_pack_without_overwriting(tmp_path, kind):
    target = tmp_path / "My New Pack"
    created = init_pack(target, kind)
    pack = load_pack(created)
    assert created == target.resolve()
    assert pack.id == "my-new-pack" and pack.version == "0.1.0"
    assert pack.kind == kind
    assert "Source fingerprint" in (target / "README.md").read_text()
    before = pack.fingerprint
    with pytest.raises(PackError, match="already exists"):
        init_pack(target, kind)
    assert load_pack(target).fingerprint == before


def test_missing_pack_and_invalid_scaffold_kind(tmp_path):
    with pytest.raises(PackError, match="not found"):
        load_pack("coding-starter@99.0.0")
    with pytest.raises(PackError, match="not found"):
        load_pack(tmp_path / "does-not-exist")
    with pytest.raises(PackError, match="kind"):
        init_pack(tmp_path / "new", "unrecognized")


_SOLUTIONS = {
    "slug": """import re
import unicodedata
def slugify(text):
    if not isinstance(text, str):
        raise TypeError('text must be a string')
    text = unicodedata.normalize('NFKD', text.casefold()).encode('ascii', 'ignore').decode('ascii')
    return re.sub('[^a-z0-9]+', '-', text).strip('-')
""",
    "dedup": """def stable_unique(values):
    return list(dict.fromkeys(values))
""",
    "intervals": """def merge_intervals(intervals):
    if any(left > right for left, right in intervals):
        raise ValueError('reversed interval')
    result = []
    for left, right in sorted(intervals):
        if result and left <= result[-1][1]:
            result[-1][1] = max(result[-1][1], right)
        else:
            result.append([left, right])
    return result
""",
}


def run_grader(task, workspace):
    return subprocess.run(
        [sys.executable, "-I", "-B", str(task.grader / "test_solution.py"), str(workspace)],
        capture_output=True, text=True, check=False, timeout=20,
    )


@pytest.mark.parametrize("task_id", ["slug", "dedup", "intervals"])
def test_independent_graders_reject_starters_and_accept_correct_repairs(tmp_path, task_id):
    task = next(task for task in load_pack("coding-starter").tasks if task.id == task_id)
    workspace = tmp_path / "workspace"
    shutil.copytree(task.workspace, workspace)
    failed = run_grader(task, workspace)
    assert failed.returncode != 0
    (workspace / "solution.py").write_text(_SOLUTIONS[task_id])
    passed = run_grader(task, workspace)
    assert passed.returncode == 0, passed.stderr
    evidence = json.loads(passed.stdout)
    assert evidence["passed"] == evidence["total"] and evidence["total"] >= 16


@pytest.mark.parametrize("task_id,function", [
    ("slug", "slugify"), ("dedup", "stable_unique"), ("intervals", "merge_intervals"),
])
def test_independent_graders_accept_repairs_with_local_helper_modules(tmp_path, task_id, function):
    task = next(task for task in load_pack("coding-starter").tasks if task.id == task_id)
    (tmp_path / "helpers.py").write_text(_SOLUTIONS[task_id])
    (tmp_path / "solution.py").write_text(f"from helpers import {function}\n")
    result = run_grader(task, tmp_path)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["passed"] == evidence["total"] and evidence["total"] >= 16


@pytest.mark.parametrize("attack", ["import os\nos._exit(0)\n", "print('success')\nraise SystemExit(0)\n"])
def test_zero_exit_without_valid_results_cannot_pass_grader(tmp_path, attack):
    task = load_pack("coding-starter").tasks[0]
    (tmp_path / "solution.py").write_text(attack)
    result = run_grader(task, tmp_path)
    assert result.returncode != 0


def test_grader_detects_correct_values_that_mutate_input(tmp_path):
    task = next(task for task in load_pack("coding-starter").tasks if task.id == "dedup")
    (tmp_path / "solution.py").write_text("def stable_unique(values):\n    values[:] = dict.fromkeys(values)\n    return list(values)\n")
    result = run_grader(task, tmp_path)
    assert result.returncode != 0 and "input mutated" in result.stderr


def test_grader_detects_returned_intervals_that_alias_input(tmp_path):
    task = next(task for task in load_pack("coding-starter").tasks if task.id == "intervals")
    source = _SOLUTIONS["intervals"].replace("for left, right in sorted(intervals):", "for interval in sorted(intervals):\n        left, right = interval")
    source = source.replace("result.append([left, right])", "result.append(interval)")
    (tmp_path / "solution.py").write_text(source)
    result = run_grader(task, tmp_path)
    assert result.returncode != 0 and "aliases input" in result.stderr
