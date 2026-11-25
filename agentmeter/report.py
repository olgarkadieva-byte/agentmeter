"""Render scorecards and release-gate results as one self-contained HTML report."""

from __future__ import annotations

import html
from datetime import date

from .grade import GateResult, VersionScore

LABEL = {"ship": "Ship", "hold": "Hold", "block": "Block"}

CSS = """
:root{--bg:#fff;--fg:#1c1d21;--muted:#5d6170;--line:#e3e5ea;--card:#f7f8fa;--accent:#2b59c3;
--ship:#1f7a4d;--ship-bg:#e3f4ea;--hold:#8a5a00;--hold-bg:#fff1d6;--block:#a32a2a;--block-bg:#fbe4e4}
@media (prefers-color-scheme:dark){:root{--bg:#121316;--fg:#ececf0;--muted:#a3a7b3;--line:#2b2d34;--card:#1b1d22;--accent:#8fb0ff;
--ship:#6fd19c;--ship-bg:#143022;--hold:#f0c060;--hold-bg:#33270c;--block:#f08a8a;--block-bg:#3a1717}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:40px 20px 64px}
h1{font-size:1.9rem;margin:0 0 6px;letter-spacing:-.01em}h2{font-size:1.25rem;margin:36px 0 12px}
.sub{color:var(--muted);margin:0 0 24px}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.93rem}th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.range{color:var(--muted);font-size:.8rem}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:700;font-size:.82rem}
.ship{color:var(--ship);background:var(--ship-bg)}.hold{color:var(--hold);background:var(--hold-bg)}.block{color:var(--block);background:var(--block-bg)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}
.card h3{margin:0 0 8px;font-size:1.05rem;display:flex;justify-content:space-between;gap:8px;align-items:center}
.card ul{margin:8px 0 0;padding-left:18px}.card li{margin:4px 0}
.note{border-left:3px solid var(--accent);padding:8px 14px;background:var(--card);border-radius:0 8px 8px 0}
footer{margin-top:40px;color:var(--muted);font-size:.85rem}
"""


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _money(x) -> str:
    return "n/a" if x is None else f"${x:.4f}"


def _chg(x) -> str:
    return "n/a" if x is None else f"{x * 100:+.0f}%"


def render(scores: list[VersionScore], gates: list[GateResult], suite_name: str = "",
           title: str = "Agent evaluation report") -> str:
    e = html.escape
    rows = "".join(
        f"<tr><td>{e(s.version)}</td>"
        f"<td class='num'>{_pct(s.success.estimate)}<div class='range'>{_pct(s.success.low)} to {_pct(s.success.high)}</div></td>"
        f"<td class='num'>{s.pass_k * 100:.0f}%</td><td class='num'>{_pct(s.tool_accuracy)}</td>"
        f"<td class='num'>{s.violations}</td><td class='num'>{s.blocked_calls}</td>"
        f"<td class='num'>{_money(s.cost_per_success)}</td><td class='num'>{s.latency_p95:.1f}s</td>"
        f"<td class='num'>{s.runs:,}</td></tr>" for s in scores)
    k = scores[0].k if scores else 1

    gate_cards = "".join(f"""
<div class="card"><h3>{e(g.candidate)} vs {e(g.baseline)} <span class="badge {g.verdict}">{LABEL[g.verdict]}</span></h3>
<div class="range">Success change {g.success_diff.estimate * 100:+.1f} points (95% range {g.success_diff.low * 100:+.1f} to
{g.success_diff.high * 100:+.1f}) on {g.common_tasks} shared tasks · cost per success {_chg(g.cost_change)} · p95 latency {_chg(g.latency_change)}</div>
<ul>{''.join(f'<li>{e(r)}</li>' for r in g.reasons)}</ul></div>""" for g in gates)

    cats = list(dict.fromkeys(c for s in scores for c in s.by_category))
    cat_rows = "".join(
        f"<tr><td>{e(c)}</td>" + "".join(
            f"<td class='num'>{_pct(s.by_category.get(c))}</td>" for s in scores) + "</tr>" for c in cats)
    fails = list(dict.fromkeys(f for s in scores for f in s.failures))
    fail_rows = "".join(
        f"<tr><td>{e(f)}</td>" + "".join(f"<td class='num'>{s.failures.get(f, 0)}</td>" for s in scores) + "</tr>"
        for f in fails)
    heads = "".join(f"<th class='num'>{e(s.version)}</th>" for s in scores)
    viol = "".join(f"<li><b>{e(s.version)}</b>: {e(x)}</li>" for s in scores for x in s.violation_examples)

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>{e(title)}</h1>
<p class="sub">{e(suite_name) + ' · ' if suite_name else ''}Generated {date.today().isoformat()}.
{sum(s.runs for s in scores):,} runs across {len(scores)} agent version{'s' if len(scores) != 1 else ''}.</p>
<div class="scroll"><table><thead><tr><th>Version</th><th class="num">Task success</th><th class="num">pass^{k}</th>
<th class="num">Right tools</th><th class="num">Policy violations</th><th class="num">Blocked by guard</th>
<th class="num">Cost / success</th><th class="num">p95 latency</th><th class="num">Runs</th></tr></thead><tbody>{rows}</tbody></table></div>
{'<h2>Release gate</h2><div class="cards">' + gate_cards + '</div>' if gates else ''}
<h2>Success by category</h2>
<div class="scroll"><table><thead><tr><th>Category</th>{heads}</tr></thead><tbody>{cat_rows}</tbody></table></div>
{'<h2>Why runs failed</h2><div class="scroll"><table><thead><tr><th>Failure</th>' + heads + '</tr></thead><tbody>' + fail_rows + '</tbody></table></div>' if fails else ''}
{'<h2>Policy violation examples</h2><ul>' + viol + '</ul>' if viol else ''}
<h2>How to read this</h2>
<p class="note">Task success ranges are 95% intervals, clustered by task. pass^{k} is the share of tasks the agent gets
right on all {k} attempts: a measure of reliability, not just average skill. <b>Ship</b>: no policy violations, success
is no worse than the baseline beyond the margin, and cost and latency stay within budget. <b>Hold</b>: something is over
budget or not yet proven. <b>Block</b>: a policy violation, or success is significantly worse.</p>
<footer>Made with AgentMeter by Olga Arkadieva.</footer>
</div></body></html>
"""
