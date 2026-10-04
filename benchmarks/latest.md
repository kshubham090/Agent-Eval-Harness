# Reproducible harness benchmark

**Scope: deterministic harness operations, not a model leaderboard.** No API credentials or model calls.

Measured 2026-10-04T07:38:23+00:00 on macOS-26.6.2-arm64-arm-64bit-Mach-O (arm64; Python 3.14.7; 10 logical CPUs).

Git revision: `348189aeb616104df8f59c2a922513962e14a4a3`; working tree dirty: `False`. Source file hashes and Git status are recorded in the JSON result.

Suite SHA-256: `94ae5312b4a6c3bcfac7a5c5b0199541e2d8851a3d449906ac57470e4a76a38b`

Reproduction command (local interpreter path normalized to `python`):

```sh
python scripts/benchmark.py --output benchmarks/latest.json --markdown benchmarks/latest.md
```

## Controlled waiting and concurrency

128 generated arithmetic cases per batch, 5 ms requested sleep per case; 1 warmup(s) excluded and 5 measured repetitions per concurrency level.

| Concurrency | Median batch ms | P95 batch ms | Min–max ms | Median cases/s | Speedup vs serial |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 875.88 | 888.64 | 872.38–888.64 | 146.1 | 1.00× |
| 2 | 436.61 | 449.92 | 423.99–449.92 | 293.2 | 2.01× |
| 4 | 222.24 | 225.65 | 219.69–225.65 | 576.0 | 3.94× |
| 8 | 109.68 | 114.07 | 108.80–114.07 | 1167.0 | 7.99× |

P95 uses the nearest-rank method over batch repetitions. With five samples it is the maximum. This small sample describes this run; it is not a stable service latency estimate. Actual sleep can exceed the requested duration. Concurrency levels are interleaved and their execution order alternates.

## Incremental harness overhead

4096 no-wait arithmetic cases, serial execution, paired direct runner + scoring loop versus full evaluation. The pair execution order alternates. Generation/import time is excluded.

| Measurement | Median | P95 | Min–max |
| --- | ---: | ---: | ---: |
| Direct loop | 2.628 ms | 2.688 ms | 2.554–2.688 ms |
| Full harness | 23.024 ms | 23.166 ms | 22.901–23.166 ms |
| Paired incremental work per case | 4.965 µs | 5.014 µs | 4.947–5.014 µs |

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
