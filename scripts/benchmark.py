#!/usr/bin/env python3
"""Reproducible offline measurements of the harness, not of model intelligence.

Run from a checkout with Python 3.12+; no optional packages or credentials needed.
All timing assertions are observational: correctness failures affect the exit
status, while machine-dependent speed does not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import operator
import os
import platform
import random
import re
import shlex
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

# Keep the documented command usable without installing the project first.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.baseline import compare_to_baseline
from harness.dataset import EvalCase
from harness.eval_runner import run_eval
from harness.runner import AgentOutput
from harness.scorers.exact import ExactMatchScorer


WORKLOAD_VERSION = "integer-arithmetic-v1"
OPERATIONS = ("+", "-", "*", "//")
PROMPT = (
    "Evaluate the integer expression {left} {operation} {right}. "
    "Use floor division for //. Reply with only the integer."
)
EXPRESSION = re.compile(
    r"Evaluate the integer expression (-?\d+) (\+|-|\*|//) (-?\d+)\. "
    r"Use floor division for //\. Reply with only the integer\."
)


@dataclass(frozen=True)
class BenchmarkConfig:
    cases: int = 128
    overhead_cases: int = 4096
    latency_ms: float = 5.0
    repetitions: int = 5
    warmups: int = 1
    seed: int = 20261004
    concurrency: tuple[int, ...] = (1, 2, 4, 8)

    def validate(self) -> None:
        for name in ("cases", "overhead_cases"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 4 or value % 4:
                raise ValueError(f"{name} must be a positive multiple of 4 (at least 4)")
        if not math.isfinite(self.latency_ms) or self.latency_ms < 0:
            raise ValueError("latency_ms must be finite and nonnegative")
        if self.repetitions < 1 or self.warmups < 0:
            raise ValueError("repetitions must be >= 1 and warmups must be >= 0")
        if not self.concurrency or self.concurrency[0] != 1:
            raise ValueError("concurrency must start with 1 for the serial reference")
        if any(c < 1 for c in self.concurrency) or len(set(self.concurrency)) != len(self.concurrency):
            raise ValueError("concurrency values must be positive and unique")


def generate_cases(count: int, seed: int) -> list[EvalCase]:
    """Generate balanced operations; reference answers are authored arithmetic.

    The runner receives only the prompt, never this reference or case object.
    This is a harness fixture, not a representative reasoning benchmark.
    """
    rng = random.Random(seed)
    cases = []
    for index in range(count):
        left = rng.randint(-999, 999)
        right = rng.randint(1, 99) * rng.choice((-1, 1))
        operation = OPERATIONS[index % len(OPERATIONS)]
        if operation == "+":
            expected = left + right
        elif operation == "-":
            expected = left - right
        elif operation == "*":
            expected = left * right
        else:
            expected = left // right
        cases.append(EvalCase(
            id=f"arithmetic-{index:05d}",
            input=PROMPT.format(left=left, operation=operation, right=right),
            expected_output=str(expected),
            tags=("synthetic", "integer-arithmetic", f"operation:{operation}"),
        ))
    return cases


class ArithmeticRunner:
    """A deterministic parser/calculator with optional controlled wait or fault."""

    def __init__(self, latency_ms: float = 0, *, degraded: bool = False, fail: bool = False):
        self.latency_seconds = latency_ms / 1000
        self.degraded = degraded
        self.fail = fail

    def run(self, input: str) -> AgentOutput:
        if self.latency_seconds:
            # This models waiting, not a network endpoint or real provider.
            time.sleep(self.latency_seconds)
        match = EXPRESSION.fullmatch(input)
        if match is None:
            raise ValueError("unsupported arithmetic fixture prompt")
        left, operation, right = match.groups()
        if self.fail and operation == "-":
            raise RuntimeError("deliberately injected subtraction failure")
        functions = {"+": operator.add, "-": operator.sub, "*": operator.mul, "//": operator.floordiv}
        value = functions[operation](int(left), int(right))
        if self.degraded and operation == "*":
            value += 1
        return AgentOutput(output=str(value))


def canonical_sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cases_sha(cases: list[EvalCase]) -> str:
    return canonical_sha([asdict(case) for case in cases])


def percentile_nearest_rank(values: list[float], percentile: float) -> float:
    """Empirical nearest-rank quantile: with five samples P95 is the maximum."""
    if not values:
        raise ValueError("cannot summarize empty samples")
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * percentile / 100) - 1]


def distribution(values: list[float]) -> dict:
    if not values:
        raise ValueError("cannot summarize empty samples")
    return {
        "sample_count": len(values),
        "median": statistics.median(values),
        "p95": percentile_nearest_rank(values, 95),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def git_provenance() -> dict:
    def read(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], text=True, capture_output=True, check=True,
        ).stdout.strip()
    try:
        status = read("status", "--porcelain", "--untracked-files=normal")
        return {"revision": read("rev-parse", "HEAD"), "dirty": bool(status), "status": status.splitlines()}
    except (OSError, subprocess.CalledProcessError):
        return {"revision": None, "dirty": None, "status": [], "note": "Git metadata unavailable"}


def source_provenance() -> dict:
    paths = sorted((ROOT / "harness").rglob("*.py")) + [Path(__file__).resolve()]
    files = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    return {"sha256": canonical_sha(files), "files": files}


def correct_and_ordered(result, cases: list[EvalCase]) -> bool:
    return (
        result.error_count == 0
        and result.pass_rate == 1.0
        and [case.case_id for case in result.case_results] == [case.id for case in cases]
        and all(
            actual.output == expected.expected_output and actual.scores == {"exact_match": 1.0}
            for actual, expected in zip(result.case_results, cases)
        )
    )


def timed_harness(cases: list[EvalCase], concurrency: int, latency_ms: float, fingerprint: str):
    runner = ArithmeticRunner(latency_ms)
    scorer = ExactMatchScorer()
    start = time.perf_counter_ns()
    result = run_eval(cases, runner, [scorer], concurrency=concurrency, dataset_sha=fingerprint)
    elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
    return result, elapsed_ms


def benchmark_overhead(cases: list[EvalCase], config: BenchmarkConfig) -> dict:
    """Compare total eval to a direct runner + scoring loop, with no wait.

    The incremental measurement includes result construction, validation,
    per-case timing and aggregation. It excludes dataset generation/imports.
    Direct and harness execution order alternates to reduce order effects.
    """
    fingerprint = cases_sha(cases)
    runner, scorer = ArithmeticRunner(), ExactMatchScorer()

    def direct():
        start = time.perf_counter_ns()
        scores = [scorer.score(case.expected_output, runner.run(case.input).output) for case in cases]
        elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
        return all(score == 1.0 for score in scores), elapsed_ms

    def harness():
        result, elapsed_ms = timed_harness(cases, 1, 0, fingerprint)
        return correct_and_ordered(result, cases), elapsed_ms

    samples, warmups = [], []
    for index in range(config.warmups + config.repetitions):
        pair = {}
        order = ("direct", "harness") if index % 2 == 0 else ("harness", "direct")
        for name in order:
            pair[name] = direct() if name == "direct" else harness()
        sample = {
            "execution_order": list(order),
            "direct_loop_ms": pair["direct"][1],
            "harness_ms": pair["harness"][1],
            "incremental_us_per_case": (pair["harness"][1] - pair["direct"][1]) * 1000 / len(cases),
            "verified": pair["direct"][0] and pair["harness"][0],
        }
        (warmups if index < config.warmups else samples).append(sample)
    return {
        "case_count": len(cases),
        "concurrency": 1,
        "latency_ms": 0,
        "description": "Incremental harness work above a direct runner + exact-scoring loop; no simulated wait.",
        "samples": samples,
        "warmup_samples_excluded": warmups,
        "direct_loop_ms": distribution([sample["direct_loop_ms"] for sample in samples]),
        "harness_ms": distribution([sample["harness_ms"] for sample in samples]),
        "incremental_us_per_case": distribution([sample["incremental_us_per_case"] for sample in samples]),
    }


def benchmark_concurrency(cases: list[EvalCase], config: BenchmarkConfig) -> list[dict]:
    fingerprint = cases_sha(cases)
    samples = {concurrency: [] for concurrency in config.concurrency}
    warmups = {concurrency: [] for concurrency in config.concurrency}
    # Interleave concurrency levels and reverse every second round, rather
    # than assigning an entire block of machine drift to one configuration.
    for index in range(config.warmups + config.repetitions):
        order = config.concurrency if index % 2 == 0 else tuple(reversed(config.concurrency))
        for concurrency in order:
            result, elapsed_ms = timed_harness(cases, concurrency, config.latency_ms, fingerprint)
            sample = {
                "round": index - config.warmups + 1,
                "round_execution_order": list(order),
                "wall_ms": elapsed_ms,
                "cases_per_second": len(cases) * 1000 / elapsed_ms,
                "case_latency_ms": [case.latency_ms for case in result.case_results],
                "pass_rate": result.pass_rate,
                "error_count": result.error_count,
                "verified": correct_and_ordered(result, cases),
            }
            destination = warmups if index < config.warmups else samples
            destination[concurrency].append(sample)
    serial_median = statistics.median(sample["wall_ms"] for sample in samples[1])
    return [{
        "concurrency": concurrency,
        "case_count": len(cases),
        "simulated_wait_ms_per_case": config.latency_ms,
        "samples": samples[concurrency],
        "warmup_samples_excluded": warmups[concurrency],
        "wall_ms": distribution([sample["wall_ms"] for sample in samples[concurrency]]),
        "cases_per_second": distribution([sample["cases_per_second"] for sample in samples[concurrency]]),
        "speedup_vs_serial": serial_median / statistics.median(sample["wall_ms"] for sample in samples[concurrency]),
    } for concurrency in config.concurrency]


def verify_integrity(cases: list[EvalCase]) -> dict:
    fingerprint = cases_sha(cases)
    options = {"scorers": [ExactMatchScorer()], "concurrency": 4, "dataset_sha": fingerprint}
    correct = run_eval(cases, ArithmeticRunner(), **options)
    degraded = run_eval(cases, ArithmeticRunner(degraded=True), **options)
    failing = run_eval(cases, ArithmeticRunner(fail=True), **options)
    repeated = run_eval(cases, ArithmeticRunner(), **options)
    comparison = compare_to_baseline(degraded.to_dict(), correct.to_dict(), threshold=0.01)
    unchanged = compare_to_baseline(repeated.to_dict(), correct.to_dict(), threshold=0.01)
    errors = [case for case in failing.case_results if case.error]
    expected_error_ids = [case.id for case in cases if "operation:-" in case.tags]
    expected_wrong_ids = [case.id for case in cases if "operation:*" in case.tags]
    wrong_ids = [case.case_id for case in degraded.case_results if case.scores["exact_match"] == 0]
    checks = {
        "correct_arithmetic_and_order": correct_and_ordered(correct, cases),
        "known_degradation_exactly_detected": wrong_ids == expected_wrong_ids and degraded.pass_rate == 0.75,
        "regression_gate_rejects_degraded_runner": not comparison.passed,
        "regression_gate_accepts_unchanged_runner": unchanged.passed,
        "all_cases_survive_injected_errors": [case.case_id for case in failing.case_results] == [case.id for case in cases],
        "injected_errors_are_isolated": [case.case_id for case in errors] == expected_error_ids and failing.error_count == len(expected_error_ids),
        "errored_cases_score_zero": all(case.scores == {"exact_match": 0.0} for case in errors),
        "healthy_cases_still_pass": all(case.scores == {"exact_match": 1.0} for case in failing.case_results if not case.error),
        "injected_errors_reduce_pass_rate": failing.pass_rate == 0.75,
    }
    return {
        "case_count": len(cases),
        "checks": checks,
        "correct_pass_rate": correct.pass_rate,
        "degraded_pass_rate": degraded.pass_rate,
        "degraded_case_ids": wrong_ids,
        "injected_error_count": failing.error_count,
        "injected_error_case_ids": [case.case_id for case in errors],
        "injected_error_pass_rate": failing.pass_rate,
        "gate_threshold_absolute": 0.01,
        "regressions": [asdict(delta) for delta in comparison.regressions],
        "interpretation": "Deterministic functional checks; these pass rates do not estimate model accuracy or CI reliability.",
    }


def run_benchmark(config: BenchmarkConfig, *, command: str | None = None) -> dict:
    config.validate()
    started = datetime.now(timezone.utc)
    source = source_provenance()
    cases = generate_cases(config.cases, config.seed)
    overhead_cases = generate_cases(config.overhead_cases, config.seed)
    suite = {
        "workload_version": WORKLOAD_VERSION,
        "config": asdict(config),
        "io_dataset_sha256": cases_sha(cases),
        "overhead_dataset_sha256": cases_sha(overhead_cases),
        "source_sha256": source["sha256"],
    }
    provenance = {
        "command": command or shlex.join(["python", *sys.argv]),
        "command_note": "Interpreter path normalized to python; runtime version is recorded separately.",
        "started_at_utc": started.isoformat(timespec="seconds"),
        "python_version": sys.version,
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "git": git_provenance(),
        "source": source,
    }
    overhead = benchmark_overhead(overhead_cases, config)
    concurrency = benchmark_concurrency(cases, config)
    integrity = verify_integrity(cases)
    passed = all(integrity["checks"].values()) and all(
        sample["verified"]
        for group in [overhead, *concurrency]
        for key in ("samples", "warmup_samples_excluded")
        for sample in group[key]
    )
    provenance["completed_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "schema_version": 1,
        "benchmark_kind": "synthetic-harness-operations",
        "passed": passed,
        "suite_sha256": canonical_sha(suite),
        "suite": suite,
        "provenance": provenance,
        "statistics": {
            "p95_method": "nearest rank over measured batch repetitions; with five samples P95 equals the maximum",
            "spread": "min, max and sample standard deviation of measured repetitions; not a confidence interval",
            "warmups": "recorded separately and excluded from all timing summaries",
            "speedup": "median serial batch wall time divided by median concurrent batch wall time",
        },
        "overhead": overhead,
        "concurrency": concurrency,
        "integrity": integrity,
        "limitations": [
            "No model, agent CLI, network service or LLM judge is called; no API credentials are used.",
            "Sleep-based concurrency measures controlled waiting, not real provider throughput or rate limits.",
            "Arithmetic is solved by deterministic code; 100% here is fixture correctness, not agent intelligence.",
            "Timing depends on this machine and its current load; small sample P95 is descriptive, not a stable tail estimate.",
            "Incremental overhead includes result construction, validation, per-case timing and aggregation, compared with a minimal runner/scorer loop.",
            "No cross-framework comparisons, model rankings, token counts or monetary costs are measured.",
        ],
    }


def render_markdown(result: dict) -> str:
    provenance = result["provenance"]
    config = result["suite"]["config"]
    git = provenance["git"]
    lines = [
        "# Reproducible harness benchmark",
        "",
        "**Scope: deterministic harness operations, not a model leaderboard.** No API credentials or model calls.",
        "",
        f"Measured {provenance['started_at_utc']} on {provenance['platform']} "
        f"({provenance['machine']}; Python {provenance['python_version'].split()[0]}; "
        f"{provenance['logical_cpu_count']} logical CPUs).",
        "",
        f"Git revision: `{git['revision']}`; working tree dirty: `{git['dirty']}`. "
        "Source file hashes and Git status are recorded in the JSON result.",
        "",
        f"Suite SHA-256: `{result['suite_sha256']}`",
        "",
        "Reproduction command (local interpreter path normalized to `python`):",
        "",
        "```sh",
        provenance["command"],
        "```",
        "",
        "## Controlled waiting and concurrency",
        "",
        f"{config['cases']} generated arithmetic cases per batch, {config['latency_ms']:g} ms requested sleep per case; "
        f"{config['warmups']} warmup(s) excluded and {config['repetitions']} measured repetitions per concurrency level.",
        "",
        "| Concurrency | Median batch ms | P95 batch ms | Min–max ms | Median cases/s | Speedup vs serial |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in result["concurrency"]:
        wall = row["wall_ms"]
        lines.append(
            f"| {row['concurrency']} | {wall['median']:.2f} | {wall['p95']:.2f} | "
            f"{wall['min']:.2f}–{wall['max']:.2f} | {row['cases_per_second']['median']:.1f} | "
            f"{row['speedup_vs_serial']:.2f}× |"
        )
    overhead = result["overhead"]
    integrity = result["integrity"]
    lines.extend([
        "",
        "P95 uses the nearest-rank method over batch repetitions. With five samples it is the maximum. "
        "This small sample describes this run; it is not a stable service latency estimate. "
        "Actual sleep can exceed the requested duration. Concurrency levels are interleaved and their execution order alternates.",
        "",
        "## Incremental harness overhead",
        "",
        f"{overhead['case_count']} no-wait arithmetic cases, serial execution, paired direct runner + scoring loop "
        "versus full evaluation. The pair execution order alternates. Generation/import time is excluded.",
        "",
        "| Measurement | Median | P95 | Min–max |",
        "| --- | ---: | ---: | ---: |",
    ])
    for key, label, unit in (
        ("direct_loop_ms", "Direct loop", "ms"),
        ("harness_ms", "Full harness", "ms"),
        ("incremental_us_per_case", "Paired incremental work per case", "µs"),
    ):
        summary = overhead[key]
        lines.append(f"| {label} | {summary['median']:.3f} {unit} | {summary['p95']:.3f} {unit} | {summary['min']:.3f}–{summary['max']:.3f} {unit} |")
    lines.extend([
        "",
        "Incremental work includes result objects, validation, per-case timing and aggregation. "
        "It is a measured difference from a minimal loop, not a claim about every integration or a pure profiler attribution.",
        "",
        "## Deterministic functional checks",
        "",
        f"Overall verification: **{'PASS' if result['passed'] else 'FAIL'}**.",
        "",
        f"Correct calculator: {integrity['correct_pass_rate']:.0%}; deliberately wrong multiplication: "
        f"{integrity['degraded_pass_rate']:.0%}; injected subtraction failures: "
        f"{integrity['injected_error_count']}/{integrity['case_count']} with "
        f"{integrity['injected_error_pass_rate']:.0%} passing. The regression threshold is "
        f"{integrity['gate_threshold_absolute']:.0%} absolute.",
        "",
        "| Check | Observed |",
        "| --- | --- |",
    ])
    lines.extend(f"| {name.replace('_', ' ')} | {'PASS' if passed else 'FAIL'} |" for name, passed in integrity["checks"].items())
    lines.extend(["", "These are functional checks, not estimates of model accuracy or CI reliability.", "", "## Limits", ""])
    lines.extend(f"- {limitation}" for limitation in result["limitations"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/latest.json"))
    parser.add_argument("--markdown", type=Path, default=Path("benchmarks/latest.md"))
    parser.add_argument("--cases", type=int, default=128)
    parser.add_argument("--overhead-cases", type=int, default=4096)
    parser.add_argument("--latency-ms", type=float, default=5.0)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20261004)
    args = parser.parse_args(argv)
    config = BenchmarkConfig(**{name: getattr(args, name) for name in (
        "cases", "overhead_cases", "latency_ms", "repetitions", "warmups", "seed",
    )})
    try:
        config.validate()
    except ValueError as error:
        parser.error(str(error))
    result = run_benchmark(config)
    for path, content in (
        (args.output, json.dumps(result, indent=2, allow_nan=False) + "\n"),
        (args.markdown, render_markdown(result)),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(f"{'PASS' if result['passed'] else 'FAIL'}: benchmark JSON {args.output}; summary {args.markdown}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
