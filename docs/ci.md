# CI gates and GitHub Action

The composite action evaluates an existing agent configuration, records JSON
and standalone HTML, checks gates and uploads evidence even when a gate fails.
It installs the harness from the action revision. Provide Python 3.12+ and your
agent's dependencies before invoking it. No model provider is required by the
action itself.

Pin `FULL_COMMIT_SHA` below to a reviewed commit in this repository. A placeholder
is shown because no `v0.3.0` release has been published yet.

```yaml
name: Agent evaluation
on: [pull_request]
permissions:
  contents: read
jobs:
  evaluate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      # Install your agent's own dependencies here.
      - uses: kshubham090/Agent-Eval-Harness@FULL_COMMIT_SHA
        with:
          config: evals/agent-eval.toml
          min-pass-rate: '0.95'
          max-p95-latency-ms: '30000'
          # Enable only when your adapter reports cost on every case:
          # max-cost-usd: '2.00'
```

Configurations may use Python, commands, HTTP, Codex or Claude Code through the
existing adapters. Optional scorer dependencies are installed separately:
`python -m pip install anthropic` for the Anthropic judge, or the documented
embedding extra dependencies. The action never changes an agent's login.

For Docker coding evaluation, build or pull a trusted image first:

```yaml
- uses: kshubham090/Agent-Eval-Harness@FULL_COMMIT_SHA
  with:
    mode: coding
    pack: coding-starter@1.0.0
    image: my-agent:tested
    command: '["my-agent", "--stdin"]'
    min-pass-rate: '1'
    artifact-name: coding-evaluation
```

`command` is a JSON argv array, never a shell string. Coding defaults to no
network. When a trusted agent needs provider access, explicitly select `network:
bridge` and list required variable **names** in `agent-env` (one per line); supply
values through the action step's `env`, never inside `command`. Graders always
have no network and receive none of those forwarded values. Provider secrets
should not be exposed to untrusted pull requests. Credentialed evaluations
belong in a trusted/manual workflow; see [security guidance](../SECURITY.md).

## Gate definitions

| Input/CLI flag | Meaning | Missing data |
| --- | --- | --- |
| `min-pass-rate` | Minimum pass fraction in **every** run | Invalid recordings rejected |
| `max-error-rate` | Maximum error fraction in **every** run; default 0 | Invalid recordings rejected |
| `max-p95-latency-ms` | Maximum p95 **agent** latency in every run | Fails if any case lacks timing |
| `max-cost-usd` | Maximum **total agent cost across all runs** | Fails if any case lacks cost |

P95 uses linear interpolation as recorded by the result schema. Repeated runs
cannot hide a bad run behind an average. All gates recompute/validate metrics
against raw cases, so editing a summary cannot produce a passing gate.

Cost gates run **after** evaluation. They are acceptance checks, not a mechanism
to cancel requests at a spending cap. Costs cover agent-reported usage only;
optional LLM grader fees are not measured. Coding's generic command interface
does not parse model billing from stdout, so a cost gate on it fails for missing
coverage. Rescoring uses the original agent cost and latency, with new grading
time recorded separately.

The existing `eval --min-pass-rate` retains its repeated-run **mean** behavior
for compatibility. Use the action or `gate` for strict per-run floors. Any
stricter baseline or quality rules inside the supplied configuration also apply.

Run the same gates locally:

```sh
agent-eval gate --result results/run.json --min-pass-rate 0.95 \
  --max-p95-latency-ms 30000 --output results/gates.json
```

The action's `result`, `report` and `gates` outputs contain absolute artifact
paths. `output-directory` must be a new directory inside the repository; an
existing directory is rejected to preserve earlier evidence. For matrix jobs,
use a unique `artifact-name` per job. Set `upload-artifacts: 'false'` to retain
results on the runner without automatic upload. The job summary contains only
gate values; full prompts, outputs and tool traces remain in artifacts.

Exit 0 means evaluation and all selected gates passed. Nonzero means a failed
gate, malformed configuration/evidence, or an execution failure. Artifacts are
written before quality gates so failures can be investigated.
