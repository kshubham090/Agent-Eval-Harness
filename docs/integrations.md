# Connect an agent you already use

All integrations receive the dataset's `input` string, never its reference
answer. No framework migration or hosted evaluation account is required.
Install the harness into your agent's Python environment when importing it.
Python 3.12 or newer is required.

## Codex

Authenticate the installed CLI normally, then run:

```sh
agent-eval eval --agent codex --cwd /path/to/your/repository \
  --dataset benchmarks/agent_smoke.jsonl --scorers json \
  --concurrency 1 --runs 3 --timeout 120 \
  --output results/codex.json --html results/codex.html
```

The adapter invokes `codex exec --json --ephemeral --sandbox read-only -`.
The input arrives on stdin. It collects the final agent message, completion
usage and supported completed tool events; failed or incomplete turns count
as errors. Each case starts a fresh conversation. MCP calls retain their tool
name; shell, web and file events use `command_execution`, `web_search` and
`file_change`. These event names are not an exhaustive tool trace.

Set `--model YOUR_MODEL` to record and pass an explicit model. Without it,
Codex's own configuration selects the model; the harness labels that choice
`agent-configured`, not a resolved model version. `--agent-arg=VALUE` forwards
one additional CLI argument and can be repeated. Use `--cwd` pointing at a
Git repository; outside Git, explicitly add `--agent-arg=--skip-git-repo-check`
if that is appropriate for your workspace.

The preset starts read-only. To evaluate edits, use a disposable checkout and
explicitly configure permissions through your CLI. This harness does not
create per-case containers, reset repositories, or grade repository tests.
Instructions about invoking an agent are checked against the official
[Codex non-interactive documentation](https://developers.openai.com/codex/noninteractive/).

## Claude Code

```sh
agent-eval eval --agent claude-code --cwd /path/to/your/project \
  --dataset benchmarks/agent_smoke.jsonl --scorers json \
  --concurrency 1 --runs 3 --timeout 120 \
  --output results/claude-code.json --html results/claude-code.html
```

The installed, authenticated CLI runs as
`claude -p --output-format json --permission-mode dontAsk` with input on stdin.
The adapter reads its successful `result`, reported token usage and USD cost.
Cached input is included once in total tokens. Raw provider usage remains in
case metadata. An unsuccessful result is an error even if the process exits
zero. This result format does not supply a tool trajectory.

`dontAsk` denies requests that would need approval; existing allowed tools and
project settings still apply. It is not a read-only sandbox. Choose an
appropriate working directory and permissions. See the official
[Claude Code programmatic guide](https://code.claude.com/docs/en/headless).

These two CLI protocols have offline fixture coverage. They have not been
live-tested against authenticated providers in this change; CLI versions can
change their flags or output format. `agent-eval doctor` checks executable
availability without invoking a model and prints authentication diagnostics
commands.

## Any command, any language

Your existing program reads the prompt from stdin and prints its final answer
to stdout. Put progress logs on stderr. Arguments are a JSON array, executed
directly without a shell:

```sh
agent-eval eval --dataset cases.jsonl \
  --command '["node", "my-agent.mjs"]' --timeout 60
```

For programs that accept a positional prompt, use the literal `{input}`
placeholder in one argument. Its content is substituted without shell
interpretation. In this mode, stdin is empty:

```toml
[agent]
type = "command"
command = ["my-agent", "--prompt", "{input}"]
timeout = 60

[eval]
dataset = "cases.jsonl"
scorers = ["exact"]
```

`command` paths and arguments are your executable's responsibility. With a
config file, its parent is the default agent working directory; override
`agent.cwd` as needed. TOML arrays avoid shell-specific quoting on Windows.
For structured responses, set `output_format = "json"` in `[agent]` or use
`--output-format json`:

```json
{
  "output": "Paris",
  "trajectory": ["search", "answer"],
  "usage": {"input_tokens": 100, "output_tokens": 8},
  "cost_usd": 0.0001,
  "metadata": {"model": "your-model-version"}
}
```

Only `output` is required. It must be a string; use `output_key = "answer"`
for a different top-level key. Usage values must be finite, nonnegative
numeric counters. Cost is optional: missing is unknown, not zero. Metadata
must be JSON serializable. Plain-text commands do not report usage or cost.

Each command/CLI invocation has a deadline and a 1 MiB limit per output
stream. On POSIX, process groups terminate remaining descendants at timeout
or completion. On Windows, only the direct child is terminated. Commands
inherit your environment and permissions. The harness is not a security
sandbox. There are no automatic retries: a retry could duplicate a side
effect or hide a real failure.

## Existing HTTP service

```sh
agent-eval eval --dataset cases.jsonl \
  --url http://localhost:8000/agent --output-key output \
  --token-env MY_AGENT_TOKEN --timeout 60
```

Requests are `POST` with `Content-Type: application/json` and a body such as
`{"input":"What is the capital of France?"}`. The response uses the same
JSON envelope above. Omit `--token-env` for endpoints that do not need auth.
It names an environment variable; it is not the token itself. Redirects are
rejected to avoid forwarding credentials. Provider error bodies and command
stderr are not copied into persisted error messages. Successful agent outputs
and metadata are stored as returned; review reports before publishing them.

HTTP timeout is a socket timeout, not a total wall-clock deadline against a
server that slowly trickles bytes. Response size is capped at 1 MiB. Endpoint
adapters with different request schemas can be written as a small Python
function. No specific OpenAI-compatible chat API schema is assumed.

## Existing Python function or framework

If your function accepts a string and returns a string, point straight to it:

```sh
agent-eval eval --dataset cases.jsonl --agent my_agent.py::answer
```

Sync and async functions are supported. Functions may also return
`AgentOutput` or the common JSON envelope. An async function runs in a fresh
event loop per call; loop-bound clients should be created for that loop or
exposed behind a synchronous wrapper/service. Direct Python calls do not have
an enforced timeout. Use a subprocess or HTTP boundary for timeout handling.
Python functions run in the harness process's working directory; `--cwd`
applies to command and CLI subprocesses. Resolve data paths relative to your
module, launch the harness from your project, or use the command adapter when
the agent needs a different working directory.

For LangChain, LangGraph, CrewAI, Pydantic AI or another framework, expose your
existing entry point rather than rebuilding the agent:

```python
# bridge.py — my_existing_agent is your own application module
from my_existing_agent import graph

def answer(prompt: str) -> str:
    state = graph.invoke({"messages": [("user", prompt)]})
    return state["messages"][-1].content
```

This is a generic callable bridge, not a framework-specific integration.
Adapt its result extraction to your application. Install your application as
a package so imports work from any directory. Top-level imports can find
sibling files while the agent module loads; that temporary search path is
restored before evaluation. Lazy imports inside agent calls should use an
installed package. The original factory contract continues to work:

```python
from harness.runner import AgentOutput

class MyAgent:
    def run(self, input: str) -> AgentOutput:
        answer, tools = my_pipeline(input)
        return AgentOutput(output=answer, trajectory=tools)

def get_agent():
    return MyAgent()
```

Run that file with `--agent bridge.py`. Programmatic users can also call
`run_eval(cases, FunctionRunner(answer), [ExactMatchScorer()])`.

## Parallelism and permissions

Concurrency defaults to 1. Increasing it shares the runner object between
threads; Python agents must support this. External cases launch separate
processes but may still share the working directory, CLI configuration, tools
and provider rate limits. Use read-only workloads or isolate mutable state
yourself. Fresh conversations alone do not isolate external side effects.
Do not infer tool safety or end-to-end coding quality from a prompt-only score.
