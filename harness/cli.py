"""CLI: run evals, save baselines, gate on regressions.

    agent-eval eval --dataset ... --agent examples/stub_agent.py \
        --scorers exact,embedding --concurrency 8 --runs 3 \
        --output results/run.json --html results/report.html
    agent-eval baseline save --name v1.0 --result results/run.json
    agent-eval eval --dataset ... --agent ... --compare-baseline v1.0 --threshold 0.05

(Also works as `python -m harness ...`.) Agent files are plain Python modules
exposing get_agent() -> AgentRunner.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from harness.baseline import (
    DEFAULT_BASELINES_DIR,
    BaselineComparison,
    compare_to_baseline,
    load_baseline,
    save_baseline,
)
from harness.dataset import dataset_sha, filter_by_tags, load_dataset
from harness.eval_runner import run_eval
from harness.report import write_report
from harness.results import ERROR_CASE_POLICY, PASS_RULE, flatten_metrics, summarize_runs
from harness.runner import AgentRunner
from harness.scorers import (EmbeddingScorer, ExactMatchScorer, LLMJudgeScorer, RegexScorer,
                             ContainsScorer, JSONMatchScorer)

SCORER_FACTORIES = {
    "exact": ExactMatchScorer,
    "regex": RegexScorer,
    "contains": ContainsScorer,
    "json": JSONMatchScorer,
    "embedding": EmbeddingScorer,
    "llm_judge": LLMJudgeScorer,
}


def load_agent(path: str) -> AgentRunner:
    """Load a factory or an existing sync/async function without framework dependencies."""
    import hashlib
    from harness.adapters import FunctionRunner

    filename, separator, symbol = path.partition("::")
    agent_path = Path(filename).resolve()
    module_name = "_agent_eval_" + hashlib.sha256(str(agent_path).encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(module_name, agent_path)
    if spec is None or spec.loader is None:
        raise click.ClickException(f"cannot import agent module {filename!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    # Like executing a script, allow its top-level imports to find siblings.
    # Restore the process import path before evaluating concurrent cases.
    previous_path = sys.path[:]
    try:
        sys.path.insert(0, str(agent_path.parent))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous_path
    if separator:
        function = getattr(module, symbol, None)
        if not callable(function):
            raise click.ClickException(f"{filename!r} has no callable {symbol!r}")
        return FunctionRunner(function)
    if not callable(getattr(module, "get_agent", None)):
        raise click.ClickException(f"{filename!r} must define get_agent(), or use file.py::function")
    agent = module.get_agent()
    if not callable(getattr(agent, "run", None)):
        raise click.ClickException("get_agent() must return an object with run(input)")
    return agent


def _print_comparison(comparison: BaselineComparison, baseline_name: str, threshold: float) -> None:
    click.echo(f"\nvs baseline {baseline_name!r} (threshold {threshold}):")
    for d in comparison.deltas:
        marker = "REGRESSION" if d in comparison.regressions else "ok"
        click.echo(f"  {d.metric:<20} {d.baseline:.3f} -> {d.current:.3f}  ({d.delta:+.3f})  {marker}")


def _write_github_summary(comparison: BaselineComparison, baseline_name: str) -> None:
    """Post the comparison as a GitHub Actions job summary, when running in CI."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    verdict = "PASS" if comparison.passed else f"FAIL — {len(comparison.regressions)} regression(s)"
    lines = [
        f"## Eval gate vs `{baseline_name}`: {verdict}",
        "",
        "| Metric | Baseline | Current | Delta | |",
        "|---|---|---|---|---|",
    ]
    for d in comparison.deltas:
        flag = "🔴 REGRESSION" if d in comparison.regressions else "✅"
        lines.append(f"| {d.metric} | {d.baseline:.3f} | {d.current:.3f} | {d.delta:+.3f} | {flag} |")
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


@click.group()
@click.version_option(package_name="agent-eval-harness")
def cli() -> None:
    """Agent eval harness."""
    load_dotenv()  # pick up ANTHROPIC_API_KEY etc. from a local .env file


@cli.command("eval")
@click.option("--config", type=click.Path(exists=True, dir_okay=False), help="TOML configuration. CLI flags override it.")
@click.option("--dataset", type=click.Path(exists=True, dir_okay=False), help="JSONL golden dataset.")
@click.option("--pack", help="Versioned output pack, e.g. json-contracts@1.0.0, or local pack directory.")
@click.option("--agent", "agent_path", help="codex, claude-code, or a Python file[::callable].")
@click.option("--command", help='Command as a JSON argv array, e.g. ["python", "agent.py"].')
@click.option("--url", help="HTTP endpoint accepting POST {input: ...}.")
@click.option("--model", help="Model passed to Codex or Claude Code; otherwise their configured default.")
@click.option("--cwd", type=click.Path(exists=True, file_okay=False),
              help="Command/CLI subprocess working directory. Python functions use the current process directory.")
