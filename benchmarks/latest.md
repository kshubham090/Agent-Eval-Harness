# Reproducible harness benchmark

**Scope: deterministic harness operations, not a model leaderboard.** No API credentials or model calls.

Measured 2026-10-04T09:27:29+00:00 on macOS-26.6.2-arm64-arm-64bit-Mach-O (arm64; Python 3.14.7; 10 logical CPUs).

Git revision: `9be8aafc68b5e9963c25b54ef5c0004274149089`; working tree dirty: `False`. Source file hashes and Git status are recorded in the JSON result.

Suite SHA-256: `b64951de893dd80e2920981159d1bc70dce8619c45c7a377e973f3d0655387af`

Reproduction command (local interpreter path normalized to `python`):

```sh
python scripts/benchmark.py --output results/review-benchmarks/latest.json --markdown results/review-benchmarks/latest.md
```

## Controlled waiting and concurrency

128 generated arithmetic cases per batch, 5 ms requested sleep per case; 1 warmup(s) excluded and 5 measured repetitions per concurrency level.

| Concurrency | Median batch ms | P95 batch ms | Min–max ms | Median cases/s | Speedup vs serial |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 790.65 | 792.70 | 775.91–792.70 | 161.9 | 1.00× |
| 2 | 392.29 | 397.20 | 385.71–397.20 | 326.3 | 2.02× |
| 4 | 197.82 | 199.61 | 194.80–199.61 | 647.1 | 4.00× |
| 8 | 100.22 | 101.24 | 99.60–101.24 | 1277.2 | 7.89× |

P95 uses the nearest-rank method over batch repetitions. With five samples it is the maximum. This small sample describes this run; it is not a stable service latency estimate. Actual sleep can exceed the requested duration. Concurrency levels are interleaved and their execution order alternates.

## Incremental harness overhead

4096 no-wait arithmetic cases, serial execution, paired direct runner + scoring loop versus full evaluation. The pair execution order alternates. Generation/import time is excluded.

| Measurement | Median | P95 | Min–max |
| --- | ---: | ---: | ---: |
| Direct loop | 2.665 ms | 2.756 ms | 2.621–2.756 ms |
| Full harness | 26.737 ms | 26.912 ms | 26.590–26.912 ms |
| Paired incremental work per case | 5.885 µs | 5.897 µs | 5.841–5.897 µs |

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
