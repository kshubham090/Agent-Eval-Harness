# Changelog

## 0.3.0 (unreleased)

- Evaluate coding-task file edits in disposable Docker workspaces, then grade
  the frozen candidate in a separate container with independent tests.
- Discover and author versioned, content-fingerprinted coding and output packs;
  starter packs ship inside the wheel.
- Record structured events, grader settings and error stages in result schema 3.
  Replay full schema-2/3 recordings and rescore output without new agent calls.
- Gate each run's quality and agent latency, plus total reported agent cost,
  with explicit missing-measurement failures.
- Use the composite GitHub Action with local configurations or coding packs.
- Add Apache-2.0 licensing, contributor/security guides and release automation.

## 0.2.0

- Universal Python, command, HTTP, Codex and Claude Code adapters.
- Strict JSONL/config validation, output and trajectory scoring, saved baselines,
  repeated runs, usage coverage, dataset slices and standalone HTML reports.
- Reproducible offline harness benchmarks and cross-platform CI.