@click.option("--timeout", default=120.0, type=click.FloatRange(min=0, min_open=True), show_default=True,
              help="Per-request timeout in seconds for command/CLI/HTTP adapters.")
@click.option("--agent-arg", "agent_args", multiple=True, help="Additional Codex/Claude CLI argument; repeat as needed.")
@click.option("--output-format", type=click.Choice(["text", "json"]), default="text", help="Generic command response format.")
@click.option("--output-key", default="output", help="JSON response field containing the answer.")
@click.option("--token-env", help="Name of environment variable containing the HTTP bearer token.")
@click.option("--scorers", "scorer_names", default="exact", show_default=True,
              help=f"Comma-separated scorers: {', '.join(SCORER_FACTORIES)}.")
@click.option("--concurrency", default=1, type=click.IntRange(min=1), show_default=True,
              help="Parallel cases. Agent implementations must support concurrent calls.")
@click.option("--runs", default=1, type=click.IntRange(min=1), show_default=True)
@click.option("--filter-tags", help="Comma-separated tags; match any.")
@click.option("--pass-threshold", default=0.5, type=click.FloatRange(0, 1), show_default=True,
              help="Minimum mean score for a case to pass.")
@click.option("--min-pass-rate", type=click.FloatRange(0, 1), help="Fail if the measured pass rate is below this floor.")
@click.option("--max-cost-usd", type=click.FloatRange(min=0), help="Post-run total agent cost limit; requires complete cost reporting.")
@click.option("--max-p95-latency-ms", type=click.FloatRange(min=0), help="Post-run agent p95 latency limit for every run.")
@click.option("--allow-errors/--no-allow-errors", default=False, help="Do not fail solely because cases raised errors.")
@click.option("--output", type=click.Path(), help="Write result JSON.")
@click.option("--html", "html_path", type=click.Path(), help="Write self-contained HTML.")
@click.option("--compare-baseline", "baseline_name", help="Gate against a named baseline.")
@click.option("--threshold", default=0.05, type=click.FloatRange(0, 1), show_default=True,
              help="Maximum allowed absolute metric drop.")
@click.option("--allow-dataset-change/--no-allow-dataset-change", default=False)
@click.option("--baselines-dir", default=DEFAULT_BASELINES_DIR, show_default=True)
@click.pass_context
def eval_command(ctx, **options):
    """Evaluate your existing agent and optionally enforce a quality gate."""
    from click.core import ParameterSource
    from harness.config import load_config

    configured = {}
    if options["config"]:
        try:
            configured = load_config(options["config"])
            selected_sources = [key for key in ("dataset", "pack")
                                if ctx.get_parameter_source(key) == ParameterSource.COMMANDLINE]
            discarded_source = None
            if len(selected_sources) == 1:
                discarded_source = "pack" if selected_sources[0] == "dataset" else "dataset"
                options[discarded_source] = None
            params = {p.name: p for p in ctx.command.params}
            for key, value in configured.items():
                if key != discarded_source and ctx.get_parameter_source(key) != ParameterSource.COMMANDLINE:
                    options[key] = params[key].process_value(ctx, value)
            # Selecting a different adapter on the command line overrides the configured target.
            selected = [key for key in ("agent_path", "command", "url")
                        if ctx.get_parameter_source(key) == ParameterSource.COMMANDLINE]
            if selected:
                changed_target = any(options[key] != configured.get(key) for key in selected)
                for key in {"agent_path", "command", "url"} - set(selected):
                    options[key] = None
                if changed_target:
                    for key, default in (("model", None), ("agent_args", ())):
                        if ctx.get_parameter_source(key) != ParameterSource.COMMANDLINE:
                            options[key] = default
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
    options["_scorers_explicit"] = ("scorer_names" in configured or
        ctx.get_parameter_source("scorer_names") == ParameterSource.COMMANDLINE)
    try:
        _evaluate(options)
    except (OSError, ValueError, ImportError) as exc:
        raise click.ClickException(str(exc)) from exc


def _make_runner(o):
    from harness.adapters import ClaudeCodeRunner, CodexRunner, CommandRunner, HTTPRunner
    targets = [bool(o[k]) for k in ("agent_path", "command", "url")]
    if sum(targets) != 1:
        raise click.UsageError("choose exactly one of --agent, --command, or --url (or configure [agent])")
    if o["agent_path"] in {"codex", "claude-code"}:
        factory = CodexRunner if o["agent_path"] == "codex" else ClaudeCodeRunner
        return factory(cwd=o["cwd"], timeout=o["timeout"], model=o["model"], extra_args=o["agent_args"])
    if o["model"] or o["agent_args"]:
        raise click.UsageError("--model and --agent-arg require --agent codex or claude-code")
    if o["agent_path"]:
        return load_agent(o["agent_path"])
    if o["command"]:
        command = json.loads(o["command"])
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise ValueError("--command must be a nonempty JSON array of strings")
        return CommandRunner(command, cwd=o["cwd"], timeout=o["timeout"],
                             output_format=o["output_format"], output_key=o["output_key"])
    return HTTPRunner(o["url"], timeout=o["timeout"], output_key=o["output_key"], token_env=o["token_env"])


