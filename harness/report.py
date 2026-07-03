"""Self-contained HTML report for an eval run.

One file, no external assets: metric summary, optional baseline comparison,
and a per-case table sorted worst-first so failures surface at the top.
"""

from __future__ import annotations

import html
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
"""


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "-"


def render_report(result: dict, comparison=None, title: str = "Eval report") -> str:
    scorer_names = list(result.get("scores", {}))
    cases = result.get("cases", [])

    parts = [f"<style>{_CSS}</style>", f"<h1>{_esc(title)}</h1>"]

    meta = result.get("metadata", {})
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
    parts.append(f"<tr><td>errors</td><td class='num {error_class}'>{error_count}</td></tr></table>")

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
        headers = ["id", "input", "expected", "actual"] + scorer_names
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

    return "\n".join(parts)


def write_report(path: str | Path, result: dict, comparison=None, title: str = "Eval report") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title></head><body>{render_report(result, comparison, title)}</body></html>"
    path.write_text(doc, encoding="utf-8")
    return path
