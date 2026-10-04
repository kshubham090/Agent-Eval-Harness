"""Auditable quality and resource gates over validated, saved case evidence."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from harness.results import _nonnegative, validate_probability


@dataclass
class GateResult:
    passed: bool
    checks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_gates(
    result: dict, *, min_pass_rate: float | None = None,
    max_error_rate: float | None = 0.0, max_cost_usd: float | None = None,
    max_p95_latency_ms: float | None = None,
) -> GateResult:
    """Require quality/agent latency per run, and total agent cost across all runs.

    Resource gates require coverage on every attempted case. A rescore retains
    the original agent measurements; these are not rescore execution budgets.
    Limits are post-run acceptance gates, not spend caps or request cancellation.
    """
    from harness.replay import validate_recording

    for name, value in (("min_pass_rate", min_pass_rate), ("max_error_rate", max_error_rate)):
        if value is not None:
            validate_probability(value, name)
    for name, value in (("max_cost_usd", max_cost_usd), ("max_p95_latency_ms", max_p95_latency_ms)):
        if value is not None:
            _nonnegative(value, name)
    data = validate_recording(result)
    runs = data["runs"] if data.get("type") == "multi_run" else [data]
    checks = []

    def check(metric, value, limit, *, minimum=False, run=None, missing=0):
        close = value is not None and math.isclose(value, limit, rel_tol=0,
                                                  abs_tol=4 * math.ulp(max(value, limit)))
        passed = not missing and value is not None and (close or (value >= limit if minimum else value <= limit))
        checks.append({"metric": metric, "run_id": run, "value": value, "limit": limit,
                       "operator": ">=" if minimum else "<=", "passed": bool(passed),
                       "missing_count": missing,
                       "reason": "missing measurements" if missing else ("within limit" if passed else "limit exceeded")})

    for run in runs:
        summary = run["summary"]
        if min_pass_rate is not None:
            check("pass_rate", summary["pass_rate"], min_pass_rate, minimum=True, run=run["run_id"])
        if max_error_rate is not None:
            check("error_rate", summary["error_rate"], max_error_rate, run=run["run_id"])
        if max_p95_latency_ms is not None:
            latency = summary["agent_latency_ms"]
            check("agent_latency_p95_ms", latency["p95"], max_p95_latency_ms,
                  run=run["run_id"], missing=latency["missing_count"])
    if max_cost_usd is not None:
        cost = [run["summary"]["cost_usd"] for run in runs]
        missing = sum(c["missing_count"] for c in cost)
        total = math.fsum(c["reported_total"] or 0 for c in cost)
        check("total_agent_cost_usd", total if not missing else None, max_cost_usd, missing=missing)
    return GateResult(passed=all(c["passed"] for c in checks), checks=checks)
