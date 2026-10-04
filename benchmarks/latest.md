# Reproducible harness benchmark

**Scope: deterministic harness operations, not a model leaderboard.** No API credentials or model calls.

Measured 2026-10-04T08:56:23+00:00 on macOS-26.6.2-arm64-arm-64bit-Mach-O (arm64; Python 3.14.7; 10 logical CPUs).

Git revision: `8a3adb57a46bc8cabfcc872a3f83c0853c5a0dc4`; working tree dirty: `False`. Source file hashes and Git status are recorded in the JSON result.

Suite SHA-256: `5e02115f201b63b2c3c08d6b2a9db83d340892f2056095c66ca88cb145726716`

Reproduction command (local interpreter path normalized to `python`):

```sh
python scripts/benchmark.py --output results/v3-benchmarks/latest.json --markdown results/v3-benchmarks/latest.md
```

## Controlled waiting and concurrency

128 generated arithmetic cases per batch, 5 ms requested sleep per case; 1 warmup(s) excluded and 5 measured repetitions per concurrency level.

| Concurrency | Median batch ms | P95 batch ms | Min–max ms | Median cases/s | Speedup vs serial |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 780.53 | 784.84 | 775.42–784.84 | 164.0 | 1.00× |
| 2 | 390.83 | 401.85 | 388.39–401.85 | 327.5 | 2.00× |
| 4 | 194.05 | 199.78 | 192.44–199.78 | 659.6 | 4.02× |
| 8 | 99.61 | 100.79 | 98.19–100.79 | 1285.0 | 7.84× |

P95 uses the nearest-rank method over batch repetitions. With five samples it is the maximum. This small sample describes this run; it is not a stable service latency estimate. Actual sleep can exceed the requested duration. Concurrency levels are interleaved and their execution order alternates.

## Incremental harness overhead

4096 no-wait arithmetic cases, serial execution, paired direct runner + scoring loop versus full evaluation. The pair execution order alternates. Generation/import time is excluded.

| Measurement | Median | P95 | Min–max |
| --- | ---: | ---: | ---: |
| Direct loop | 2.794 ms | 2.838 ms | 2.703–2.838 ms |
| Full harness | 26.991 ms | 27.402 ms | 26.935–27.402 ms |
| Paired incremental work per case | 5.919 µs | 6.027 µs | 5.894–6.027 µs |

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
