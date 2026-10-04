"""Abstract agent runner interface.

The harness never knows how an agent works internally -- anything that can
take an input string and return an AgentOutput can be evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Protocol, runtime_checkable


class ResponseValidationError(ValueError):
    """An agent returned output or telemetry that violates its response protocol."""


def validate_json_value(value: object, label: str = "value") -> None:
    """Reject values JSON would coerce, lose, or encode non-standardly.

    This also prevents integer keys from silently becoming strings in traces.
    Callers should only include information they intend to save in artifacts.
    """
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} must contain only finite JSON numbers")
        return
    if isinstance(value, list):
        for item in value:
            validate_json_value(item, label)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"{label} must contain only string object keys")
        for item in value.values():
            validate_json_value(item, label)
        return
    raise ValueError(f"{label} must contain only JSON values")


@dataclass
class AgentOutput:
    output: str
    trajectory: list[str] | None = None
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    metadata: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)


@runtime_checkable
class AgentRunner(Protocol):
    def run(self, input: str) -> AgentOutput: ...