def _evaluate(o):
    import math
    import platform
    from importlib.metadata import version

    pack = None
    if o.get("pack"):
        from harness.packs import load_pack
        if o["dataset"]:
            raise click.UsageError("choose --dataset or --pack, not both")
        pack = load_pack(o["pack"])
        if pack.kind != "output":
            raise click.UsageError("use agent-eval coding --pack for coding packs")
        o["dataset"] = str(pack.dataset)
        if not o.get("_scorers_explicit"):
            o["scorer_names"] = ",".join(pack.scorers)
    if not o["dataset"]:
        raise click.UsageError("provide --dataset, --pack or eval.dataset in --config")
    from harness.workflows import validate_output_paths
    validate_output_paths(inputs=[o["dataset"], o.get("config")], outputs=[o["output"], o["html_path"]],
                          protected_directories=[pack.root] if pack else [])
    for name in ("timeout", "pass_threshold", "min_pass_rate", "threshold", "max_cost_usd", "max_p95_latency_ms"):
        if o[name] is not None and not math.isfinite(o[name]):
            raise ValueError(f"{name} must be finite")
    names = [n.strip() for n in o["scorer_names"].split(",") if n.strip()]
    if not names or len(names) != len(set(names)):
        raise ValueError("choose at least one scorer without duplicates")
    unknown = set(names) - set(SCORER_FACTORIES)
    if unknown:
        raise ValueError(f"unknown scorers {sorted(unknown)}; available: {list(SCORER_FACTORIES)}")
    cases = load_dataset(o["dataset"])
    sha = pack.fingerprint if pack else dataset_sha(o["dataset"])
    if o["filter_tags"]:
        tags = sorted(set(t.strip() for t in o["filter_tags"].split(",") if t.strip()))
        cases = filter_by_tags(cases, tags)
        if not cases:
            raise ValueError(f"no cases match tags {tags}")
        sha = f"{sha}+tags:{','.join(tags)}"
    # Validate known scorer prerequisites before any potentially paid agent call.
    if "json" in names:
        from harness.scorers.structured import strict_json
        for case in cases:
            try:
                strict_json(case.expected_output)
            except (ValueError, RecursionError) as exc:
                raise ValueError(f"case {case.id!r}: expected_output is not valid JSON") from exc
    if "regex" in names:
        import re
        for case in cases:
            try:
                re.compile(case.expected_output)
            except re.error as exc:
                raise ValueError(f"case {case.id!r}: invalid regex reference: {exc}") from exc
    for scorer, module, extra in (("embedding", "sentence_transformers", "embedding"),
                                  ("llm_judge", "anthropic", "judge")):
        if scorer in names and importlib.util.find_spec(module) is None:
            raise ValueError(f"{scorer} requires optional dependencies; run pip install '.[{extra}]' in the checkout")
    saved_baseline = None
    scorers = [SCORER_FACTORIES[n]() for n in names]
    if o["baseline_name"]:
        # Reject unusable gates before starting agent calls or loading model scorers.
        saved_baseline = load_baseline(o["baseline_name"], o["baselines_dir"])
        from harness.eval_runner import describe_scorers
        probe = {
            "dataset_sha": sha,
            "scores": {SCORER_FACTORIES[n].name: {"mean": 0.0} for n in names},
            "pass_rate": 0.0,
            "metadata": {"pass_threshold": o["pass_threshold"], "pass_rule": PASS_RULE,
                         "error_case_policy": ERROR_CASE_POLICY, "grader_config": describe_scorers(scorers)},
        }
        if any(c.expected_trajectory is not None for c in cases):
            probe["trajectory_score"] = {"mean": 0.0}
        compare_to_baseline(probe, saved_baseline, o["threshold"],
                            ignore_dataset_mismatch=o["allow_dataset_change"])
    agent = _make_runner(o)
    # Keep prompts, command arguments, auth headers, and URL query strings out of metadata.
    label = o["agent_path"] or ("command" if o["command"] else "http")
    metadata = {"dataset": str(o["dataset"]), "agent": label, "scorers": ",".join(names),
                "concurrency": o["concurrency"], "harness_version": version("agent-eval-harness"),
                "python": platform.python_version(), "model": o["model"] or "agent-configured",
                "timeout_seconds": o["timeout"] if not o["agent_path"] or o["agent_path"] in {"codex", "claude-code"} else None}
    if pack:
        metadata["pack"] = {"id": pack.id, "version": pack.version, "fingerprint": pack.fingerprint}
    results = []
    for i in range(o["runs"]):
        result = run_eval(cases, agent, scorers, concurrency=o["concurrency"],
                          pass_threshold=o["pass_threshold"], dataset_sha=sha, metadata=metadata)
        results.append(result)
        click.echo(f"run {i + 1}/{o['runs']}  id: {result.run_id}  cases: {len(cases)}")
        for name, summary in result.scores.items():
            click.echo(f"  {name:<16} {summary.mean:.3f}")
        if result.trajectory_score is not None:
            click.echo(f"  {'trajectory':<16} {result.trajectory_score.mean:.3f}")
        click.echo(f"  {'pass_rate':<16} {result.pass_rate:.3f}")
        if result.error_count:
            click.secho(f"  errors: {result.error_count} (details in JSON/HTML)", fg="red")
    result_dict = results[0].to_dict() if len(results) == 1 else summarize_runs([r.to_dict() for r in results])
    if o["runs"] > 1:
        click.echo("Across runs (mean +/- sample standard deviation):")
        for metric, mean in result_dict["mean"].items():
            click.echo(f"  {metric:<20} {mean:.3f} +/- {result_dict['std'][metric]:.3f}")
    if o["output"]:
        target = Path(o["output"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result_dict, indent=2, allow_nan=False), encoding="utf-8")
        click.echo(f"Result: {target}")
    comparison = None
    if o["baseline_name"]:
        comparison = compare_to_baseline(result_dict, saved_baseline,
                                         o["threshold"], ignore_dataset_mismatch=o["allow_dataset_change"])
        _print_comparison(comparison, o["baseline_name"], o["threshold"])
        _write_github_summary(comparison, o["baseline_name"])
    if o["html_path"]:
        write_report(o["html_path"], result_dict, comparison, title=f"Eval report — {Path(label).name}")
        click.echo(f"HTML: {o['html_path']}")
    failures = []
    if any(r.error_count for r in results) and not o["allow_errors"]:
        failures.append("agent/scorer errors occurred")
    rate = flatten_metrics(result_dict)["pass_rate"]
    floor = o["min_pass_rate"]
    if floor is not None and rate < floor and not math.isclose(
        rate, floor, rel_tol=0.0, abs_tol=4 * math.ulp(max(rate, floor)),
    ):
        failures.append(f"pass rate {rate:.3f} is below {o['min_pass_rate']:.3f}")
    if comparison is not None and not comparison.passed:
        failures.append(f"{len(comparison.regressions)} metric(s) regressed")
    if o["max_cost_usd"] is not None or o["max_p95_latency_ms"] is not None:
        from harness.gates import evaluate_gates
        budgets = evaluate_gates(result_dict, max_error_rate=None, max_cost_usd=o["max_cost_usd"],
                                 max_p95_latency_ms=o["max_p95_latency_ms"])
        failures.extend(f"{c['metric']}: {c['reason']}" for c in budgets.checks if not c["passed"])
    if failures:
        click.echo("FAIL: " + "; ".join(failures))
        raise click.exceptions.Exit(1)
    if comparison is not None or o["min_pass_rate"] is not None:
        click.echo("PASS: quality gates satisfied")


