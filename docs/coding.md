# Evaluate repository edits

Coding packs give an agent a fresh repository, then test its edited files in a
second, fresh container. The prompt and starter files are available to the
agent. Grader files are copied only into the grader container. The included
`coding-starter@1.0.0` pack has three small Python repair tasks; it is a workflow
smoke test, not a representative coding leaderboard.

You need a working Docker daemon using Linux containers. Images must contain
`/bin/sh` and `sleep`, your agent executable, and all dependencies required by
the task and grader. Install dependencies when building the image: graders
never have network access. Images declaring Docker `VOLUME` entries are rejected
because the harness explicitly controls every writable mount. The Docker local
volume driver must support tmpfs (Docker Desktop's Linux VM and ordinary Linux
Docker support this).

```sh
docker pull python:3.12-slim
agent-eval pack list
agent-eval coding --pack coding-starter@1.0.0 \
  --image my-existing-agent:latest \
  --command '["my-agent", "--read-prompt-from-stdin"]' \
  --artifacts artifacts/coding --output results/coding.json \
  --html results/coding.html --min-pass-rate 1
```

The command is an argument array, executes directly with `/workspace` as its
working directory, and receives the prompt on standard input. There is no shell
interpolation or prompt placeholder expansion. Wrap an existing agent with a
small executable if its input interface differs. All edits must stay inside
`/workspace`; temporary files may use `/tmp`. The user is UID/GID 65534 and
`HOME` is `/home/agent`, a fresh writable 64 MiB tmpfs for agent configuration
and caches. It is separate from `/tmp` so CLIs such as Codex can create their
helper executables. Each grader receives its own empty home; agent credentials
and configuration are never copied into it. Bake executables into a globally readable location such as `/opt`.
Agent-specific permissions still need to permit writing this workspace.

### Codex and other installed CLIs

For the existing host CLI and login, use `agent-eval eval --agent codex` for
output tasks as described in [integrations](integrations.md). Container coding
tasks need the executable inside the image and explicit authentication; your
host home directory and login files are not copied into it.

The repository supplies an image recipe for npm-distributed CLIs, Python
graders and Git. Pin the package version you intend to evaluate:

```sh
docker build -f examples/Dockerfile.cli \
  --build-arg AGENT_PACKAGE=@openai/codex@0.160.0 \
  -t my-codex:0.160.0 .
agent-eval coding --pack coding-starter@1.0.0 --image my-codex:0.160.0 \
  --command '["codex", "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "danger-full-access", "-"]' \
  --network bridge --env CODEX_API_KEY \
  --output results/codex-coding.json --html results/codex-coding.html
```

