# Python repository repair starter — 1.0.0

Three original, deliberately small repair tasks: Unicode slug normalization,
stable deduplication, and merging closed intervals. Each workspace contains
`solution.py` with a buggy implementation and a README. The task prompt specifies
the complete required behavior; tests must not impose unstated requirements.

Independent standard-library graders check ordinary examples, edge cases, input
mutation, and deterministic generated inputs. The grader calls candidate code in
a child process and checks returned values in its own process, so exiting the
candidate process with status zero without returning results does not pass.
Candidate functions may import helper modules from their workspace; expected
answers and comparisons remain in the separate grader parent process.

The harness gives the agent only the selected task's `workspace/`. After the
agent exits, a fresh grading container receives the candidate files and the
trusted `grader/` directory. These tests are **withheld at agent runtime**, not
private data: all tests are published in this repository and wheel. This pack
does not establish resistance to benchmark contamination or deliberately
adversarial code, and three tasks cannot rank general coding ability.

`python:3.12-slim` is the default image. It is a mutable upstream tag; retain the
resolved image ID reported by the harness, or supply an image pinned by digest
for reproducible comparisons. No third-party Python packages are needed.

All content was authored for Agent Eval Harness and is licensed under
Apache-2.0; see LICENSE and provenance.toml. Change the pack version when changing
prompts, starter files, graders, dependencies, or expected behavior. Published
versions should be immutable; use the full content fingerprint in reports.