@cli.group()
def baseline() -> None:
    """Manage baseline snapshots."""


@baseline.command("save")
@click.option("--name", required=True, help="Baseline name, e.g. v1.0.")
@click.option("--result", "result_path", required=True, type=click.Path(exists=True),
              help="Result JSON produced by `eval --output`.")
@click.option("--baselines-dir", default=DEFAULT_BASELINES_DIR, show_default=True)
def baseline_save(name, result_path, baselines_dir):
    """Save an eval result as a named baseline."""
    try:
        result = json.loads(Path(result_path).read_text(encoding="utf-8"))
        path = save_baseline(name, result, baselines_dir)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"baseline {name!r} saved to {path}")


@baseline.command("list")
@click.option("--baselines-dir", default=DEFAULT_BASELINES_DIR, show_default=True)
def baseline_list(baselines_dir):
    """List saved baselines with their headline metrics."""
    directory = Path(baselines_dir)
    files = sorted(directory.glob("*.json")) if directory.exists() else []
    if not files:
        click.echo(f"no baselines in {baselines_dir}/")
        return
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        metrics = flatten_metrics(data)
        headline = "  ".join(f"{k}={v:.3f}" for k, v in sorted(metrics.items()))
        click.echo(f"{f.stem:<16} {headline}")


from harness.onboarding import register_commands

register_commands(cli)
from harness.workflows import register_workflows

register_workflows(cli)

if __name__ == "__main__":
    cli()
