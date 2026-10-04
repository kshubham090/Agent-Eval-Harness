Real Docker coding workflow, using known reference patches and unchanged starter files.
These deterministic fixtures are **not AI model benchmark results**.

| Fixture | Passed task attempts | Execution errors | Median batch time | Batch p95 |
| --- | ---: | ---: | ---: | ---: |
| Known reference patches | 9/9 | 0 | 3.00 s | 3.05 s |
| Unchanged buggy starters | 0/9 | 0 | 2.85 s | 3.23 s |

3 interleaved repetitions × 3 tasks per fixture; concurrency 1, no warmup, agent and grader network disabled.
Batch timing includes fresh containers, file transfers, execution, grading, and cleanup. P95 uses empirical nearest rank (the maximum with three trials).
Token use and cost are unreported. These teaching tasks measure the workflow's correctness; they do not establish model quality or a world ranking.

Measured 2026-10-04T08:56:32+00:00 on macOS-26.6.2-arm64-arm-64bit-Mach-O, Python 3.14.7; Docker 29.7.2.
Source `8a3adb57a46bc8cabfcc872a3f83c0853c5a0dc4` (clean); pack `coding-starter@1.0.0`.
Source SHA-256: `d04638b6382ff30739dc80aeffc6c7bc6a8d152e73fcdeea01f41479f099f95d`.
Pack SHA-256: `b16c5920076b65abb8828693ef1ace80c203ca9473b0590f29974fe40f72e6fd`.
Immutable image: `sha256:bb7d0f2b1bbe6a5d8498d7cd3aa38eca9efad4b512e91b7c0b6ab236041a268e`.

Workflow checks: **PASS**. See [full raw trial results](coding-latest.json) and [methodology](../docs/coding.md).
