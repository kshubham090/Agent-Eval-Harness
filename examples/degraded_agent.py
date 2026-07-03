"""A deliberately worse agent: always answers 'London'.

Use it to watch the regression gate catch a degradation:
stub_agent gets q1 right (1/5); this gets everything wrong (0/5).
"""

from harness.runner import AgentOutput


class DegradedAgent:
    def run(self, input: str) -> AgentOutput:
        return AgentOutput(output="London")


def get_agent() -> DegradedAgent:
    return DegradedAgent()
