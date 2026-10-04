"""Composite action entry point. Inputs are data, never interpolated shell code."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
    directory = os.environ.get("AE_OUTPUT_DIRECTORY", "agent-eval-results")
    if not directory or any(c in directory for c in "\r\n"):
        raise ValueError("output-directory must be a nonempty path without newlines")
    target = (workspace / directory).resolve()
    if target == workspace or not target.is_relative_to(workspace):
        raise ValueError("output-directory must be a subdirectory inside the repository")
    if target.exists():
        raise ValueError("output-directory already exists; choose a new directory to preserve earlier evidence")
    mode = os.environ.get("AE_MODE", "output")
    if mode not in {"output", "coding"}:
        raise ValueError("mode must be output or coding")
    limits = []
    for key, flag, default in (
        ("AE_MIN_PASS_RATE", "--min-pass-rate", "1"),
        ("AE_MAX_ERROR_RATE", "--max-error-rate", "0"),
        ("AE_MAX_COST_USD", "--max-cost-usd", ""),
        ("AE_MAX_P95_LATENCY_MS", "--max-p95-latency-ms", ""),
    ):
        raw = os.environ.get(key, default).strip()
        if raw:
            value = float(raw)
            if not math.isfinite(value) or value < 0 or ("RATE" in key and value > 1):
                raise ValueError(f"{flag} is outside its valid range")
            limits += [flag, str(value)]
    result, report, gates = (target / name for name in ("result.json", "report.html", "gates.json"))
    base = [sys.executable, "-I", "-m", "harness"]
    if mode == "output":
        command = base + ["eval", "--config", os.environ.get("AE_CONFIG", "agent-eval.toml"),
                          "--output", str(result), "--html", str(report), "--allow-errors"]
    else:
        argv = os.environ.get("AE_COMMAND", "")
        try:
            parsed = json.loads(argv)
        except ValueError:
            raise ValueError("coding mode requires command as a JSON argv array") from None
        if not isinstance(parsed, list) or not parsed or not all(isinstance(x, str) for x in parsed):
            raise ValueError("coding mode requires command as a JSON argv array")
        network = os.environ.get("AE_NETWORK", "none")
        if network not in {"none", "bridge"}:
            raise ValueError("network must be none or bridge")
        command = base + ["coding", "--pack", os.environ.get("AE_PACK", "coding-starter@1.0.0"),
                          "--command", argv, "--network", network, "--artifacts", str(target / "artifacts"),
                          "--max-error-rate", os.environ.get("AE_MAX_ERROR_RATE", "0").strip() or "0",
                          "--output", str(result), "--html", str(report)]
        if os.environ.get("AE_IMAGE"):
            command += ["--image", os.environ["AE_IMAGE"]]
        for name in os.environ.get("AE_AGENT_ENV", "").splitlines():
            if name.strip():
                command += ["--env", name.strip()]
    target.mkdir(parents=True)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            for key, path in (("result", result), ("report", report), ("gates", gates), ("artifacts", target / "artifacts")):
                stream.write(f"{key}={path}\n")
    evaluation = subprocess.run(command, cwd=workspace, check=False)
    gate_status = 1
    if result.is_file():
        gate_status = subprocess.run(base + ["gate", "--result", str(result), "--output", str(gates), *limits],
                                     cwd=workspace, check=False).returncode
    passed = evaluation.returncode == 0 and gate_status == 0
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(f"## Agent evaluation: {'PASS' if passed else 'FAIL'}\n\n")
            stream.write("Full answers, grader details and coverage are in the evaluation artifacts. "
                         "Cost limits cover reported agent usage and are checked after execution.\n")
            if gates.is_file():
                checks = json.loads(gates.read_text(encoding="utf-8"))["checks"]
                stream.write("\n| Gate | Measured | Limit | Result |\n|---|---:|---:|---|\n")
                for c in checks:
                    value = "unknown" if c["value"] is None else f"{c['value']:.6g}"
                    stream.write(f"| {c['metric']} | {value} | {c['operator']} {c['limit']:.6g} | "
                                 f"{'PASS' if c['passed'] else 'FAIL'} |\n")
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"Action configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2)
