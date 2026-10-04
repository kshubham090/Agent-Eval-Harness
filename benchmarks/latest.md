# Reproducible harness benchmark

**Scope: deterministic harness operations, not a model leaderboard.** No API credentials or model calls.

Measured 2026-10-04T10:08:09+00:00 on macOS-26.6.2-arm64-arm-64bit-Mach-O (arm64; Python 3.14.7; 10 logical CPUs).

Git revision: `3ab5db20f8cf35becca5892d10f2e9e1796dee20`; working tree dirty: `False`. Source file hashes and Git status are recorded in the JSON result.

Suite SHA-256: `a32d00815c822815b80a1b5e918b6cef4dfa8bff66c1a268ed6f93573207a9dc`

Reproduction command (local interpreter path normalized to `python`):

```sh
python scripts/benchmark.py --output results/final-review-benchmarks/latest.json --markdown results/final-review-benchmarks/latest.md
```

## Controlled waiting and concurrency

128 generated arithmetic cases per batch, 5 ms requested sleep per case; 1 warmup(s) excluded and 5 measured repetitions per concurrency level.

| Concurrency | Median batch ms | P95 batch ms | Min–max ms | Median cases/s | Speedup vs serial |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 776.33 | 793.38 | 767.81–793.38 | 164.9 | 1.00× |
| 2 | 391.79 | 394.10 | 390.20–394.10 | 326.7 | 1.98× |
| 4 | 193.98 | 195.04 | 191.48–195.04 | 659.9 | 4.00× |
| 8 | 98.76 | 100.21 | 97.50–100.21 | 1296.1 | 7.86× |

P95 uses the nearest-rank method over batch repetitions. With five samples it is the maximum. This small sample describes this run; it is not a stable service latency estimate. Actual sleep can exceed the requested duration. Concurrency levels are interleaved and their execution order alternates.

## Incremental harness overhead

4096 no-wait arithmetic cases, serial execution, paired direct runner + scoring loop versus full evaluation. The pair execution order alternates. Generation/import time is excluded.

| Measurement | Median | P95 | Min–max |
| --- | ---: | ---: | ---: |
| Direct loop | 2.709 ms | 2.770 ms | 2.636–2.770 ms |
| Full harness | 27.087 ms | 27.142 ms | 26.953–27.142 ms |
| Paired incremental work per case | 5.965 µs | 5.970 µs | 5.904–5.970 µs |

Incremental work includes result objects, validation, per-case timing and aggregation. It is a measured difference from a minimal loop, not a claim about every integration or a pure profiler attribution.

## Deterministic functional checks

Overall verification: **PASS**.

Correct calculator: 100%; deliberately wrong multiplication: 75%; injected subtraction failures: 32/128 with 75% passing. The regression threshold is 1% absolute.

| Check | Observed |
| --- | --- |
| correct arithmetic and order | PASS |
| known degradation exactly detected | PASS |
| regression gate rejects degraded runner | PASS |
| regression gate accepts unchanged runner | PASS |
| all cases survive injected errors | PASS |
| injected errors are isolated | PASS |
| errored cases score zero | PASS |
| healthy cases still pass | PASS |
| injected errors reduce pass rate | PASS |

These are functional checks, not estimates of model accuracy or CI reliability.

## Limits

- No model, agent CLI, network service or LLM judge is called; no API credentials are used.
- Sleep-based concurrency measures controlled waiting, not real provider throughput or rate limits.
- Arithmetic is solved by deterministic code; 100% here is fixture correctness, not agent intelligence.
- Timing depends on this machine and its current load; small sample P95 is descriptive, not a stable tail estimate.
- Incremental overhead includes result construction, validation, per-case timing and aggregation, compared with a minimal runner/scorer loop.
- No cross-framework comparisons, model rankings, token counts or monetary costs are measured.
