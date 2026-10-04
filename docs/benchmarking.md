# Benchmarking with evidence

The repository includes an offline benchmark of harness overhead, concurrency,
error isolation and regression detection. Its arithmetic calculator is a
deterministic fixture. It makes no calls to Codex, Claude Code, a hosted model,
or an LLM judge, and it is **not an agent intelligence leaderboard**.

From the repository root, after activating Python 3.12+:

```sh
python scripts/benchmark.py --output benchmarks/latest.json --markdown benchmarks/latest.md
```

No dependencies beyond the Python standard library and this checkout are
needed. A default run usually takes around ten seconds; machine load and timer
resolution affect this. The script exits with status 1 if a functional
verification fails. It does not fail on machine-dependent speed thresholds.

The [JSON evidence](../benchmarks/latest.json) includes raw timing samples;
the [generated summary](../benchmarks/latest.md) is a readable view of the same
measurements. Rerunning overwrites those two files. Use different `--output`
and `--markdown` paths to retain each measurement.

## What is measured

| Workload | Default | What the result means |
| --- | --- | --- |
| Incremental overhead | 4,096 cases; serial; no sleep | Full evaluation minus a paired direct calculator and exact-scoring loop. Includes result construction, validation, per-case timing and aggregation. |
| Controlled waiting | 128 cases; requested 5 ms sleep per case; concurrency 1, 2, 4, 8 | Whether the harness overlaps waiting and what throughput this machine achieves on a synthetic I/O-shaped workload. |
| Correct arithmetic | 128 seeded cases | The calculator parses the prompt, computes an integer and is graded by exact match. References are independently generated; the runner receives no answer map. |
| Deliberate degradation | Multiplication is wrong by 1 on exactly 25% of cases | The scorer observes 75% passing and the baseline gate rejects the regression with a 1 percentage point threshold. |
| Injected errors | Subtraction raises on exactly 25% of cases | All cases remain in order, each error scores zero, and healthy cases still pass. The run completes at 75% passing. |

The generated integer operations are balanced across addition, subtraction,
multiplication and floor division, with nonzero divisors and a fixed seed.
The calculator parses arbitrary prompts in this grammar; it does not retrieve
the expected answer from the dataset. The test suite also verifies an unseen
input independently of the generated fixture.

One warmup and five measured repetitions are the defaults. For controlled
waiting, concurrency levels run in interleaved rounds and the order reverses
every other round. For overhead, direct-loop and harness order alternate.
Dataset generation, imports and file writing are outside the timing window.
The harness window includes execution, scoring and aggregation, and starts
after runner/scorer construction. Warmup samples are retained separately and
excluded from every reported timing summary.

Batch P95 is the nearest-rank quantile of the measured batch wall times. With
five repetitions it is the maximum sample, so it is not a precise estimate of
a service's tail latency. Median, minimum, maximum and sample standard
deviation describe observed timing spread; they are not confidence intervals.
Speedup is the ratio of serial median batch time to concurrent median batch
time. Per-case raw latency is included in JSON and excludes queue waiting,
because that timer begins when the worker starts a case. It includes the
runner and scorer and excludes final batch aggregation.

Use more repetitions to inspect timing stability:

```sh
python scripts/benchmark.py --repetitions 20 --warmups 2 \
  --output benchmarks/local-20.json --markdown benchmarks/local-20.md
```

The CLI also accepts `--cases`, `--overhead-cases`, `--latency-ms` and `--seed`.
Case counts must be multiples of four to preserve the exact known fault rate.

## Provenance and reproduction

Each JSON result records:

- Reproduction invocation (interpreter path normalized to `python`), start/end
  UTC timestamps, Python version, OS, architecture
  and logical CPU count.
- Git revision, dirty status and changed paths at the start of the run.
- SHA-256 hashes for the benchmark implementation and all harness source files.
- A suite fingerprint covering configuration, generated datasets and source.
- Every measured and warmup batch timing, case count, correctness outcome,
  injected failure and observed regression metric.

A dirty working tree means the Git revision alone does not reconstruct the
measured source. Use the published source hashes and the corresponding changed
files. The suite fingerprint intentionally changes if harness source,
benchmark source, configuration or generated fixtures change. It does not
contain timestamp or timing measurements. For comparisons, keep the runtime,
hardware, workload, scoring and configuration fixed and record any intended
source changes. Close unrelated heavy workloads when measuring local overhead.

## What this cannot establish

Synthetic sleep is controlled waiting, not a network request. These numbers
do not predict a provider's rate limits, agent process startup, tool execution,
tokens per second, cost, quality, memory usage or end-to-end coding ability.
Thread speedup on waiting cannot be extrapolated to CPU-bound agents. This
benchmark makes no comparison against other evaluation frameworks.

The perfect calculator's pass rate and injected failures are deterministic
functional checks. They do not measure the reliability of CI, a model, or an
agent. More fixture repetitions do not turn them into model accuracy data.

## Measuring your existing agent

Use the harness's Python, command, HTTP, Codex or Claude Code integration with
your own task suite. Real-agent results should publish the dataset revision,
task selection, adapter command, model/provider/version, agent configuration,
prompt, permissions, environment, concurrency and repetition count. Record
provider usage and cost only when measured; absent usage is unknown, not free.

Build checkable tasks that represent your workload: repository changes with
tests, structured output contracts, retrieved answers with references, or
tool trajectories. Hold evaluation cases apart from prompt tuning. Inspect
judge agreement on a separate human-labeled set before using a model's grade
to compare systems. Repeat stochastic runs and inspect per-case outcomes;
an aggregate score alone can conceal severe regressions on one task category.

## Historical baseline provenance

The existing `baselines/claude-v1.json` is a historical artifact dated
2026-07-05. It contains 60 case outcomes and its dataset fingerprint matches
`datasets/examples/simple_qa.jsonl`. It records exact-match mean 0.8667,
embedding mean 0.9841, judge mean 1.0 and pass rate 0.9833. Those are different
graders applied to the same stored answers, not a comparison of three agents.

The artifact records an agent file path and scorer names, but does not record
the resolved agent model, judge model, dependency versions, prompt revision,
invocation, hardware, token usage, cost or repeated trials. Its live-call
provenance has not been independently reproduced here. A current example's
default model cannot establish which model produced a historical result.
Treat it as an inspectable example snapshot, not a verified leaderboard or
evidence that a judge is always correct.

`baselines/ci.json` uses the always-Paris stub and passes one of 60 cases. It
demonstrates the original gate wiring; that score measures a deliberately
limited fixture, not a usable agent.
