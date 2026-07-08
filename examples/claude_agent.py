"""A real agent backed by the Anthropic API.

Usage:
    agent-eval eval --dataset datasets/examples/simple_qa.jsonl \
        --agent examples/claude_agent.py --scorers exact,llm_judge

Needs ANTHROPIC_API_KEY (the CLI loads it from .env automatically).
Override the model with AGENT_MODEL, e.g. AGENT_MODEL=claude-haiku-4-5.
"""

import os

from harness.runner import AgentOutput

DEFAULT_MODEL = "claude-opus-4-8"

SYSTEM = (
    "Answer the question with just the answer, as concisely as possible. "
    "No preamble, no punctuation beyond what the answer itself needs."
)


class ClaudeAgent:
    def __init__(self, model: str | None = None):
        from anthropic import Anthropic  # deferred so importing the file never needs the SDK

        self.model = model or os.environ.get("AGENT_MODEL", DEFAULT_MODEL)
        # generous retries: concurrent eval runs burst-hit rate limits, and the
        # SDK backs off using the server's retry-after
        self.client = Anthropic(max_retries=8)

    def run(self, input: str) -> AgentOutput:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM,
            messages=[{"role": "user", "content": input}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        return AgentOutput(output=text.strip())


def get_agent() -> ClaudeAgent:
    return ClaudeAgent()
