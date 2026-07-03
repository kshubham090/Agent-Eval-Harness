"""CLI: run evals, save baselines, gate on regressions.

    python -m harness eval --dataset ... --agent examples/stub_agent.py \
        --scorers exact,embedding --output results/run.json
    python -m harness baseline save --name v1.0 --result results/run.json
    python -m harness eval --dataset ... --agent ... --compare-baseline v1.0 --threshold 0.05

Agent files are plain Python modules exposing get_agent() -> AgentRunner.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from harness.baseline import (
    DEFAULT_BASELINES_DIR,
    compare_to_baseline,
    load_baseline,
    save_baseline,
)
from harness.dataset import load_dataset
from harness.eval_runner import run_eval
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


@click.group()
def cli() -> None:
    """Agent eval harness."""
    load_dotenv()  # pick up ANTHROPIC_API_KEY etc. from a local .env file


@cli.command("eval")
@click.option("--dataset", required=True, help="Path to a JSONL golden dataset.")
@click.option("--agent", "agent_path", required=True, help="Python file exposing get_agent().")
@click.option("--scorers", "scorer_names", default="exact", show_default=True,
              help=f"Comma-separated scorer names: {', '.join(SCORER_FACTORIES)}.")
@click.option("--output", type=click.Path(), help="Write the result JSON here.")
@click.option("--compare-baseline", "baseline_name", help="Gate against this saved baseline.")
@click.option("--threshold", default=0.05, show_default=True,
              help="Max allowed absolute drop in any metric vs the baseline.")
@click.option("--baselines-dir", default=DEFAULT_BASELINES_DIR, show_default=True)
def eval_command(dataset, agent_path, scorer_names, output, baseline_name, threshold, baselines_dir):
    """Run an eval: dataset x agent x scorers."""
    names = [n.strip() for n in scorer_names.split(",") if n.strip()]
    unknown = [n for n in names if n not in SCORER_FACTORIES]
    if unknown:
        raise click.ClickException(f"unknown scorers {unknown}; available: {list(SCORER_FACTORIES)}")

    cases = load_dataset(dataset)
    agent = load_agent(agent_path)
    result = run_eval(cases, agent, [SCORER_FACTORIES[n]() for n in names])

    click.echo(f"run_id: {result.run_id}  cases: {len(cases)}")
    for name, summary in result.scores.items():
        click.echo(f"  {name:<12} mean={summary.mean:.3f}")
    if result.trajectory_score is not None:
        click.echo(f"  {'trajectory':<12} mean={result.trajectory_score.mean:.3f}")
    click.echo(f"  {'pass_rate':<12} {result.pass_rate:.3f}")

    if output:
        result.save(output)
        click.echo(f"result written to {output}")

    if baseline_name:
        baseline = load_baseline(baseline_name, baselines_dir)
        comparison = compare_to_baseline(result.to_dict(), baseline, threshold)
        click.echo(f"\nvs baseline {baseline_name!r} (threshold {threshold}):")
        for d in comparison.deltas:
            marker = "REGRESSION" if d in comparison.regressions else "ok"
            click.echo(f"  {d.metric:<20} {d.baseline:.3f} -> {d.current:.3f}  ({d.delta:+.3f})  {marker}")
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


if __name__ == "__main__":
    cli()
