"""Self-contained HTML report for an eval run.

One file, no external assets: metric summary, optional baseline comparison,
and a per-case table sorted worst-first so failures surface at the top.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

_CSS = """
:root { color-scheme: light dark; }
body { font-family: system-ui, -apple-system, sans-serif; margin: 2rem auto; max-width: 1100px;
       padding: 0 1rem; line-height: 1.45; }
h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.88rem; }
th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid rgba(128,128,128,0.35);
         vertical-align: top; }
th { font-weight: 600; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.meta { color: rgba(128,128,128,0.95); font-size: 0.85rem; }
.regression { color: #c0392b; font-weight: 700; }
.ok { color: #27ae60; }
.error { color: #c0392b; }
.io { max-width: 26rem; overflow-wrap: anywhere; }
.scroll { overflow-x: auto; }
details { margin: 1rem 0; border: 1px solid #8885; border-radius: 8px; padding: 0.8rem; }
summary { cursor: pointer; font-weight: 600; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 0.8rem; }
"""


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "-"


def render_report(result: dict, comparison=None, title: str = "Eval report") -> str:
    if result.get("type") == "multi_run":
        means = result["mean"]
        overview = {
            "run_id": "repeated-run-summary", "timestamp": "",
            "dataset_sha": result.get("dataset_sha"),
            "metadata": {**result.get("metadata", {}), "run_count": result["run_count"]},
            "scores": {name.removeprefix("scorer:"): {"mean": mean}
                       for name, mean in means.items() if name.startswith("scorer:")},
            "pass_rate": means["pass_rate"],
            "error_count": sum(run.get("error_count", 0) for run in result["runs"]),
        }
        if "trajectory" in means:
            overview["trajectory_score"] = {"mean": means["trajectory"]}
        parts = [render_report(overview, comparison, title)]
        parts.append("<h2>Across repeated runs</h2><p>Means and sample standard deviations across runs. "
                     "The baseline comparison above uses these means; case details below belong to each run.</p>")
        parts.append("<table><tr><th>Metric</th><th>Mean</th><th>Sample std</th></tr>")
        for name, mean in means.items():
            parts.append(f"<tr><td>{_esc(name)}</td><td>{_fmt(mean)}</td><td>{_fmt(result['std'][name])}</td></tr>")
        parts.append("</table>")
        for i, run in enumerate(result["runs"], 1):
            parts.append(f"<details><summary>Run {i}: {_esc(run.get('run_id'))}</summary>"
                         + render_report(run, title=f"Run {i}") + "</details>")
        return "\n".join(parts)
    scorer_names = list(result.get("scores", {}))
    cases = result.get("cases", [])

    parts = [f"<style>{_CSS}</style>", f"<h1>{_esc(title)}</h1>"]

    meta = result.get("metadata", {})
    if meta.get("evaluation_mode") == "rescore":
        parts.append("<p><strong>Rescored recording.</strong> Agent timings, usage and cost below belong to the "
                     "original execution. No agent was called again. New grading time is recorded per case.</p>")
    meta_bits = [f"run <code>{_esc(result.get('run_id'))}</code>", _esc(result.get("timestamp"))]
    if result.get("dataset_sha"):
        meta_bits.append(f"dataset <code>{_esc(result['dataset_sha'])}</code>")
    meta_bits.extend(f"{_esc(k)}: <code>{_esc(v)}</code>" for k, v in meta.items())
    parts.append(f"<p class='meta'>{' &middot; '.join(meta_bits)}</p>")

    # --- metric summary ---
    parts.append("<h2>Metrics</h2><table><tr><th>Metric</th><th>Mean</th></tr>")
    for name in scorer_names:
        parts.append(f"<tr><td>{_esc(name)}</td><td class='num'>{_fmt(result['scores'][name]['mean'])}</td></tr>")
    if result.get("trajectory_score") is not None:
        parts.append(f"<tr><td>trajectory</td><td class='num'>{_fmt(result['trajectory_score']['mean'])}</td></tr>")
    parts.append(f"<tr><td>pass_rate</td><td class='num'>{_fmt(result.get('pass_rate'))}</td></tr>")
    error_count = result.get("error_count", 0)
    error_class = "error" if error_count else "ok"
    parts.append(f"<tr><td>errors</td><td class='num {error_class}'>{_esc(error_count)}</td></tr></table>")

    summary = result.get("summary", {})
    if summary:
        parts.append("<h2>Reliability, latency and reported usage</h2>")
        interval = summary.get("pass_rate_ci95", {})
        if interval:
            parts.append(f"<p>Pass rate: 95% Wilson interval {_fmt(interval.get('lower'))}–{_fmt(interval.get('upper'))}. "
                         f"{_esc(interval.get('assumptions', 'Assumes independent, representative cases.'))}</p>")
        parts.append("<table><tr><th>Measurement</th><th>Value</th></tr>")
        for key, label in (("latency_ms", "Agent + scoring latency"), ("agent_latency_ms", "Agent latency")):
            latency = summary.get(key, {})
            parts.append(f"<tr><td>{label}</td><td>p50 {_fmt(latency.get('p50'))} ms · "
                         f"p95 {_fmt(latency.get('p95'))} ms · p99 {_fmt(latency.get('p99'))} ms</td></tr>")
        for key, label in (("cost_usd", "Reported agent cost (USD)"), ("tokens", "Reported agent tokens")):
            usage = summary.get(key, {})
            parts.append(f"<tr><td>{label}</td><td>{_fmt(usage.get('reported_total'))} "
                         f"· reported on {_esc(usage.get('reported_count', 0))}/{_esc(summary.get('case_count', 0))} cases</td></tr>")
        parts.append("</table><p class='meta'>Missing usage is unknown, never assumed free. "
                     "Reported costs cover the agent, not optional model-based scorers.</p>")
        if summary.get("by_tag"):
            parts.append("<h2>Dataset slices</h2><table><tr><th>Tag</th><th>Cases</th><th>Pass rate</th><th>Errors</th></tr>")
            for tag, group in summary["by_tag"].items():
                parts.append(f"<tr><td>{_esc(tag)}</td><td>{_esc(group.get('case_count'))}</td>"
                             f"<td>{_fmt(group.get('pass_rate'))}</td><td>{_esc(group.get('error_count'))}</td></tr>")
            parts.append("</table>")

    # --- baseline comparison ---
    if comparison is not None:
        verdict = ("<span class='ok'>PASS</span>" if comparison.passed
                   else f"<span class='regression'>FAIL &mdash; {len(comparison.regressions)} regression(s)</span>")
        parts.append(f"<h2>Baseline comparison &mdash; {verdict}</h2>")
        parts.append("<table><tr><th>Metric</th><th>Baseline</th><th>Current</th><th>Delta</th><th></th></tr>")
        for d in comparison.deltas:
            flag = "<span class='regression'>REGRESSION</span>" if d in comparison.regressions else "<span class='ok'>ok</span>"
            parts.append(
                f"<tr><td>{_esc(d.metric)}</td><td class='num'>{_fmt(d.baseline)}</td>"
                f"<td class='num'>{_fmt(d.current)}</td><td class='num'>{d.delta:+.3f}</td><td>{flag}</td></tr>"
            )
        parts.append("</table>")

    # --- per-case detail, worst first ---
    def case_mean(c: dict) -> float:
        values = list(c.get("scores", {}).values())
        if c.get("trajectory_score") is not None:
            values.append(c["trajectory_score"])
        return sum(values) / len(values) if values else 0.0

    if cases:
        has_trajectory = any(c.get("trajectory_score") is not None for c in cases)
        parts.append("<h2>Cases (worst first)</h2><div class='scroll'><table>")
        headers = ["id", "input", "expected", "actual"] + [_esc(n) for n in scorer_names]
        if has_trajectory:
            headers.append("trajectory")
        headers += ["latency&nbsp;ms", "error"]
        parts.append("<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>")

        for c in sorted(cases, key=case_mean):
            row = [
                f"<td><code>{_esc(c['id'])}</code></td>",
                f"<td class='io'>{_esc(c['input'])}</td>",
                f"<td class='io'>{_esc(c['expected_output'])}</td>",
                f"<td class='io'>{_esc(c['output'])}</td>",
            ]
            row += [f"<td class='num'>{_fmt(c['scores'].get(n))}</td>" for n in scorer_names]
            if has_trajectory:
                row.append(f"<td class='num'>{_fmt(c.get('trajectory_score'))}</td>")
            latency = c.get("latency_ms")
            row.append(f"<td class='num'>{latency:.0f}</td>" if latency is not None else "<td class='num'>-</td>")
            row.append(f"<td class='error io'>{_esc(c.get('error') or '')}</td>")
            parts.append("<tr>" + "".join(row) + "</tr>")
        parts.append("</table></div>")
        for c in cases:
            if c.get("trajectory") or c.get("events") or c.get("usage") or c.get("metadata"):
                detail = {key: c.get(key) for key in ("trajectory", "events", "usage", "cost_usd", "error_stage", "rescore_latency_ms", "metadata")}
                parts.append(f"<details><summary>{_esc(c['id'])}: trace and usage</summary>"
                             f"<pre>{_esc(json.dumps(detail, indent=2, ensure_ascii=False))}</pre></details>")

    return "\n".join(parts)


def write_report(path: str | Path, result: dict, comparison=None, title: str = "Eval report") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = f"<!doctype html><html lang='en'><head><meta name='viewport' content='width=device-width, initial-scale=1'><meta charset='utf-8'><title>{html.escape(title)}</title></head><body>{render_report(result, comparison, title)}</body></html>"
    path.write_text(doc, encoding="utf-8")
    return path
