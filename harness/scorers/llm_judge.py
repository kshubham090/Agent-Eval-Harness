"""LLM-as-judge scorer: ask a model to grade the answer against the expected one.

Where exact/regex/embedding measure text, a judge can weigh *correctness and
quality* ("is this a helpful, factually right answer?"). The price is that the
judge has its own biases -- which is why Phase 9 (meta-eval) evaluates the
judge itself.

Like EmbeddingScorer, the backend is injectable: any complete_fn mapping a
prompt string to the model's raw text response works. The default backend
calls the Anthropic API (install with: pip install .[judge], needs
ANTHROPIC_API_KEY). Judge duty is a simple grading task, so the default model
is Haiku (cheapest -- ~$0.001/case); pass model= to trade up.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable

CompleteFn = Callable[[str], str]

DEFAULT_MODEL = "claude-haiku-4-5"

JUDGE_SYSTEM_PROMPT = """\
You are grading an AI agent's answer against a reference answer.

Score how well the actual answer matches the meaning and correctness of the
expected answer:
- 1.0: fully correct and equivalent to the expected answer
- 0.5: partially correct (right direction, missing or wrong details)
- 0.0: incorrect, contradictory, or unrelated

Grading rules:
- NAMING the answer is what matters. If the actual answer states the expected
  answer -- even by a shortened but unambiguous form (a surname like
  "Einstein" for "Albert Einstein", a standard abbreviation) -- it is fully
  correct: score 1.0, even if phrased inside a sentence.
- DESCRIBING without naming is only partial. If the actual answer merely
  describes or hints at the expected answer without stating it, score 0.5.
- Additional correct information must NOT reduce the score. If the expected
  answer is stated and right, extra accurate context is still 1.0.
- Only deduct for missing, wrong, or contradictory content.

Respond with ONLY a JSON object, no other text:
{"score": <float 0.0-1.0>, "reasoning": "<one short sentence>"}"""

JUDGE_PROMPT_TEMPLATE = """\
Expected answer:
{expected}

Actual answer:
{actual}"""


class JudgeError(ValueError):
    """Raised when the judge's response can't be parsed into a score."""


def _make_anthropic_backend(model: str) -> CompleteFn:
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise ImportError(
            "LLMJudgeScorer needs the anthropic package; install with: pip install .[judge]"
        ) from e

    client = Anthropic(max_retries=8)  # concurrent eval runs burst-hit rate limits

    def complete(prompt: str) -> str:
        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=JUDGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text")

    return complete


def parse_judge_response(raw: str) -> float:
    """Extract the score from the judge's response, tolerating extra prose."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise JudgeError(f"no JSON object in judge response: {raw!r}")
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError as e:
        raise JudgeError(f"invalid JSON in judge response: {raw!r}") from e

    score = data.get("score")
    if not isinstance(score, (int, float)):
        raise JudgeError(f"judge response missing numeric 'score': {raw!r}")
    return max(0.0, min(1.0, float(score)))


class LLMJudgeScorer:
    name = "llm_judge"

    def __init__(self, complete_fn: CompleteFn | None = None, model: str = DEFAULT_MODEL):
        self.model = model
        self._complete = complete_fn
        self._init_lock = threading.Lock()

    def score(self, expected: str, actual: str) -> float:
        if self._complete is None:
            # concurrent eval threads must not each build their own backend
            with self._init_lock:
                if self._complete is None:
                    self._complete = _make_anthropic_backend(self.model)
        prompt = JUDGE_PROMPT_TEMPLATE.format(expected=expected, actual=actual)
        return parse_judge_response(self._complete(prompt))
