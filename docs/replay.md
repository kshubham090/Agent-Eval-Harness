# Record, replay, and rescore

Every evaluation JSON file is a recording of the attempted cases. It includes
the input, reference, final response, available tool events, expected and actual
trajectory, grader configuration, individual scores, errors, and reported usage,
cost, and execution times. Recording does not require a telemetry service.

```bash
agent-eval eval --dataset datasets/examples/simple_qa.jsonl \
  --agent examples/stub_agent.py --scorers exact --output run.json
agent-eval replay --result run.json --html replay.html
agent-eval rescore --result run.json --scorers exact,contains \
  --output rescored.json --html rescored.html
```

`replay` validates and renders saved evidence. It does not execute the agent,
recreate its environment, or repeat tool calls. `rescore` applies the selected
graders to each saved response and reference. It matches by case identity,
preserving separate attempts even when several cases have the same prompt.
Single runs and complete repeated-run recordings are supported. An optional
`--pass-threshold` changes the pass rule for the new result; otherwise each
recorded run's threshold is retained. `--concurrency` controls grader workers.

Rescoring never calls the original agent. A selected model judge can make its
own API calls and incur grading cost. Agent cost fields do not include that
cost. Coding-task recordings can be replayed, but cannot be rescored as text:
their score comes from running held-out tests against file changes, which
requires a new coding evaluation.

## Trace contract

An adapter can return an optional `events` list:

```python
from harness.runner import AgentOutput

result = AgentOutput(
    output="The answer is 42.",
    trajectory=["calculator"],
    events=[{
        "type": "tool_result",
        "name": "calculator",
        "result": {"value": 42},
    }],
    usage={"input_tokens": 30, "output_tokens": 8},
)
```

Each event must be an object containing only finite JSON values and string
object keys. Event shapes remain adapter-specific; the harness does not invent
tool calls when a provider exposes only a final answer. Empty `events` means
that no events were recorded, rather than proof that no tools were used.
Only include tool content and metadata that should be retained in local or CI
artifacts. Provider credentials and complete agent environments are not needed
to replay a recording.

## What changes during rescoring

The new result has a new run ID, timestamp, grader configuration, scores, pass
threshold, and derived quality summaries. The original output, case IDs,
references, events, trajectories, tags, agent usage, and cost remain unchanged.
`latency_ms` and `agent_latency_ms` still describe the original execution;
`rescore_latency_ms` measures only the new grading work. Skipped failed agent
attempts have no new grading time. The report separates these meanings.

`metadata.rescore` records the immediate source run IDs, a SHA-256 digest of
the complete source recording, zero original-agent calls, and the timing/cost
semantics. The digest is computed over UTF-8 JSON with sorted keys, no ASCII
escaping, separators `(',', ':')`, and no nonfinite values. It identifies
content; it is not a signature. Rescoring an already rescored file retains
the prior provenance in `metadata.rescore_history`. The new grading process's
harness and Python versions are recorded inside `metadata.rescore`; existing
run-level environment fields continue to describe the original agent run.

Failures are attributed to one of three stages:

| `error_stage` | Meaning | Rescore behavior |
| --- | --- | --- |
| `agent` | Agent execution failed | Preserve failure and zero scores |
| `validation` | Returned output or telemetry was invalid | Preserve failure and zero scores |
| `scorer` | Valid recorded output could not be graded | Retry grading against the saved output |
| `null` with an error | Legacy or unattributed failure | Preserve failure and zero scores |

An ordinary wrong answer is a zero score, without an execution error. An error
always fails, even with a pass threshold of zero. When a previous scoring
failure is retried, its error is retained in case metadata.

## Grader reproducibility

`metadata.grader_config` records each grader's name, qualified Python type,
effective supported settings, and a reproducibility note. Built-in exact and
regex graders retain case sensitivity and full-match settings. Model judges
retain the configured model, prompt template, and default backend prompt and
token limit; embeddings retain the configured model name. Injected backend
callables are identified by name, without serializing closures or clients.

Custom scorers can publish an `evaluation_config()` method returning a JSON
object with the settings that should be saved. Arbitrary instance attributes
are never inspected. Include a rubric/version identifier and any public
parameters needed to reproduce the score.

Repeated-run summaries require the same recorded grader configuration in every
run, including whether configuration was recorded. Baseline comparison rejects
different configurations when both recordings provide one; historical baselines
without grader configuration remain usable with their original limitations.

Deterministic text graders require the same harness version and settings for
exact reproduction. Model aliases, remotely served weights, API behavior,
custom callable code, and dependencies are not frozen by a recording. Model
judge scores may change across rescores, and their new cost is not metered by
the harness. Keep the original artifact to compare both grading outcomes.

## Schema and validation

New recordings use schema version **3**. Relative to version 2, case objects
add `events` (default `[]`), `error_stage` (default `null`), and
`rescore_latency_ms` (default `null`). Existing fields and the pass rule retain
their meanings. A rescore summary adds `rescore_latency_ms` statistics when at
least one case was graded. Original version 2 complete recordings remain
readable; missing trace/stage fields use their defaults. A version 2 error
without a stage cannot become a pass through rescoring.

The loader requires complete raw cases and the supported scoring protocol.
It rejects duplicate JSON keys, nonfinite numbers, duplicate case IDs,
invalid telemetry, unsupported schemas/protocols, and summaries that disagree
with the raw cases. Counts, means, pass rates, per-case scores, trajectory
coverage, cost coverage, latency statistics, and repeated-run summaries are
checked before a replay or gate trusts them. Summary-only legacy baselines
remain usable by baseline commands; they do not contain enough evidence for
replay or rescore. Additional JSON fields are preserved for forward-compatible
annotations.

The same operations are available without the CLI:

```python
from harness.replay import load_recording, rescore_result, validate_recording
from harness.scorers.exact import ExactMatchScorer

source = load_recording("run.json")
rescored = rescore_result(source, [ExactMatchScorer(case_sensitive=False)])
validated_copy = validate_recording(rescored)
```
