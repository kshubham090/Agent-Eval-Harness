"""A local agent backed by Ollama (https://ollama.com) -- zero API cost.

Usage:
    ollama pull llama3.2           # once
    agent-eval eval --dataset datasets/examples/simple_qa.jsonl \
        --agent examples/ollama_agent.py --scorers exact

Override the model with OLLAMA_MODEL, the host with OLLAMA_HOST.
Uses only the standard library -- no extra dependencies.
"""

import json
import os
import urllib.request

from harness.runner import AgentOutput

SYSTEM = (
    "Answer the question with just the answer, as concisely as possible. "
    "No preamble, no punctuation beyond what the answer itself needs."
)


class OllamaAgent:
    def __init__(self, model: str | None = None, host: str | None = None):
        self.model = model or os.environ.get("OLLAMA_MODEL", "llama3.2")
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")

    def run(self, input: str) -> AgentOutput:
        payload = json.dumps({
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": input},
            ],
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.host}/api/chat", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
        return AgentOutput(output=data["message"]["content"].strip())


def get_agent() -> OllamaAgent:
    return OllamaAgent()
