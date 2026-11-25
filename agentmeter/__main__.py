"""Command line: python -m agentmeter <command> ..."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

from . import __version__
from .contract import ContractError, load
from .grade import Thresholds, gate, gate_all, score
from .policy import audit

LABEL = {"ship": "SHIP", "hold": "HOLD", "block": "BLOCK"}


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _money(x):
    return "n/a" if x is None else f"${x:.4f}"


def _add_inputs(p):
    p.add_argument("path", help="folder with runs.jsonl, tasks.json and (optional) policy.json, or a runs.jsonl file")
    p.add_argument("--tasks", help="task suite file, if not in the same folder")
    p.add_argument("--policy", help="policy file, if not in the same folder")


def _thresholds(a) -> Thresholds:
    return Thresholds(a.max_success_drop / 100, a.max_cost_increase / 100, a.max_latency_increase / 100)


def _add_thresholds(p):
    p.add_argument("--max-success-drop", type=float, default=5, metavar="POINTS",
                   help="largest success drop to tolerate, in percentage points (default 5)")
    p.add_argument("--max-cost-increase", type=float, default=20, metavar="PCT",
                   help="largest increase in cost per successful run, in percent (default 20)")
    p.add_argument("--max-latency-increase", type=float, default=25, metavar="PCT",
                   help="largest increase in p95 latency, in percent (default 25)")


def _add_model(p):
    p.add_argument("--provider", choices=["anthropic", "openai"], required=True)
    p.add_argument("--model", required=True, help="model name, as your provider spells it")
    p.add_argument("--base-url", help="OpenAI-compatible endpoint (openai provider only)")
    p.add_argument("--price-in", type=float, default=0.0, metavar="USD", help="price per million input tokens")
    p.add_argument("--price-out", type=float, default=0.0, metavar="USD", help="price per million output tokens")


def _model(a):
    from .harness import AnthropicModel, OpenAIModel
    if a.provider == "anthropic":
        return AnthropicModel(a.model, price_in_per_mtok=a.price_in, price_out_per_mtok=a.price_out)
    kw = {"base_url": a.base_url} if a.base_url else {}
    return OpenAIModel(a.model, price_in_per_mtok=a.price_in, price_out_per_mtok=a.price_out, **kw)


def _live(a) -> int:
    import json as _json
    from .contract import parse_policy
    from .demo import AGENT_ID, POLICY, SUPPORT_SYSTEM_PROMPT, support_toolbox, write_suite
    from .harness import MemoryToolbox, run_suite
    from .mcp import MCPToolbox
    from .policy import Guard
    model = _model(a)
    out = Path(a.out)
    tasks = write_suite(out)[: a.limit] if a.limit else write_suite(out)
    tools = MCPToolbox([sys.executable, "-m", "agentmeter.mcp"]) if a.via_mcp else support_toolbox()
    if a.memory:
        tools = MemoryToolbox(tools, out / f"memory-{a.version}.json")
    guard = None if a.no_guard else Guard(parse_policy(_json.dumps(POLICY)), agent_id=AGENT_ID)
    print(f"Running {a.model} as '{a.version}' on {len(tasks)} tasks x {a.trials} trials"
          f"{' with memory' if a.memory else ''}{' via MCP' if a.via_mcp else ''}...")
    try:
        info = run_suite(model, tools, tasks, out / "runs.jsonl", a.version, a.trials, guard,
                         SUPPORT_SYSTEM_PROMPT, AGENT_ID, a.max_steps,
                         progress=lambda m: print("  " + m) if "trial" in m else None)
    finally:
        inner = getattr(tools, "inner", tools)
        if hasattr(inner, "close"):
            inner.close()
    print(f"Done: {info['runs']} runs, {info['errors']} with errors. Next: agentmeter judge {out} "
          f"--provider {a.provider} --model {a.model}, then agentmeter score {out}")
    return 0


def _check_version(name, scores):
    names = [s.version for s in scores]
    if name not in names:
        raise ContractError(f"version '{name}' not found; versions in the data: {', '.join(names)}")
    return next(s for s in scores if s.version == name)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agentmeter", description="Evaluate AI agents before they ship.")
    ap.add_argument("--version", action="version", version=f"agentmeter {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("score", help="scorecard for every agent version, and the gate against the first one")
    _add_inputs(p)
    p.add_argument("--baseline", help="version to compare the others against (default: the first in the file)")
    _add_thresholds(p)
    p.add_argument("--json", action="store_true", help="print everything as JSON")

    p = sub.add_parser("gate", help="release gate for one candidate version; exit code 0 means ship")
    _add_inputs(p)
    p.add_argument("--candidate", required=True, help="version to check")
    p.add_argument("--baseline", help="version to compare against (default: the first in the file)")
    _add_thresholds(p)
    p.add_argument("--allow-hold", action="store_true", help="exit 0 on Hold too; only Block fails")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("report", help="write a shareable HTML report")
    _add_inputs(p)
    p.add_argument("--baseline")
    _add_thresholds(p)
    p.add_argument("-o", "--output", default="agent-report.html")

    p = sub.add_parser("audit", help="every tool call with its policy decision")
    _add_inputs(p)
    p.add_argument("--csv", metavar="FILE", help="write the full audit log to a CSV file")
    p.add_argument("--limit", type=int, default=20, metavar="N", help="violations to list on screen (default 20)")

    p = sub.add_parser("demo", help="run the synthetic demo agent and write sample data")
    p.add_argument("out_dir", nargs="?", default="data")
    p.add_argument("--seed", type=int, default=42)

    p = sub.add_parser("run", help="run a real model as the support agent on the demo task suite")
    _add_model(p)
    p.add_argument("--version", required=True, help="label for this agent version, e.g. baseline or with-memory")
    p.add_argument("--out", default="live", help="folder for runs.jsonl, tasks.json and policy.json (default live)")
    p.add_argument("--trials", type=int, default=3, help="attempts per task (default 3)")
    p.add_argument("--limit", type=int, help="run only the first N tasks, for a quick test")
    p.add_argument("--memory", action="store_true", help="give the agent save_note / search_notes tools that persist across runs")
    p.add_argument("--via-mcp", action="store_true", help="serve the tools from an MCP server subprocess")
    p.add_argument("--no-guard", action="store_true", help="turn off the runtime permission guard")
    p.add_argument("--max-steps", type=int, default=8)

    p = sub.add_parser("judge", help="grade open-ended answers against each task's rubric with a model")
    _add_inputs(p)
    _add_model(p)
    p.add_argument("--redo", action="store_true", help="judge again runs that already have a verdict")

    sub.add_parser("mcp-server", help="serve the demo support tools as an MCP server over stdio")

    a = ap.parse_args(argv)
    try:
        return _run(a)
    except BrokenPipeError:
        # output piped into a command that stopped reading early (e.g. | head)
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    except FileNotFoundError as e:
        print(f"error: file not found: {e.filename or e}", file=sys.stderr)
        return 2
    except (ContractError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _run(a) -> int:
    if a.cmd == "mcp-server":
        from .demo import support_toolbox
        from .mcp import serve
        serve(support_toolbox(), name="agentmeter-demo-support-tools")
        return 0

    if a.cmd == "run":
        return _live(a)

    if a.cmd == "judge":
        from .contract import _read, parse_suite, resolve_paths
        from .judge import judge_runs
        runs_p, tasks_p, _ = resolve_paths(a.path, a.tasks, a.policy)
        info = judge_runs(runs_p, parse_suite(_read(tasks_p), tasks_p.name), _model(a), a.redo)
        print(f"Judged {info['judged']} runs ({info['unparsed']} unreadable verdicts, {info['skipped']} skipped: "
              f"no rubric or already judged).")
        return 0

    if a.cmd == "demo":
        from .demo import generate
        info = generate(a.out_dir, a.seed)
        print(f"Wrote {info['runs']:,} runs of {info['versions']} agent versions on {info['tasks']} tasks to "
              f"{Path(a.out_dir)}/ (runs.jsonl, tasks.json, policy.json).")
        return 0

    runs, suite, policy = load(a.path, a.tasks, a.policy)

    if a.cmd == "audit":
        entries = audit(runs, policy)
        counts = {}
        for x in entries:
            counts[x.decision] = counts.get(x.decision, 0) + 1
        print(f"{len(entries):,} tool calls in {len(runs):,} runs: " +
              ", ".join(f"{n:,} {k}{'s' if k == 'violation' and n != 1 else ''}" for k, n in counts.items()))
        if policy is None:
            print("No policy.json given, so calls were not checked.")
        violations = [x for x in entries if x.decision == "violation"]
        for x in violations[:a.limit]:
            print(f"VIOLATION  {x.run_id} step {x.step}: {x.reason}")
        if len(violations) > a.limit:
            print(f"... and {len(violations) - a.limit:,} more" + ("" if a.csv else " (use --csv to save them all)"))
        if a.csv:
            with open(a.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["run_id", "agent_version", "task_id", "step", "tool", "args", "decision", "reason"])
                for x in entries:
                    w.writerow([x.run_id, x.agent_version, x.task_id, x.step, x.tool,
                                json.dumps(x.args, sort_keys=True), x.decision, x.reason])
            print(f"Wrote the audit log to {a.csv}")
        return 0

    scores = score(runs, suite, policy)
    t = _thresholds(a)
    if a.baseline:
        _check_version(a.baseline, scores)

    if a.cmd == "gate":
        cand = _check_version(a.candidate, scores)
        base = _check_version(a.baseline, scores) if a.baseline else scores[0]
        if cand.version == base.version:
            raise ContractError("the candidate and the baseline are the same version; pick another with --baseline")
        g = gate(base, cand, t)
        if a.json:
            print(json.dumps(g.to_dict(), indent=2))
        else:
            print(f"{LABEL[g.verdict]}  {g.candidate} vs {g.baseline}")
            for r in g.reasons:
                print(f"  - {r}")
        return 0 if g.verdict == "ship" or (g.verdict == "hold" and a.allow_hold) else 1

    gates = gate_all(scores, a.baseline, t)

    if a.cmd == "report":
        from .report import render
        Path(a.output).write_text(render(scores, gates, suite.name), encoding="utf-8")
        print(f"Wrote {a.output}")
        return 0

    if a.json:
        print(json.dumps({"scores": [s.to_dict() for s in scores], "gates": [g.to_dict() for g in gates]}, indent=2))
        return 0
    w = max(len(s.version) for s in scores)
    print(f"{suite.name or 'Task suite'}: {len(suite.tasks)} tasks, {len(scores)} version{'s' if len(scores) != 1 else ''}, {len(runs):,} runs\n")
    for s in scores:
        print(f"{s.version:<{w}}  success {_pct(s.success.estimate)} ({_pct(s.success.low)} to {_pct(s.success.high)})"
              f"  pass^{s.k} {s.pass_k * 100:.0f}%  violations {s.violations}"
              f"  cost/success {_money(s.cost_per_success)}  p95 {s.latency_p95:.1f}s")
    if gates:
        print(f"\nRelease gate vs {gates[0].baseline}")
        for g in gates:
            print(f"{LABEL[g.verdict]:<6} {g.candidate}: {g.reasons[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
