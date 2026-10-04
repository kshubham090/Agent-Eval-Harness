"""CLI workflows for portable packs, container tasks and saved evidence."""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

import click

from harness.gates import evaluate_gates
from harness.report import write_report


def validate_output_paths(*, inputs=(), outputs=(), protected_directories=()):
    """Prevent evidence outputs from aliasing each other or their inputs."""
    sources = [Path(p).resolve() for p in inputs if p]
    targets = [Path(p).resolve() for p in outputs if p]
    for i, target in enumerate(targets):
        for other in sources + targets[:i]:
            if target == other or (target.exists() and other.exists() and target.samefile(other)):
                raise ValueError("output paths must be distinct from source files and each other")
        if any(target.is_relative_to(Path(p).resolve()) for p in protected_directories if p):
            raise ValueError("outputs must be outside the benchmark pack")


def _save(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _limits(**limits):
    for key, value in limits.items():
        if value is not None and not math.isfinite(value):
            raise click.BadParameter(f"{key} must be finite")


def _gate(data, *, output=None, **limits):
    verdict = evaluate_gates(data, **limits)
    if output:
        _save(output, verdict.to_dict())
    for c in verdict.checks:
        value = "unknown" if c["value"] is None else f"{c['value']:.6g}"
        label = f" ({c['run_id']})" if c["run_id"] else " (all runs)"
        click.echo(f"{'PASS' if c['passed'] else 'FAIL'}: {c['metric']}{label}: "
                   f"{value} {c['operator']} {c['limit']:.6g}; {c['reason']}")
    if not verdict.passed:
        raise click.exceptions.Exit(1)


def register_workflows(cli):
    @cli.group("pack")
    def packs():
        """Discover, inspect or author versioned benchmark packs."""

    @packs.command("list")
    def pack_list():
        from harness.packs import list_packs
        for pack in list_packs():
            click.echo(f"{pack.id}@{pack.version}  {pack.kind}  {pack.name}")

    @packs.command("inspect")
    @click.argument("source")
    def pack_inspect(source):
        from harness.packs import load_pack
        try:
            pack = load_pack(source)
            data = {"id": pack.id, "version": pack.version, "kind": pack.kind,
                    "name": pack.name, "license": pack.license,
                    "fingerprint": pack.fingerprint, "image": pack.image,
                    "scorers": list(pack.scorers), "tasks": [t.id for t in pack.tasks]}
            if pack.dataset:
                from harness.dataset import load_dataset
                data["tasks"] = [c.id for c in load_dataset(pack.dataset)]
            click.echo(json.dumps(data, indent=2))
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc

    @packs.command("init")
    @click.argument("destination", type=click.Path())
    @click.option("--kind", type=click.Choice(["coding", "output"]), default="coding")
    def pack_init(destination, kind):
        from harness.packs import init_pack
        try:
            path = init_pack(destination, kind=kind)
            click.echo(f"Created pack: {path}")
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc

    @cli.command("replay")
    @click.option("--result", "result_path", required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option("--html", "html_path", required=True, type=click.Path())
    def replay(result_path, html_path):
        """Validate and render recorded evidence without invoking an agent or grader."""
        from harness.replay import load_recording
        try:
            validate_output_paths(inputs=[result_path], outputs=[html_path])
            data = load_recording(result_path)
            write_report(html_path, data, title="Recorded evaluation")
            click.echo(f"Replay: {html_path}")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise click.ClickException(str(exc)) from exc

    @cli.command("rescore")
    @click.option("--result", "result_path", required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option("--scorers", required=True, help="Comma-separated scorers to apply to saved output.")
    @click.option("--pass-threshold", type=click.FloatRange(0, 1))
    @click.option("--concurrency", type=click.IntRange(min=1), default=1)
    @click.option("--output", required=True, type=click.Path())
    @click.option("--html", "html_path", type=click.Path())
    @click.option("--min-pass-rate", type=click.FloatRange(0, 1))
    def rescore(result_path, scorers, pass_threshold, concurrency, output, html_path, min_pass_rate):
        """Apply new output graders to saved attempts; no new agent inference."""
        from harness.cli import SCORER_FACTORIES
        from harness.replay import load_recording, rescore_result
        try:
            _limits(pass_threshold=pass_threshold, min_pass_rate=min_pass_rate)
            names = [s.strip() for s in scorers.split(",") if s.strip()]
            if not names or len(names) != len(set(names)) or set(names) - set(SCORER_FACTORIES):
                raise ValueError(f"choose distinct scorers from {', '.join(SCORER_FACTORIES)}")
            validate_output_paths(inputs=[result_path], outputs=[output, html_path])
            data = rescore_result(load_recording(result_path), [SCORER_FACTORIES[n]() for n in names],
                                  pass_threshold=pass_threshold, concurrency=concurrency)
            _save(output, data)
            if html_path:
                write_report(html_path, data, title="Rescored evaluation — original agent evidence")
            click.echo(f"Rescored result: {output}")
            _gate(data, min_pass_rate=min_pass_rate)
        except (OSError, ValueError, TypeError, KeyError, ImportError) as exc:
            raise click.ClickException(str(exc)) from exc

    @cli.command("gate")
    @click.option("--result", "result_path", required=True, type=click.Path(exists=True, dir_okay=False))
    @click.option("--min-pass-rate", type=click.FloatRange(0, 1))
    @click.option("--max-error-rate", type=click.FloatRange(0, 1), default=0.0, show_default=True)
    @click.option("--max-cost-usd", type=click.FloatRange(min=0))
    @click.option("--max-p95-latency-ms", type=click.FloatRange(min=0))
    @click.option("--output", type=click.Path(), help="Write machine-readable gate evidence.")
    def gate(result_path, output, **limits):
        """Check every run's quality/latency and the total agent cost of all runs."""
        from harness.replay import load_recording
        try:
            _limits(**limits)
            validate_output_paths(inputs=[result_path], outputs=[output])
            _gate(load_recording(result_path), output=output, **limits)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise click.ClickException(str(exc)) from exc

    @cli.command("coding")
    @click.option("--pack", "pack_source", required=True, help="Coding pack directory or built-in name@version.")
    @click.option("--command", required=True, help="JSON argv executed inside the image; prompt arrives on stdin.")
    @click.option("--image", help="Agent image with installed tools; defaults to the pack image.")
    @click.option("--timeout", type=click.FloatRange(min=0, min_open=True), default=300.0, show_default=True)
    @click.option("--network", type=click.Choice(["none", "bridge"]), default="none", show_default=True)
    @click.option("--env", "env_names", multiple=True, help="Explicit host environment variable name to pass to agent only.")
    @click.option("--concurrency", type=click.IntRange(min=1), default=1)
    @click.option("--runs", type=click.IntRange(min=1), default=1)
    @click.option("--artifacts", "artifacts_dir", type=click.Path(), default="results/coding-artifacts")
    @click.option("--output", type=click.Path(), default="results/coding.json")
    @click.option("--html", "html_path", type=click.Path(), default="results/coding.html")
    @click.option("--min-pass-rate", type=click.FloatRange(0, 1))
    @click.option("--max-p95-latency-ms", type=click.FloatRange(min=0))
    @click.option("--max-error-rate", type=click.FloatRange(0, 1), default=0.0)
    def coding(pack_source, command, image, timeout, network, env_names, concurrency, runs,
               artifacts_dir, output, html_path, min_pass_rate, max_p95_latency_ms, max_error_rate):
        """Evaluate repository edits in fresh Docker containers with independent tests."""
        from harness.coding import run_coding_eval
        from harness.packs import load_pack
        from harness.results import summarize_runs
        try:
            _limits(timeout=timeout, min_pass_rate=min_pass_rate, max_p95_latency_ms=max_p95_latency_ms,
                    max_error_rate=max_error_rate)
            argv = json.loads(command)
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                raise ValueError("--command must be a nonempty JSON argv array")
            env = {}
            for name in env_names:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    raise ValueError("--env takes environment variable names, not assignments")
                if name not in os.environ:
                    raise ValueError(f"requested environment variable {name!r} is missing")
                env[name] = os.environ[name]
            pack = load_pack(pack_source)
            if pack.kind != "coding":
                raise ValueError("coding requires a coding pack; use eval --pack for output packs")
            validate_output_paths(outputs=[artifacts_dir], protected_directories=[pack.root])
            validate_output_paths(outputs=[output, html_path], protected_directories=[pack.root, artifacts_dir])
            results = []
            for i in range(runs):
                artifact_path = Path(artifacts_dir) / f"run-{i+1}" if runs > 1 else Path(artifacts_dir)
                result = run_coding_eval(pack, argv, image=image, timeout=timeout, concurrency=concurrency,
                                         network=network, env=env, artifacts_dir=artifact_path)
                results.append(result.to_dict())
                click.echo(f"run {i+1}/{runs}: {result.pass_rate:.1%} passed, {result.error_count} errors")
            data = results[0] if len(results) == 1 else summarize_runs(results)
            _save(output, data)
            write_report(html_path, data, title=f"Coding evaluation — {pack.id}@{pack.version}")
            click.echo(f"Result: {output}\nHTML: {html_path}\nArtifacts: {artifacts_dir}")
            _gate(data, min_pass_rate=min_pass_rate, max_p95_latency_ms=max_p95_latency_ms, max_error_rate=max_error_rate)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            raise click.ClickException(str(exc)) from exc
