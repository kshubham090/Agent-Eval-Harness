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
from harness.results import flatten_metrics, summarize_runs
from harness.runner import AgentRunner
from harness.scorers import EmbeddingScorer, ExactMatchScorer, LLMJudgeScorer, RegexScorer

SCORER_FACTORIES = {
    "exact": ExactMatchScorer,
    "regex": RegexScorer,
    "embedding": EmbeddingScorer,
    "llm_judge": LLMJudgeScorer,
}


def load_agent(path: str) -> AgentRunner:
    agent_path = Path(path)
    spec = importlib.util.spec_from_file_location(agent_path.stem, agent_path)
    if spec is None or spec.loader is None:
        raise click.ClickException(f"cannot import agent module {path!r}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "get_agent"):
        raise click.ClickException(f"{path!r} must define get_agent() -> AgentRunner")
    return module.get_agent()


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
def cli() -> None:
    """Agent eval harness."""
    load_dotenv()  # pick up ANTHROPIC_API_KEY etc. from a local .env file


@cli.command("eval")
@click.option("--dataset", required=True, help="Path to a JSONL golden dataset.")
@click.option("--agent", "agent_path", required=True, help="Python file exposing get_agent().")
@click.option("--scorers", "scorer_names", default="exact", show_default=True,
              help=f"Comma-separated scorer names: {', '.join(SCORER_FACTORIES)}.")
@click.option("--concurrency", default=4, show_default=True,
              help="Cases run in parallel (agents/LLM scorers are I/O-bound).")
@click.option("--runs", default=1, show_default=True,
              help="Repeat the eval N times and report mean +/- std per metric.")
@click.option("--filter-tags", default=None,
              help="Comma-separated tags; only cases carrying at least one are run.")
@click.option("--output", type=click.Path(), help="Write the result JSON here.")
@click.option("--html", "html_path", type=click.Path(), help="Write a self-contained HTML report here.")
@click.option("--compare-baseline", "baseline_name", help="Gate against this saved baseline.")
@click.option("--threshold", default=0.05, show_default=True,
              help="Max allowed absolute drop in any metric vs the baseline.")
@click.option("--allow-dataset-change", is_flag=True,
              help="Compare against the baseline even if the dataset changed.")
@click.option("--baselines-dir", default=DEFAULT_BASELINES_DIR, show_default=True)
def eval_command(dataset, agent_path, scorer_names, concurrency, runs, filter_tags,
                 output, html_path, baseline_name, threshold, allow_dataset_change, baselines_dir):
    """Run an eval: dataset x agent x scorers."""
    names = [n.strip() for n in scorer_names.split(",") if n.strip()]
    unknown = [n for n in names if n not in SCORER_FACTORIES]
    if unknown:
        raise click.ClickException(f"unknown scorers {unknown}; available: {list(SCORER_FACTORIES)}")

    cases = load_dataset(dataset)
    sha = dataset_sha(dataset)
    if filter_tags:
        tags = sorted(t.strip() for t in filter_tags.split(",") if t.strip())
        cases = filter_by_tags(cases, tags)
        if not cases:
            raise click.ClickException(f"no cases match tags {tags}")
        # a filtered run is a different effective dataset -- fingerprint it as such
        sha = f"{sha}+tags:{','.join(tags)}"

    agent = load_agent(agent_path)
    scorers = [SCORER_FACTORIES[n]() for n in names]
    metadata = {"dataset": dataset, "agent": agent_path, "scorers": ",".join(names)}

    results = []
    for i in range(runs):
        result = run_eval(cases, agent, scorers, concurrency=concurrency,
                          dataset_sha=sha, metadata=metadata)
        results.append(result)
        prefix = f"run {i + 1}/{runs}  " if runs > 1 else ""
        click.echo(f"{prefix}run_id: {result.run_id}  cases: {len(cases)}")
        for name, summary in result.scores.items():
            click.echo(f"  {name:<12} mean={summary.mean:.3f}")
        if result.trajectory_score is not None:
            click.echo(f"  {'trajectory':<12} mean={result.trajectory_score.mean:.3f}")
        click.echo(f"  {'pass_rate':<12} {result.pass_rate:.3f}")
        if result.error_count:
            click.secho(f"  {'errors':<12} {result.error_count} case(s) raised -- see result JSON/report",
                        fg="red")

    if runs == 1:
        result_dict = results[0].to_dict()
    else:
        result_dict = summarize_runs([r.to_dict() for r in results])
        click.echo(f"\nacross {runs} runs (mean +/- std):")
        for metric, mean in result_dict["mean"].items():
            click.echo(f"  {metric:<20} {mean:.3f} +/- {result_dict['std'][metric]:.3f}")

    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(result_dict, indent=2), encoding="utf-8")
        click.echo(f"result written to {output}")

    comparison = None
    if baseline_name:
        baseline = load_baseline(baseline_name, baselines_dir)
        try:
            comparison = compare_to_baseline(result_dict, baseline, threshold,
                                             ignore_dataset_mismatch=allow_dataset_change)
        except ValueError as e:
            raise click.ClickException(str(e))
        _print_comparison(comparison, baseline_name, threshold)
        _write_github_summary(comparison, baseline_name)

    if html_path:
        # per-case detail lives on individual runs; report the last one
        write_report(html_path, results[-1].to_dict(), comparison,
                     title=f"Eval report — {Path(agent_path).stem}")
        click.echo(f"HTML report written to {html_path}")

    if comparison is not None:
        if not comparison.passed:
            click.echo(f"\nFAIL: {len(comparison.regressions)} metric(s) regressed more than {threshold}")
            sys.exit(1)
        click.echo("\nPASS: no regressions")


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
    result = json.loads(Path(result_path).read_text(encoding="utf-8"))
    path = save_baseline(name, result, baselines_dir)
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


if __name__ == "__main__":
    cli()
