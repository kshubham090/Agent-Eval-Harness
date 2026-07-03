"""Load and validate golden datasets (JSONL, one eval case per line)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class DatasetError(ValueError):
    """Raised when a dataset file is malformed."""


@dataclass(frozen=True)
class EvalCase:
    """One golden test case.

    expected_trajectory is the ordered list of tool/step names the agent is
    expected to take; None means "output-only case, don't score trajectory".
    """

    id: str
    input: str
    expected_output: str
    expected_trajectory: tuple[str, ...] | None = None
    tags: tuple[str, ...] = ()


_REQUIRED_STR_FIELDS = ("id", "input", "expected_output")


def _parse_case(record: dict, line_no: int) -> EvalCase:
    if not isinstance(record, dict):
        raise DatasetError(f"line {line_no}: expected a JSON object, got {type(record).__name__}")

    for name in _REQUIRED_STR_FIELDS:
        value = record.get(name)
        if not isinstance(value, str) or not value.strip():
            raise DatasetError(f"line {line_no}: missing or empty required string field {name!r}")

    trajectory = record.get("expected_trajectory")
    if trajectory is not None:
        if not isinstance(trajectory, list) or not all(isinstance(s, str) for s in trajectory):
            raise DatasetError(f"line {line_no}: 'expected_trajectory' must be a list of strings or null")
        trajectory = tuple(trajectory)

    tags = record.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise DatasetError(f"line {line_no}: 'tags' must be a list of strings")

    known = {"id", "input", "expected_output", "expected_trajectory", "tags"}
    unknown = set(record) - known
    if unknown:
        raise DatasetError(f"line {line_no}: unknown fields {sorted(unknown)}")

    return EvalCase(
        id=record["id"],
        input=record["input"],
        expected_output=record["expected_output"],
        expected_trajectory=trajectory,
        tags=tuple(tags),
    )


def load_dataset(path: str | Path) -> list[EvalCase]:
    """Load a JSONL golden dataset, validating shape and id uniqueness."""
    path = Path(path)
    cases: list[EvalCase] = []
    seen_ids: set[str] = set()

    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                raise DatasetError(f"line {line_no}: invalid JSON: {e}") from e

            case = _parse_case(record, line_no)
            if case.id in seen_ids:
                raise DatasetError(f"line {line_no}: duplicate case id {case.id!r}")
            seen_ids.add(case.id)
            cases.append(case)

    if not cases:
        raise DatasetError(f"{path}: dataset is empty")
    return cases
