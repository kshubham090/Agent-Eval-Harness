"""Validated, content-addressed benchmark packs shipped with the distribution.

Pack manifests describe data and trusted grading commands, not host-side hooks.
All referenced paths are relative to the pack. Loading never executes a grader.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from harness.dataset import DatasetError, load_dataset


class PackError(ValueError):
    """A benchmark pack is missing, malformed, or unsafe to copy."""


@dataclass(frozen=True)
class CodingTask:
    id: str
    input: str
    workspace: Path
    grader: Path
    command: tuple[str, ...]
    tags: tuple[str, ...] = ()
    timeout: float = 30.0


@dataclass(frozen=True)
class BenchmarkPack:
    id: str
    version: str
    kind: str
    root: Path
    fingerprint: str
    dataset: Path | None = None
    scorers: tuple[str, ...] = ()
    image: str | None = None
    tasks: tuple[CodingTask, ...] = ()
    name: str = ""
    description: str = ""
    license: str = ""
    schema_version: int = 1


_BUNDLED_ROOT = Path(__file__).parent / "packs_data"
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z"
)
_SCORERS = {"exact", "regex", "contains", "json", "embedding", "llm_judge"}
_MAX_FILES = 10_000
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_PACK_BYTES = 64 * 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_TASKS = 1_000


def _keys(value: object, allowed: set[str], required: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise PackError(f"{label} must be a table")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise PackError(f"{label}: unknown fields {sorted(unknown)}")
    if missing:
        raise PackError(f"{label}: missing fields {sorted(missing)}")
    return value


def _string(value: object, label: str, maximum: int = 65_536) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PackError(f"{label} must be a nonempty string")
    if len(value) > maximum or "\0" in value:
        raise PackError(f"{label} exceeds its length limit or contains NUL")
    return value


def _identifier(value: object, label: str) -> str:
    value = _string(value, label, 64)
    if not _ID.fullmatch(value) or value in {".", ".."}:
        raise PackError(f"{label} must be a lowercase identifier (letters, digits, '.', '_' or '-')")
    return value


def _strings(value: object, label: str, *, empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not empty and not value) or len(value) > 256:
        raise PackError(f"{label} must be {'a' if empty else 'a nonempty'} list with at most 256 strings")
    result = tuple(_string(item, label, 4096) for item in value)
    if len(set(result)) != len(result):
        raise PackError(f"{label} contains duplicate entries")
    return result


def _portable_component(name: str) -> bool:
    # The fingerprint records paths identically on Windows, macOS, and Linux.
    stem = name.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    return (
        bool(re.fullmatch(r"[A-Za-z0-9_. -]+", name))
        and name not in {".", ".."}
        and not name.endswith((" ", "."))
        and stem not in reserved
    )


def _inventory(root: Path) -> list[tuple[str, Path, int, bool]]:
    """Reject links/special files and bound reads before hashing pack content."""
    files: list[tuple[str, Path, int, bool]] = []
    total = 0
    visited = 0
    folded: set[str] = set()
    def fail_walk(error: OSError) -> None:
        raise error

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=fail_walk):
        for name in sorted([*dirs, *names]):
            visited += 1
            if visited > _MAX_FILES:
                raise PackError(f"pack exceeds {_MAX_FILES} files and directories")
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if len(relative) > 1024 or len(path.relative_to(root).parts) > 64:
                raise PackError("pack paths exceed the 1024-character or 64-level limit")
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise PackError(f"symlinks are not allowed in packs: {relative}")
            if not _portable_component(name):
                raise PackError(f"pack contains a non-portable path: {relative}")
            if relative.casefold() in folded:
                raise PackError(f"pack contains case-insensitive path collisions: {relative}")
            folded.add(relative.casefold())
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise PackError(f"pack contains a non-regular file: {relative}")
            if info.st_nlink > 1:
                raise PackError(f"hard links are not allowed in packs: {relative}")
            if info.st_size > _MAX_FILE_BYTES:
                raise PackError(f"pack file exceeds {_MAX_FILE_BYTES} bytes: {relative}")
            total += info.st_size
            if total > _MAX_PACK_BYTES:
                raise PackError(f"pack exceeds {_MAX_PACK_BYTES} bytes")
            files.append((relative, path, info.st_size, bool(info.st_mode & 0o111)))
    return sorted(files)


def _path(root: Path, value: object, label: str, *, directory: bool = False) -> Path:
    value = _string(value, label, 1024)
    pure = PurePosixPath(value)
    if (
        pure.is_absolute() or "\\" in value or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise PackError(f"{label} must be a confined relative POSIX path")
    current = root
    for component in pure.parts:
        if not current.is_dir() or component not in {entry.name for entry in current.iterdir()}:
            raise PackError(f"{label} does not reference an existing {'directory' if directory else 'file'} with exact path spelling")
        current = current / component
    path = root.joinpath(*pure.parts)
    if not path.resolve().is_relative_to(root):
        raise PackError(f"{label} escapes the pack")
    if not (path.is_dir() if directory else path.is_file()):
        raise PackError(f"{label} does not reference an existing {'directory' if directory else 'file'}")
    if directory and not any(path.iterdir()):
        raise PackError(f"{label} directory is empty")
    return path


def _hash_files(files: list[tuple[str, Path, int, bool]]) -> str:
    digest = hashlib.sha256(b"agent-eval-pack-v1\0")
    for relative, path, expected_size, executable in files:
        name = relative.encode("utf-8")
        with path.open("rb") as stream:
            contents = stream.read(_MAX_FILE_BYTES + 1)
        if len(contents) != expected_size:
            raise PackError(f"pack changed while reading: {relative}")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(bytes([executable]))
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def _load_directory(root: Path) -> BenchmarkPack:
    if root.is_symlink():
        raise PackError("pack root must not be a symlink")
    if not root.is_dir():
        raise PackError(f"pack directory does not exist: {root}")
    root = root.resolve()
    files = _inventory(root)
    manifest = root / "pack.toml"
    if not manifest.is_file():
        raise PackError(f"missing pack.toml in {root}")
    if manifest.stat().st_size > _MAX_MANIFEST_BYTES:
        raise PackError("pack.toml exceeds 1 MiB")
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PackError(f"invalid pack.toml: {exc}") from exc
    common = {"schema_version", "id", "version", "name", "description", "license", "kind"}
    _keys(data, common | {"output", "coding", "tasks"}, common, "pack")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise PackError("schema_version must be the integer 1")
    identifier = _identifier(data["id"], "id")
    version = _string(data["version"], "version", 128)
    if not _SEMVER.fullmatch(version):
        raise PackError("version must follow semantic versioning, for example '1.0.0'")
    name = _string(data["name"], "name", 200)
    description = _string(data["description"], "description", 20_000)
    license_name = _string(data["license"], "license", 255)
    kind = data["kind"]
    if kind not in ("output", "coding"):
        raise PackError("kind must be 'output' or 'coding'")
    dataset = None
    scorers: tuple[str, ...] = ()
    image = None
    tasks: list[CodingTask] = []
    if kind == "output":
        if "coding" in data or "tasks" in data:
            raise PackError("output packs cannot define coding or tasks")
        output = _keys(data.get("output"), {"dataset", "scorers"}, {"dataset", "scorers"}, "output")
        dataset = _path(root, output["dataset"], "output.dataset")
        scorers = _strings(output["scorers"], "output.scorers")
        if set(scorers) - _SCORERS:
            raise PackError(f"unknown scorers: {sorted(set(scorers) - _SCORERS)}")
        try:
            load_dataset(dataset)
        except (DatasetError, UnicodeError) as exc:
            raise PackError(f"invalid pack dataset: {exc}") from exc
    else:
        if "output" in data:
            raise PackError("coding packs cannot define output")
        coding = _keys(data.get("coding"), {"image"}, {"image"}, "coding")
        image = _string(coding["image"], "coding.image", 255)
        if image.startswith("-") or any(c.isspace() or ord(c) < 32 for c in image):
            raise PackError("coding.image must be a Docker image reference without whitespace")
        raw_tasks = data.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks or len(raw_tasks) > _MAX_TASKS:
            raise PackError(f"tasks must be a nonempty array with at most {_MAX_TASKS} tasks")
        ids: set[str] = set()
        for index, raw in enumerate(raw_tasks):
            label = f"tasks[{index}]"
            required = {"id", "input", "workspace", "grader", "command"}
            _keys(raw, required | {"tags", "timeout"}, required, label)
            task_id = _identifier(raw["id"], label + ".id")
            if task_id in ids:
                raise PackError(f"duplicate task id {task_id!r}")
            ids.add(task_id)
            prompt = _string(raw["input"], label + ".input")
            workspace = _path(root, raw["workspace"], label + ".workspace", directory=True)
            grader = _path(root, raw["grader"], label + ".grader", directory=True)
            command = raw["command"]
            if not isinstance(command, list) or not command or len(command) > 256:
                raise PackError(f"{label}.command must be a nonempty argv array with at most 256 arguments")
            argv = tuple(_string(item, label + ".command", 4096) for item in command)
            if any(any(ord(c) < 32 for c in arg) for arg in argv):
                raise PackError(f"{label}.command contains control characters")
            tags = _strings(raw.get("tags", []), label + ".tags", empty=True)
            timeout = raw.get("timeout", 30.0)
            if type(timeout) not in (int, float) or not 0 < timeout <= 3600 or not math.isfinite(timeout):
                raise PackError(f"{label}.timeout must be a finite number greater than 0 and at most 3600 seconds")
            tasks.append(CodingTask(task_id, prompt, workspace, grader, argv, tags, float(timeout)))
        for task in tasks:
            for other in tasks:
                if task.workspace.is_relative_to(other.grader) or other.grader.is_relative_to(task.workspace):
                    raise PackError("workspace and grader directories must not overlap (including across tasks)")
    return BenchmarkPack(
        id=identifier, version=version, kind=kind, root=root,
        fingerprint=_hash_files(files), dataset=dataset, scorers=scorers, image=image,
        tasks=tuple(tasks), name=name, description=description, license=license_name,
    )


def _version_key(version: str) -> tuple:
    match = _SEMVER.fullmatch(version)
    assert match is not None
    major, minor, patch, prerelease, _build = match.groups()
    parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in (prerelease or "").split("."))
    return int(major), int(minor), int(patch), prerelease is None, parts, version


def list_packs() -> list[BenchmarkPack]:
    """Return every bundled version, validated and ordered by id then version."""
    if not _BUNDLED_ROOT.is_dir():
        return []
    try:
        packs = [_load_directory(manifest.parent) for manifest in sorted(_BUNDLED_ROOT.glob("*/*/pack.toml"))]
    except OSError as exc:
        raise PackError(f"cannot read bundled benchmark packs: {exc}") from exc
    identities = [(pack.id, pack.version) for pack in packs]
    if len(set(identities)) != len(identities):
        raise PackError("duplicate bundled pack id/version")
    return sorted(packs, key=lambda pack: (pack.id, _version_key(pack.version)))


def load_pack(path_or_name: str | Path) -> BenchmarkPack:
    """Load a directory/manifest, a bundled id, or an explicit ``id@version``.

    An unversioned bundled id selects its highest semantic version. Results must
    retain both that resolved version and fingerprint for reproducibility.
    """
    path = Path(path_or_name).expanduser()
    try:
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                raise PackError("pack path must not be a symlink")
            if path.is_file():
                if path.name != "pack.toml":
                    raise PackError("pack manifest must be named pack.toml")
                path = path.parent
            return _load_directory(path)
        name, separator, version = str(path_or_name).partition("@")
        matches = [pack for pack in list_packs() if pack.id == name and (not separator or pack.version == version)]
        if matches:
            return matches[-1]
        raise PackError(f"benchmark pack not found: {path_or_name}")
    except OSError as exc:
        raise PackError(f"cannot read benchmark pack: {exc}") from exc


def init_pack(destination: str | Path, kind: str = "coding") -> Path:
    """Create an editable, validated starter pack without overwriting a path."""
    if kind not in {"coding", "output"}:
        raise PackError("kind must be 'coding' or 'output'")
    destination = Path(destination).expanduser()
    if destination.exists() or destination.is_symlink():
        raise PackError(f"destination already exists: {destination}")
    source = load_pack("coding-starter@1.0.0" if kind == "coding" else "json-contracts@1.0.0")
    identifier = re.sub(r"[^a-z0-9._-]+", "-", destination.name.lower()).strip(".-_")[:64] or "my-pack"
    try:
        shutil.copytree(source.root, destination)
        manifest = destination / "pack.toml"
        contents = manifest.read_text(encoding="utf-8")
        contents = contents.replace(f'id = "{source.id}"', f'id = "{identifier}"', 1)
        contents = contents.replace(f'version = "{source.version}"', 'version = "0.1.0"', 1)
        contents = contents.replace(f'name = "{source.name}"', f'name = "{identifier}"', 1)
        manifest.write_text(contents, encoding="utf-8")
        (destination / "README.md").write_text(
            f"# {identifier} — 0.1.0\n\n"
            f"Editable starter copied from `{source.id}@{source.version}`.\n"
            f"Source fingerprint: `{source.fingerprint}`.\n\n"
            "Edit pack.toml and the task files, document your changes and data\n"
            "provenance, and keep LICENSE/attribution when reusing starter content.\n"
            "Validate and test both a passing and failing implementation before\n"
            "publishing an immutable version. These small public starter cases\n"
            "are integration checks, not a general agent capability benchmark.\n",
            encoding="utf-8",
        )
        _load_directory(destination)
    except OSError as exc:
        raise PackError(f"cannot create benchmark pack: {exc}") from exc
    return destination.resolve()