Set `CODEX_API_KEY` through your secret manager or local environment before
running this command. The explicit Codex sandbox option lets it edit files
inside the outer Docker sandbox. `--skip-git-repo-check` supports starter
workspaces without a `.git` directory; `-` reads the full prompt from stdin.
These behaviors follow the [official noninteractive Codex documentation](https://developers.openai.com/codex/noninteractive).
Choose a model explicitly in your command when comparing runs. No authenticated
Codex inference was performed for this release's fixture benchmarks.

For Claude Code, build the same recipe with an exact reviewed
`@anthropic-ai/claude-code@VERSION` package and supply a print-mode command:

```sh
agent-eval coding --pack coding-starter@1.0.0 --image my-claude:tested \
  --command '["claude", "-p", "--permission-mode", "dontAsk", "--allowedTools", "Read,Edit,Write,Bash"]' \
  --network bridge --env ANTHROPIC_API_KEY \
  --output results/claude-coding.json --html results/claude-coding.html
```

Use a version whose CLI options and authentication you have verified. This is
a configuration example, not a tested live Claude result. For an existing Python
or Node agent, build your normal application image with the agent entry point
and grader dependencies available globally, then pass its argv. It must accept
stdin, work as UID 65534, and place its edits in `/workspace`.

For a provider-backed coding agent, build an image containing its CLI, then
explicitly enable agent networking and select required environment variables:

```sh
agent-eval coding --pack coding-starter@1.0.0 \
  --image my-coding-agent:tested \
  --command '["my-agent", "--stdin"]' \
  --network bridge --env MY_PROVIDER_API_KEY \
  --artifacts artifacts/provider --output results/provider.json
```

Only named variables are forwarded, using a temporary owner-readable env file;
values do not appear in Docker command arguments or result metadata. Temporary
env files are removed after execution. Multiline environment values are rejected.
Grading runs without those variables. Both containers inherit the image's own
configured environment; no unselected host environment variables are forwarded.
Credentials already baked into an image
are part of that image, so use an image without embedded credentials. Review
agent logs before publishing artifacts: an agent can print its own credentials
or private task data. No host directory, home directory, Docker socket, or host
credential store is mounted.

For Codex on GitHub Actions, official guidance recommends the
[Codex Action](https://learn.chatgpt.com/docs/github-action) with its API proxy
for credential isolation. Avoid placing API keys at job scope. The generic
coding-container example above is intended for trusted local or dedicated
automation environments; it does not implement that proxy protocol.

## What is measured

Each task's `tests` score is binary: grader command exit 0 passes, an ordinary
nonzero exit fails. A failure still counts in the denominator. Agent failures,
timeouts, unavailable executables, unsafe candidate archives, Docker errors,
and cleanup failures are evaluation errors and always fail. Docker exec error
statuses 125, 126, 127, and 137 are treated as errors rather than ordinary failed
tests. A malformed grader that exits 0 is therefore a bad benchmark: pack authors
must ensure their grader fails closed and checks the intended behavior.

`agent_latency_ms` measures execution plus Docker's exec transport; overall
`latency_ms` also includes container provisioning, transfers, grading, and
cleanup. Generic coding commands do not supply trustworthy token or cost
telemetry, so those values remain unknown. A requested cost gate must not infer
that missing cost means free. The image is resolved once to an immutable local
SHA-256 image ID before tasks run; that ID, platform, available registry digests,
pack fingerprint, task file hashes, grader command, and resource limits are
recorded for reproducibility. Neither image tags nor pack versions alone prove
identical contents. Arbitrary agent arguments can contain credentials, so result
metadata records the command's SHA-256 and argument count rather than its full
text. Keep a reviewed copy of your invocation/configuration alongside results
when sharing a reproducible run. The deterministic fixture benchmark includes
its known-safe command explicitly.

## Isolation, limits, and artifacts

Agent and grader have separate fresh containers and Docker-managed tmpfs volumes.
Both run as a non-root user with a read-only root filesystem, all Linux
capabilities dropped, and `no-new-privileges`. Agent networking defaults to
`none`; the explicit `bridge` option affects only the agent. The grader always
uses `none`. Each container has a 512 MiB memory limit, one CPU, 64 processes,
64 MiB workspace, 64 MiB temporary directory, and 64 MiB private home directory.
The home directory is non-root owned with mode 0700 and is never included in
candidate snapshots or copied to the host. Grader files have an additional
64 MiB tmpfs volume, are owned by root, and are not writable by candidate code.

After the agent returns, its entire container is paused before copying candidate
files, preventing descendants from editing the snapshot. The agent container
and its volumes are then removed. Agent timeouts kill the local Docker client
and remove the actual remote container and volumes; stopping only the client
would leave agent processes running. Cleanup failures fail the task and name
resources requiring attention. The harness never prunes unrelated containers,
volumes, or images. If the harness process is forcibly killed or Docker becomes
unavailable, an operator may need to remove resources with the recorded
`org.agent-eval-harness.run` label after confirming that run is no longer active.

Input and candidate workspaces accept regular files and directories only.
Symlinks, hard links, devices, sparse archive entries, escaping paths, duplicate
paths, and file/directory collisions are rejected. A workspace is limited to
4096 entries, 32 MiB total regular-file bytes, and 8 MiB per file; each log stream
is limited to 1 MiB. Use images with dependencies preinstalled instead of
creating a large virtual environment inside the task workspace. Executable bits
are normalized to 0755 or 0644; trusted grader files use 0555 or 0444. File
contents, relative names, and the executable flag are preserved.

Each invocation creates a unique artifact directory and never overwrites an
earlier run. Each task records bounded agent/grader stdout and stderr,
`candidate.tar`, file hashes, `patch.diff`, and `evidence.json`. The text diff is
an inspection aid: binary/large files are summarized and long diffs are marked
truncated. The candidate archive is the complete validated file snapshot within
the stated limits. The full result JSON is also saved inside the run directory.

This is isolation for evaluation, not a proof that Docker safely contains hostile
multi-tenant code. Trust your Docker daemon, image, and pack. Public grader files
are publicly inspectable and may already be known to a model; “withheld” means
absent from the agent's runtime workspace, not secret from the world. Candidate
code runs during grading and a poorly designed grader can be manipulated from
inside its process. The bundled pack probes candidates in child processes and
checks responses in the parent, but does not claim formal protection against
adversarial code. For stronger adversarial evaluation, use dedicated disposable
hosts or a stronger sandbox and independently audited graders.

## Verify without provider credentials

The repository includes a deterministic fixture agent/image for checking the
entire Docker workflow. It is deliberately capable of solving only the bundled
teaching tasks. Its success is not an AI model result. See the README's coding
workflow and benchmark instructions for the fixture build and commands.

The Docker integration tests are opt-in because ordinary unit tests must work
without a daemon:

```sh
docker pull python:3.12-slim
AGENT_EVAL_DOCKER_TESTS=1 python -m pytest tests/test_coding.py
```
