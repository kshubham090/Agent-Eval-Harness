#!/usr/bin/env python3
"""Measure the real Docker coding workflow using known deterministic fixtures.

This is an integration/correctness benchmark, not a model or product ranking.
Build examples/Dockerfile.fixture before running it; no provider keys are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.coding import run_coding_eval
from harness.packs import load_pack

PACK = "coding-starter@1.0.0"
FIXTURE_COMMAND = ("python", "-I", "-B", "/opt/fixture_agent.py")
WORKLOAD_VERSION = "docker-coding-fixtures-v1"


def _canonical_sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def source_provenance(root: Path = ROOT) -> dict:
    """Include engine, pack contents, fixture image recipe/agent, and this script."""
    paths = set((root / "harness").rglob("*.py"))
    pack_root = root / "harness" / "packs_data"
    paths.update(path for path in pack_root.rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    paths.update(root / path for path in ("examples/coding_fixture_agent.py", "examples/Dockerfile.fixture", "scripts/benchmark_coding.py"))
    files = {}
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        files[relative] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "executable": bool(path.stat().st_mode & 0o111)}
    return {"sha256": _canonical_sha(files), "files": files}


def git_provenance(root: Path = ROOT) -> dict:
    try:
        revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        status = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"], text=True, stderr=subprocess.DEVNULL).splitlines()
        return {"revision": revision, "dirty": bool(status), "status": status}
    except (OSError, subprocess.CalledProcessError):
        return {"revision": None, "dirty": None, "status": [], "note": "Git metadata unavailable"}


def docker_provenance() -> dict:
    try:
        raw = subprocess.check_output(["docker", "version", "--format", "{{json .Server}}"], timeout=15, stderr=subprocess.DEVNULL)
        info = json.loads(raw)
        return {key: info.get(key) for key in ("Version", "ApiVersion", "Os", "Arch", "GitCommit")}
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, TypeError):
        return {"error": "Docker server version unavailable"}


def sanitize_result(value: Any, *, artifacts: Path, root: Path = ROOT) -> Any:
    """Portable paths in published JSON; local detailed artifacts stay local.

    This fixture has fixed non-secret prompts and output. This function is not
    a general credential scrubber for arbitrary agent traces.
    """
    substitutions = [(str(artifacts.resolve()), "artifacts"), (str(root.resolve()), "$CHECKOUT"),
                     (str(Path.home()), "$HOME")]
    substitutions.sort(key=lambda pair: len(pair[0]), reverse=True)

    def convert(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: convert(child) for key, child in item.items()}
        if isinstance(item, list):
            return [convert(child) for child in item]
        if isinstance(item, tuple):
            return [convert(child) for child in item]
        if isinstance(item, str):
            for prefix, replacement in substitutions:
                item = item.replace(prefix, replacement)
            return item
        return item
    return convert(value)


def distribution(values: list[float]) -> dict:
    if not values or any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("distributions require nonempty finite nonnegative samples")
    ordered = sorted(values)
    return {"sample_count": len(values), "median": statistics.median(values),
            "p95": ordered[math.ceil(0.95 * len(values)) - 1], "min": min(values), "max": max(values),
            "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
            "percentile_method": "nearest_rank; p95 is maximum for fewer than 20 samples"}


def _trial_valid(result: dict, *, expected_pass_rate: float, case_count: int) -> bool:
    cases = result.get("cases", [])
    return (
        result.get("pass_rate") == expected_pass_rate
        and result.get("error_count") == 0
        and len(cases) == case_count
        and all(case.get("error") is None and case.get("scores", {}).get("tests") == expected_pass_rate for case in cases)
        and all(case.get("metadata", {}).get("grader_exit_code") == (0 if expected_pass_rate == 1 else 1) for case in cases)
    )


def run_benchmark(*, image: str, repetitions: int = 3, concurrency: int = 1,
                  timeout: float = 60, artifacts: Path = ROOT / "results" / "coding-benchmark") -> dict:
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")
    provenance_before = source_provenance()
    git_before = git_provenance()
    pack = load_pack(PACK)
    report = {
        "schema_version": 1, "workload_version": WORKLOAD_VERSION,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark_type": "deterministic real-container workflow; not an AI model benchmark",
        "config": {"pack": PACK, "pack_fingerprint": pack.fingerprint, "case_count": len(pack.tasks),
                   "image_reference": image, "repetitions": repetitions, "concurrency": concurrency,
                   "agent_timeout_seconds": timeout, "network": "none", "warmups": 0,
                   "trial_order": "interleaved correct/noop, reversed on odd trials",
                   "commands": {"correct": list(FIXTURE_COMMAND), "noop": [*FIXTURE_COMMAND, "--noop"]}},
        "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                        "platform": platform.platform(), "machine": platform.machine(), "logical_cpu_count": os.cpu_count(),
                        "docker": docker_provenance()},
        "provenance": {"git": git_before, "source": provenance_before},
        "limitations": [
            "Known reference patches are deliberately supplied by a deterministic fixture; these are not model-generated solutions.",
            "Three small teaching tasks check integration, not representative coding competence or a framework ranking.",
            "No provider inference is made. Generic command token use and cost are unreported, not measured as zero.",
            "Batch elapsed time includes image resolution, startup, transfer, agent execution, grading, and cleanup; no warmup is excluded.",
            "Agent/grader times include Docker exec transport. Timing differences describe this host and image only.",
            "With the default three trials, empirical nearest-rank p95 is the maximum; timing is not a confidence interval.",
            "Grader files are withheld from the agent runtime workspace. Public pack tests are not secret from humans or model training.",
            "Image tags may change. Actual immutable image IDs and source hashes are saved with every result.",
        ],
        "trials": [], "summary": {}, "checks": [],
    }
    for repetition in range(repetitions):
        order = ("correct", "noop") if repetition % 2 == 0 else ("noop", "correct")
        for variant in order:
            command = FIXTURE_COMMAND if variant == "correct" else (*FIXTURE_COMMAND, "--noop")
            start = time.perf_counter()
            trial = {"repetition": repetition + 1, "variant": variant, "sequence": len(report["trials"]) + 1}
            try:
                result = run_coding_eval(pack, command, image=image, timeout=timeout, concurrency=concurrency,
                                         artifacts_dir=artifacts, network="none").to_dict()
                elapsed_ms = (time.perf_counter() - start) * 1000
                trial.update(elapsed_ms=elapsed_ms,
                             agent_execution_ms=[case["agent_latency_ms"] for case in result["cases"]],
                             grader_execution_ms=[case["metadata"].get("grader_latency_ms") for case in result["cases"]],
                             result=sanitize_result(result, artifacts=artifacts),
                             valid=_trial_valid(result, expected_pass_rate=1.0 if variant == "correct" else 0.0, case_count=len(pack.tasks)))
            except (OSError, RuntimeError, ValueError) as exc:
                trial.update(elapsed_ms=(time.perf_counter() - start) * 1000, valid=False,
                             error=sanitize_result(str(exc), artifacts=artifacts))
            report["trials"].append(trial)
            # The caller sees enough progress to identify a stuck Docker run.
            print(f"trial {repetition + 1}/{repetitions}, {variant}: {'passed' if trial['valid'] else 'FAILED'} ({trial['elapsed_ms'] / 1000:.2f}s)", file=sys.stderr, flush=True)

    for variant in ("correct", "noop"):
        trials = [trial for trial in report["trials"] if trial["variant"] == variant]
        results = [trial["result"] for trial in trials if "result" in trial]
        agent = [duration for trial in trials for duration in trial.get("agent_execution_ms", []) if duration is not None]
        grader = [duration for trial in trials for duration in trial.get("grader_execution_ms", []) if duration is not None]
        report["summary"][variant] = {
            "trial_count": len(trials), "valid_trial_count": sum(trial["valid"] for trial in trials),
            "case_attempts": sum(len(result["cases"]) for result in results),
            "passed_cases": sum(result["summary"]["passed_count"] for result in results),
            "error_count": sum(result["error_count"] for result in results),
            "batch_elapsed_ms": distribution([trial["elapsed_ms"] for trial in trials]),
            "agent_execution_ms": distribution(agent) if agent else None,
            "grader_execution_ms": distribution(grader) if grader else None,
            "missing_agent_timing_count": repetitions * len(pack.tasks) - len(agent),
            "missing_grader_timing_count": repetitions * len(pack.tasks) - len(grader),
        }
    source_after = source_provenance()
    image_ids = {trial["result"]["metadata"]["image"]["id"] for trial in report["trials"] if "result" in trial}
    report["checks"] = [
        {"name": "every reference patch passes every task", "passed": all(trial["valid"] for trial in report["trials"] if trial["variant"] == "correct")},
        {"name": "every unchanged starter fails its tests without execution errors", "passed": all(trial["valid"] for trial in report["trials"] if trial["variant"] == "noop")},
        {"name": "source files unchanged during measurement", "passed": source_after["sha256"] == provenance_before["sha256"]},
        {"name": "one immutable image used by every trial", "passed": len(image_ids) == 1 and all("result" in trial for trial in report["trials"])},
    ]
    report["passed"] = all(check["passed"] for check in report["checks"])
    report["image_ids"] = sorted(image_ids)
    report["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return sanitize_result(report, artifacts=artifacts)


def render_markdown(report: dict) -> str:
    lines = [
        "Real Docker coding workflow, using known reference patches and unchanged starter files.",
        "These deterministic fixtures are **not AI model benchmark results**.", "",
        "| Fixture | Passed task attempts | Execution errors | Median batch time | Batch p95 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for variant, label in (("correct", "Known reference patches"), ("noop", "Unchanged buggy starters")):
        result = report["summary"][variant]
        elapsed = result["batch_elapsed_ms"]
        lines.append(f"| {label} | {result['passed_cases']}/{result['case_attempts']} | {result['error_count']} | {elapsed['median'] / 1000:.2f} s | {elapsed['p95'] / 1000:.2f} s |")
    config = report["config"]
    lines.extend([
        "", f"{config['repetitions']} interleaved repetitions × {config['case_count']} tasks per fixture; concurrency {config['concurrency']}, no warmup, agent and grader network disabled.",
        "Batch timing includes fresh containers, file transfers, execution, grading, and cleanup. P95 uses empirical nearest rank (the maximum with three trials).",
        "Token use and cost are unreported. These teaching tasks measure the workflow's correctness; they do not establish model quality or a world ranking.",
        "", f"Measured {report['started_at']} on {report['environment']['platform']}, Python {report['environment']['python']}; Docker {report['environment']['docker'].get('Version', 'unavailable')}.",
        f"Source `{report['provenance']['git']['revision'] or 'unavailable'}` ({'dirty' if report['provenance']['git']['dirty'] else 'clean'}); pack `{config['pack']}`.",
        f"Source SHA-256: `{report['provenance']['source']['sha256']}`.",
        f"Pack SHA-256: `{config['pack_fingerprint']}`.",
        f"Immutable image: `{', '.join(report['image_ids']) or 'unavailable'}`.",
        "", f"Workflow checks: **{'PASS' if report['passed'] else 'FAIL'}**. See [full raw trial results](coding-latest.json) and [methodology](../docs/coding.md).", "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="agent-eval-fixture:local")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "results" / "coding-benchmark")
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks" / "coding-latest.json")
    parser.add_argument("--markdown", type=Path, default=ROOT / "benchmarks" / "coding-latest.md")
    args = parser.parse_args(argv)
    try:
        report = run_benchmark(image=args.image, repetitions=args.repetitions, concurrency=args.concurrency,
                               timeout=args.timeout, artifacts=args.artifacts)
    except (OSError, RuntimeError, ValueError) as exc:
        # Preflight problems still leave a machine-readable diagnostic.
        report = {"schema_version": 1, "workload_version": WORKLOAD_VERSION, "passed": False,
                  "error": sanitize_result(str(exc), artifacts=args.artifacts)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(render_markdown(report) if "summary" in report else "Coding workflow benchmark could not start. Inspect the JSON diagnostic.\n", encoding="utf-8")
    print(f"Coding workflow benchmark {'passed' if report['passed'] else 'FAILED'}; wrote {args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
