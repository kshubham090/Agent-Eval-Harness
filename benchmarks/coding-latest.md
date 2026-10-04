Real Docker coding workflow, using known reference patches and unchanged starter files.
These deterministic fixtures are **not AI model benchmark results**.

| Fixture | Passed task attempts | Execution errors | Median batch time | Batch p95 |
| --- | ---: | ---: | ---: | ---: |
| Known reference patches | 9/9 | 0 | 4.63 s | 4.64 s |
| Unchanged buggy starters | 0/9 | 0 | 4.73 s | 6.11 s |

3 interleaved repetitions × 3 tasks per fixture; concurrency 1, no warmup, agent and grader network disabled.
Batch timing includes fresh containers, file transfers, execution, grading, and cleanup. P95 uses empirical nearest rank (the maximum with three trials).
Token use and cost are unreported. These teaching tasks measure the workflow's correctness; they do not establish model quality or a world ranking.

Measured 2026-10-04T09:02:12+00:00 on macOS-26.6.2-arm64-arm-64bit-Mach-O, Python 3.14.7; Docker 29.7.2.
Source `92d31c7500a3d0c049d090eaf893451b5baef93b` (clean); pack `coding-starter@1.0.0`.
Source SHA-256: `bff4568ca96659194539ad4de918cb8f73fad1b82676feb7cdc79e55760ef612`.
Pack SHA-256: `b16c5920076b65abb8828693ef1ace80c203ca9473b0590f29974fe40f72e6fd`.
Immutable image: `sha256:bb7d0f2b1bbe6a5d8498d7cd3aa38eca9efad4b512e91b7c0b6ab236041a268e`.

Workflow checks: **PASS**. See [full raw trial results](coding-latest.json) and [methodology](../docs/coding.md).
