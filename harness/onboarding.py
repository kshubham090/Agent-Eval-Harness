"""Credential-free setup, validation, diagnostics, and saved-result commands."""
from __future__ import annotations

import json
import platform
import shutil
import sys
from pathlib import Path

import click

_DEMO = '''"""Deterministic arithmetic fixture for learning the harness; not an AI agent."""
import re
from harness.runner import AgentOutput

class DemoAgent:
    def run(self, input):
        match = re.search(r"(-?\\d+) \\+ (-?\\d+)", input)
        if not match:
            raise ValueError("This demo only adds two integers")
        return AgentOutput(output=str(int(match[1]) + int(match[2])))

def get_agent():
    return DemoAgent()

if __name__ == "__main__":
    import sys
    print(get_agent().run(sys.stdin.read()).output)
'''


def register_commands(cli):
    @cli.command("init")
    @click.argument("directory", default=".", type=click.Path(file_okay=False))
    @click.option("--agent", type=click.Choice(["demo", "python", "codex", "claude-code", "command", "http"]), default="demo", show_default=True)
    def initialize(directory, agent):
        """Create an editable configuration and three teaching cases. Never overwrites files."""
        target = Path(directory)
        agent_section = {
            "demo": 'type = "python"\npath = "demo_agent.py"',
            "python": 'type = "python"\npath = "demo_agent.py" # Replace with your file.py::function or factory',
            "codex": 'type = "codex"\n# model = "your-model"\n# cwd = "/path/to/your/project"',
            "claude-code": 'type = "claude-code"\n# model = "your-model"\n# cwd = "/path/to/your/project"',
            "command": f'type = "command"\ncommand = {json.dumps([sys.executable, "demo_agent.py"])}\noutput_format = "text"',
            "http": 'type = "http"\nurl = "http://localhost:8000/agent"\noutput_key = "output"\n# token_env = "MY_AGENT_TOKEN"',
        }[agent]
        config = f'''# CLI flags override this file. Paths are relative to this file.
[agent]
{agent_section}
timeout = 120

[eval]
dataset = "cases.jsonl"
scorers = ["exact"]
concurrency = 1
runs = 1
pass_threshold = 1.0
min_pass_rate = 1.0

[output]
json = "results/run.json"
html = "results/report.html"
'''
        cases = [{"id": f"add-{i}", "input": f"Compute {a} + {b}. Reply with only the integer.",
                  "expected_output": str(a + b), "tags": ["arithmetic", "starter"]}
                 for i, (a, b) in enumerate([(7, 5), (18, -6), (21, 20)], 1)]
        files = {"agent-eval.toml": config, "cases.jsonl": "".join(json.dumps(c) + "\n" for c in cases)}
        if agent in {"demo", "python", "command"}:
            files["demo_agent.py"] = _DEMO
        conflicts = [name for name in files if (target / name).exists()]
        if conflicts:
            raise click.ClickException(f"refusing to overwrite {', '.join(conflicts)}; choose a new directory")
        try:
            target.mkdir(parents=True, exist_ok=True)
            for name, content in files.items():
                (target / name).write_text(content, encoding="utf-8")
        except OSError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Created {target / 'agent-eval.toml'} and {len(cases)} starter cases.")
        click.echo(f'Run: agent-eval eval --config "{target / "agent-eval.toml"}"')
        if agent in {"codex", "claude-code"}:
            click.echo("The installed agent CLI uses its existing login/configuration. Authenticate it before running.")

    @cli.command("doctor")
    @click.option("--agent", type=click.Choice(["codex", "claude-code"]), help="Require this CLI to be installed.")
    @click.option("--json", "as_json", is_flag=True)
    def doctor(agent, as_json):
        """Check the Python runtime and optional CLI executables; never calls a model."""
        checks = {"python": platform.python_version(), "executable": sys.executable,
                  "codex": shutil.which("codex"), "claude-code": shutil.which("claude"),
                  "authentication": "not checked; use codex login status or claude auth status"}
        if as_json:
            click.echo(json.dumps(checks, indent=2))
        else:
            for name, value in checks.items():
                click.echo(f"{name}: {value or 'not installed (optional)'}")
        if agent and not checks[agent]:
            raise click.exceptions.Exit(1)

    @cli.command("validate")
    @click.option("--dataset", required=True, type=click.Path(exists=True, dir_okay=False))
    def validate(dataset):
        """Validate a dataset and print its fingerprint without running an agent."""
        from harness.dataset import dataset_sha, load_dataset
        try:
            cases = load_dataset(dataset)
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Valid: {len(cases)} cases; sha256={dataset_sha(dataset)}")
        click.echo(f"Tags: {', '.join(sorted({t for c in cases for t in c.tags})) or '(none)'}")

    @cli.command("report")
    @click.option("--result", required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option("--html", required=True, type=click.Path())
    def report(result, html):
        """Render saved results without spending tokens on another run."""
        from harness.report import write_report
        from harness.results import flatten_metrics
        try:
            data = json.loads(Path(result).read_text(encoding="utf-8"))
            flatten_metrics(data)
            write_report(html, data)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise click.ClickException(f"cannot render result: {exc}") from exc
        click.echo(f"HTML: {html}")

    @cli.command("compare")
    @click.option("--current", required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option("--baseline", required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option("--threshold", default=0.05, type=click.FloatRange(0, 1), show_default=True)
    def compare(current, baseline, threshold):
        """Compare two saved JSON results; exit 1 on regression."""
        from harness.baseline import compare_to_baseline
        from harness.cli import _print_comparison
        try:
            comparison = compare_to_baseline(json.loads(Path(current).read_text(encoding="utf-8")),
                                             json.loads(Path(baseline).read_text(encoding="utf-8")), threshold)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise click.ClickException(str(exc)) from exc
        _print_comparison(comparison, baseline, threshold)
        click.echo("PASS: no regressions" if comparison.passed else "FAIL: regression detected")
        if not comparison.passed:
            raise click.exceptions.Exit(1)
