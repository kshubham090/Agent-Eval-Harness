"""Abstract agent runner interface.

The harness never knows how an agent works internally -- anything that can
take an input string and return an AgentOutput can be evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class AgentOutput:
    output: str
    trajectory: list[str] | None = None


@runtime_checkable
class AgentRunner(Protocol):
    def run(self, input: str) -> AgentOutput: ...
