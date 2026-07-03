"""Stub agent for testing the harness: always answers 'Paris'.

Right for simple_qa's q1, wrong for everything else -- a stable, deterministic
fixture for exercising the eval loop and the CI gate.
"""

from harness.runner import AgentOutput


class StubAgent:
    def run(self, input: str) -> AgentOutput:
        return AgentOutput(output="Paris")


def get_agent() -> StubAgent:
    return StubAgent()
