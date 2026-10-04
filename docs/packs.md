# Versioned benchmark packs

A pack is a local directory containing `pack.toml` plus its data, starter
workspaces, and graders. Packs need no Python plugin installation. Loading or
inspecting a pack validates files and computes a SHA-256 fingerprint without
executing its code.

The wheel includes two original Apache-2.0 packs:

| Pack | Kind | Scope | Intended use |
| --- | --- | --- | --- |
| `coding-starter@1.0.0` | Coding | 3 Python repository repairs, 85 independent checks | Exercise editing, clean workspaces, and grading end to end |
| `json-contracts@1.0.0` | Output | 16 structured-output prompts | Verify existing agents and JSON output contracts |

These small public starters are integration benchmarks. They do not measure
general agent capability, establish production readiness, or provide a fair
leaderboard of models. The JSON pack preserves the repository's original
`benchmarks/agent_smoke.jsonl`; it does not import another benchmark dataset.
Each pack includes a README, LICENSE, and `provenance.toml` explaining its origin.

## Use and inspect a pack

```sh
agent-eval pack list
agent-eval pack inspect coding-starter@1.0.0
agent-eval eval --pack json-contracts@1.0.0 --agent codex --output results/contracts.json
```

The output pack uses the existing agent adapters, including Python functions,
commands, HTTP endpoints, Codex, and Claude Code. Its default scorer is `json`.
The coding pack uses the separate [container workflow](coding.md), where the
agent actually changes files and a fresh container tests the resulting files.
To verify that workflow with the deterministic fixture included in the source
checkout:

```sh
docker build -f examples/Dockerfile.fixture -t agent-eval-fixture:local .
agent-eval coding --pack coding-starter@1.0.0 --image agent-eval-fixture:local \
  --command '["python","/opt/fixture_agent.py"]' --min-pass-rate 1
```

The fixture contains known solutions. A passing fixture run validates the
harness and graders; it is not a measurement of an AI agent's reasoning.

A pack argument accepts a directory, its `pack.toml` path, a bundled ID, or an
explicit bundled `id@version`. An unversioned ID selects the highest bundled
semantic version. Prefer `id@version` for shared commands and retain the resolved
version and full fingerprint in saved results. Existing local paths take
precedence over bundled IDs.

## Create and contribute a pack

```sh
agent-eval pack init ./my-pack --kind coding
agent-eval pack inspect ./my-pack
```

Use `--kind output` for response-scoring tasks. Scaffolding copies the relevant
starter, gives it a new ID and version `0.1.0`, and preserves its license and
source attribution. It refuses any existing destination, including an empty
directory. Edit the generated content and provenance before publishing it.

An output manifest looks like:

```toml
schema_version = 1
id = "my-output-pack"
version = "1.0.0"
name = "Support response contracts"
description = "Check structured routing decisions for synthetic support requests."
license = "Apache-2.0"
kind = "output"

[output]
dataset = "dataset.jsonl"
scorers = ["json"]
```

Its dataset uses the existing strict JSONL format: unique `id`, nonempty `input`
and `expected_output`, and optional `tags` and `expected_trajectory`. Put the
expected JSON response inside the `expected_output` string. Supported scorer
names are `exact`, `regex`, `contains`, `json`, `embedding`, and `llm_judge`.
Optional model-based scorers still need their corresponding installation extras
and credentials.

A coding manifest defines trusted grader commands:

```toml
schema_version = 1
id = "my-coding-pack"
version = "1.0.0"
name = "Python collection repairs"
description = "Repair a collection helper under independently checked contracts."
license = "Apache-2.0"
kind = "coding"

[coding]
image = "python:3.12-slim"

[[tasks]]
id = "stable-dedup"
input = "Repair stable_unique(values) in solution.py; preserve first occurrence order."
workspace = "tasks/stable-dedup/workspace"
grader = "tasks/stable-dedup/grader"
command = ["python", "-I", "-B", "/grader/test_solution.py"]
tags = ["python", "collections"]
timeout = 30
```

Only the task's workspace is supplied to the editing agent. After it exits, the
grader receives the candidate files at `/workspace` and trusted tests at
`/grader`, with `/workspace` as the working directory. A grader exits zero only
when all required checks pass. Commands are argument arrays and are not shell
scripts. Grader timeouts default to 30 seconds and must be greater than zero and
at most 3,600 seconds. Tags default to an empty list. The image must already
contain any dependencies; pin it by digest when publishing reproducible results.

Write complete prompts that specify every graded requirement. Include tests for
ordinary behavior, edge cases, invalid input where specified, and unintended
mutation. Verify a correct implementation, the original broken implementation,
and plausible partial fixes. Check candidate code in a child process and assert
its results in the trusted parent when practical; importing candidate code into
the test runner lets it accidentally or deliberately change the runner itself.
The bundled graders reject a candidate that exits zero without returning valid
probe results. They are not a formal defense against hostile candidate code.

Keep grader directories outside **every** task workspace. Tests are withheld
from the agent's filesystem during editing, but bundled test files are public
and are supplied in the grading container. Do not describe them as secret,
private, or protected against training-data contamination.

For an open-source contribution, record the source and license for every prompt,
reference answer, repository snapshot, and test. Synthetic content should say
how it was authored or generated. Do not redistribute private code or datasets
without permission. Include explicit attribution when adapting an upstream task;
the harness's Apache-2.0 license does not relicense third-party benchmark data.

Publish pack versions as immutable artifacts. Use semantic versioning: change
the major version when task contracts or intended comparisons become
incompatible; use a minor version for additional tasks and a patch version for
corrections. **Any content change produces a different fingerprint**, even when
the version is unchanged, so comparisons should retain both. Document changed
grading rules and regenerate reference evidence rather than silently reusing
old scores.

## Validation and fingerprints

Unknown manifest fields are errors. IDs must contain up to 64 lowercase letters,
digits, dots, underscores, or hyphens and start with a letter or digit. Versions
follow semantic versioning, including valid prerelease/build suffixes. Required
text is nonempty; task IDs, scorer names, and tags cannot contain duplicates.

Paths are relative POSIX paths with exact case. Absolute paths, `..`, backslashes,
symlinks, hard links, special files, case-insensitive collisions, and overlapping workspace
and grader directories are rejected. Portable file names use ASCII letters,
digits, dots, underscores, spaces, and hyphens; reserved Windows names and
trailing dots/spaces are rejected. Files must fit within 8 MiB each, packs within
64 MiB total, manifests within 1 MiB, and the tree within 10,000 entries, 64
directory levels, and 1,024 characters per relative path. A coding pack supports
up to 1,000 tasks. These conservative limits keep loading and copying bounded.
Coding workspaces and candidate transfers also have the tighter per-task limits
listed in the [container workflow](coding.md), including 32 MiB and 4,096 entries.

The fingerprint hashes each relative file path, its executable flag, and its
exact bytes in sorted portable order, including manifest, dataset, workspace,
grader, license, and documentation files. Absolute host paths and timestamps
are excluded. Source files with different line endings or executable flags have
different fingerprints because their transferred contents or execution behavior
differ. The fingerprint identifies the pack; also record resolved images, agent
configuration, prompts, harness version, and grading outcomes to reproduce a run.

Python callers can use `load_pack(path_or_name)`, `list_packs()`, and
`init_pack(destination, kind="coding")` from `harness.packs`. `list_packs()` returns
validated `BenchmarkPack` objects for every bundled version. Invalid packs raise
`PackError`, a `ValueError` subclass. Loading validates metadata and files; it
does not establish that an arbitrary community grader is fair or correct.
