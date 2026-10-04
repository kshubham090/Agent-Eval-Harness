Real Docker coding workflow, using known reference patches and unchanged starter files.
These deterministic fixtures are **not AI model benchmark results**.

| Fixture | Passed task attempts | Execution errors | Median batch time | Batch p95 |
| --- | ---: | ---: | ---: | ---: |
| Known reference patches | 9/9 | 0 | 3.18 s | 3.26 s |
| Unchanged buggy starters | 0/9 | 0 | 3.08 s | 3.17 s |

3 interleaved repetitions × 3 tasks per fixture; concurrency 1, no warmup, agent and grader network disabled.
Batch timing includes fresh containers, file transfers, execution, grading, and cleanup. P95 uses empirical nearest rank (the maximum with three trials).
Token use and cost are unreported. These teaching tasks measure the workflow's correctness; they do not establish model quality or a world ranking.

Measured 2026-10-04T09:27:46+00:00 on macOS-26.6.2-arm64-arm-64bit-Mach-O, Python 3.14.7; Docker 29.7.2.
Source `9be8aafc68b5e9963c25b54ef5c0004274149089` (clean); pack `coding-starter@1.0.0`.
Source SHA-256: `0e63e40b994bda9515bd0717e8b753b13396242d0d75176ddd1630a8d41c0335`.
Pack SHA-256: `a0af50bbcdd27d6fd09bf329807c1fcf9996a3d6d9cb5ba5edffc7c94e2850c6`.
Immutable image: `sha256:bb7d0f2b1bbe6a5d8498d7cd3aa38eca9efad4b512e91b7c0b6ab236041a268e`.

Workflow checks: **PASS**. See [full raw trial results](coding-latest.json) and [methodology](../docs/coding.md).
