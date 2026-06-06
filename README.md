# Agent Eval Harness

Build a production-grade eval system from scratch. No frameworks — own the concepts first.

---

## What You're Building

```
Golden Dataset  →  Agent Runner  →  Scorer(s)  →  Results  →  Baseline Compare  →  CI Gate
     ↑                                  ↑
  (your test cases)           exact / regex / embedding / llm-judge / trajectory
```

The harness answers one question: **"Is my agent getting better or worse?"**  
And it blocks deploys when the answer is "worse."

---

## Directory Structure

```
agent-eval-harness/
├── harness/
│   ├── dataset.py          # Load + validate golden datasets
│   ├── runner.py           # Abstract agent runner interface
│   ├── scorers/
│   │   ├── base.py         # Scorer Protocol (0.0–1.0 contract)
│   │   ├── exact.py        # Exact string match
│   │   ├── regex_scorer.py # Regex pattern match
│   │   ├── embedding.py    # Cosine similarity via sentence-transformers
│   │   └── llm_judge.py    # LLM-as-judge via Claude API
│   ├── trajectory.py       # Trajectory comparison (LCS-based)
│   ├── eval_runner.py      # Orchestrates dataset × runner × scorers
│   ├── results.py          # Result dataclasses + aggregation
│   ├── baseline.py         # Save/load/compare baseline snapshots
│   └── cli.py              # CLI: eval, baseline save, baseline compare
├── datasets/
│   └── examples/
│       ├── simple_qa.jsonl       # Q&A pairs
│       └── tool_calling.jsonl    # Multi-step trajectory cases
├── baselines/              # Stored baseline JSON snapshots
├── tests/
│   ├── test_scorers.py
│   ├── test_dataset.py
│   ├── test_trajectory.py
│   ├── test_regression.py
│   └── test_meta_eval.py   # Phase 9: eval the judge
├── .github/
│   └── workflows/
│       └── eval.yml        # CI gate
└── pyproject.toml
```

---

## Data Shapes

### Golden Dataset Record (JSONL)
```json
{
  "id": "q1",
  "input": "What is the capital of France?",
  "expected_output": "Paris",
  "expected_trajectory": null,
  "tags": ["geography"]
}
```

Trajectory case:
```json
{
  "id": "t1",
  "input": "Book a flight from NYC to London",
  "expected_output": "Flight booked",
  "expected_trajectory": ["search_flights", "select_cheapest", "confirm_booking"],
  "tags": ["tool-use", "multi-step"]
}
```

### Scorer Protocol
```python
class Scorer(Protocol):
    name: str
    def score(self, expected: str, actual: str) -> float: ...  # 0.0–1.0
```

### Agent Runner Interface
```python
@dataclass
class AgentOutput:
    output: str
    trajectory: list[str] | None = None

class AgentRunner(Protocol):
    def run(self, input: str) -> AgentOutput: ...
```

### Result Shape
```json
{
  "run_id": "abc123",
  "timestamp": "2026-06-06T12:00:00Z",
  "scores": {
    "exact_match": { "mean": 0.75, "per_case": { "q1": 1.0, "q2": 0.0 } },
    "llm_judge":   { "mean": 0.88, "per_case": { "q1": 0.9, "q2": 0.85 } }
  },
  "trajectory_score": { "mean": 0.80 },
  "pass_rate": 0.75
}
```

---

## CLI

```bash
# Run eval
python -m harness eval \
  --dataset datasets/examples/simple_qa.jsonl \
  --agent examples/stub_agent.py \
  --scorers exact embedding llm_judge \
  --output results/run_001.json

# Save baseline
python -m harness baseline save --name v1.0 --result results/run_001.json

# CI gate: exits 1 if any scorer drops > 5% from baseline
python -m harness eval \
  --dataset datasets/examples/simple_qa.jsonl \
  --agent examples/stub_agent.py \
  --compare-baseline v1.0 \
  --threshold 0.05
```

---

## Build Phases

| Phase | What you build | Concept |
|-------|---------------|---------|
| 1 | Dataset loader + result models | What a test case IS |
| 2 | Runner interface + exact match | The eval loop |
| 3 | Regex scorer | Pattern-based assertions |
| 4 | Embedding similarity | Semantic meaning |
| 5 | LLM-as-judge | Subjective quality + bias |
| 6 | Trajectory eval | Agent step correctness |
| 7 | Baseline + regression | CI gates |
| 8 | CLI + CI YAML | Production wiring |
| 9 | Meta-eval | Trusting your evals |

---

## Prerequisites

- Python 3.12+
- Anthropic API key (used from phase 5 onward)
- Git

---

## Dependencies

```toml
[project]
name = "agent-eval-harness"
requires-python = ">=3.12"
dependencies = [
    "anthropic>=0.30",
    "sentence-transformers>=3.0",
    "numpy>=1.26",
    "pytest>=8.0",
    "click>=8.0",
]
```

---

## Verification

End-to-end test: create a stub agent that always returns `"Paris"`, eval against `simple_qa.jsonl`, save as baseline, degrade the agent, re-eval, confirm the CI gate exits 1.

Learning resources for each concept: see [RESOURCES.md](RESOURCES.md)
