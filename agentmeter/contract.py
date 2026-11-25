"""The data contract: the three files AgentMeter reads, and the checks it runs on them.

runs.jsonl   one JSON object per line, one line per agent run (one attempt at one task)
tasks.json   the task suite: what a correct run must say and which tools it must or must not use
policy.json  optional: which tools the agent may call, hard limits, and when a human must approve

See DATA_CONTRACT.md for the full description.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class ContractError(ValueError):
    """Raised when an input file does not follow the data contract."""


@dataclass(frozen=True)
class Step:
    tool: str
    args: dict
    ok: bool = True
    approved_by: str | None = None
    blocked: bool = False  # stopped by a runtime guard before it ran


@dataclass(frozen=True)
class Run:
    run_id: str
    agent_version: str
    task_id: str
    trial: int
    steps: tuple[Step, ...]
    output: str
    cost_usd: float
    latency_s: float
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None


@dataclass(frozen=True)
class Expect:
    output_contains: tuple[str, ...] = ()
    output_excludes: tuple[str, ...] = ()
    tools_required: tuple[str, ...] = ()
    tools_forbidden: tuple[str, ...] = ()


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    prompt: str
    expect: Expect


@dataclass(frozen=True)
class Suite:
    name: str
    tasks: dict  # task id -> Task, in file order


@dataclass(frozen=True)
class ToolRule:
    allowed: bool = True
    max: dict | None = None             # argument -> hard limit, never allowed above it
    approval_above: dict | None = None  # argument -> limit above which approved_by is required


@dataclass(frozen=True)
class Policy:
    tools: dict  # tool name -> ToolRule


# ---------- helpers ----------

def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"), float("-inf"))


def _str_list(obj, key, where) -> tuple[str, ...]:
    v = obj.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ContractError(f"{where}: '{key}' must be a list of strings")
    return tuple(v)


def _num_map(obj, key, where) -> dict | None:
    v = obj.get(key)
    if v is None:
        return None
    if not isinstance(v, dict) or not all(_is_num(x) for x in v.values()):
        raise ContractError(f"{where}: '{key}' must map argument names to numbers")
    return dict(v)


def _load_json(text: str, name: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ContractError(f"{name} is not valid JSON (line {e.lineno}, column {e.colno}): {e.msg}") from None


# ---------- tasks.json ----------

def parse_suite(text: str, name: str = "tasks.json") -> Suite:
    data = _load_json(text, name)
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ContractError(f"{name} must be an object with a 'tasks' list")
    tasks: dict = {}
    for i, t in enumerate(data["tasks"], 1):
        where = f"{name}, task {i}"
        if not isinstance(t, dict):
            raise ContractError(f"{where}: must be an object")
        tid = t.get("id")
        if not isinstance(tid, str) or not tid:
            raise ContractError(f"{where}: 'id' must be a non-empty string")
        if tid in tasks:
            raise ContractError(f"{where}: duplicate task id '{tid}'")
        where = f"{name}, task '{tid}'"
        exp = t.get("expect", {})
        if not isinstance(exp, dict):
            raise ContractError(f"{where}: 'expect' must be an object")
        expect = Expect(
            output_contains=_str_list(exp, "output_contains", where),
            output_excludes=_str_list(exp, "output_excludes", where),
            tools_required=_str_list(exp, "tools_required", where),
            tools_forbidden=_str_list(exp, "tools_forbidden", where),
        )
        cat = t.get("category", "general")
        prompt = t.get("prompt", "")
        if not isinstance(cat, str) or not isinstance(prompt, str):
            raise ContractError(f"{where}: 'category' and 'prompt' must be strings")
        tasks[tid] = Task(tid, cat or "general", prompt, expect)
    if not tasks:
        raise ContractError(f"{name} has no tasks")
    suite_name = data.get("suite", "")
    return Suite(suite_name if isinstance(suite_name, str) else "", tasks)


# ---------- policy.json ----------

def parse_policy(text: str, name: str = "policy.json") -> Policy:
    data = _load_json(text, name)
    if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
        raise ContractError(f"{name} must be an object with a 'tools' object")
    rules = {}
    for tool, r in data["tools"].items():
        where = f"{name}, tool '{tool}'"
        if not isinstance(r, dict):
            raise ContractError(f"{where}: must be an object")
        allowed = r.get("allowed", True)
        if not isinstance(allowed, bool):
            raise ContractError(f"{where}: 'allowed' must be true or false")
        rules[tool] = ToolRule(allowed, _num_map(r, "max", where), _num_map(r, "approval_above", where))
    return Policy(rules)


# ---------- runs.jsonl ----------

def _parse_step(s, where) -> Step:
    if not isinstance(s, dict):
        raise ContractError(f"{where}: each step must be an object")
    tool = s.get("tool")
    if not isinstance(tool, str) or not tool:
        raise ContractError(f"{where}: each step needs a 'tool' name")
    args = s.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ContractError(f"{where}: 'args' of '{tool}' must be an object")
    ok = s.get("ok", True)
    blocked = s.get("blocked", False)
    if not isinstance(ok, bool) or not isinstance(blocked, bool):
        raise ContractError(f"{where}: 'ok' and 'blocked' must be true or false")
    approved = s.get("approved_by")
    if approved is not None and not isinstance(approved, str):
        raise ContractError(f"{where}: 'approved_by' must be a string or null")
    return Step(tool, args, ok, approved or None, blocked)


def parse_runs(text: str, suite: Suite, name: str = "runs.jsonl") -> list[Run]:
    runs: list[Run] = []
    seen: set[str] = set()
    unknown: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        where = f"{name}, line {lineno}"
        try:
            r = json.loads(line)
        except json.JSONDecodeError as e:
            raise ContractError(f"{where}: not valid JSON ({e.msg})") from None
        if not isinstance(r, dict):
            raise ContractError(f"{where}: must be a JSON object")
        for key in ("run_id", "agent_version", "task_id"):
            if not isinstance(r.get(key), str) or not r.get(key):
                raise ContractError(f"{where}: '{key}' must be a non-empty string")
        if r["run_id"] in seen:
            raise ContractError(f"{where}: duplicate run_id '{r['run_id']}'")
        seen.add(r["run_id"])
        if r["task_id"] not in suite.tasks:
            unknown.setdefault(r["task_id"], lineno)
            continue
        steps = r.get("steps", [])
        if not isinstance(steps, list):
            raise ContractError(f"{where}: 'steps' must be a list")
        output = r.get("output", "")
        if output is None:
            output = ""
        if not isinstance(output, str):
            raise ContractError(f"{where}: 'output' must be a string")
        for key in ("cost_usd", "latency_s"):
            if not _is_num(r.get(key)) or r[key] < 0:
                raise ContractError(f"{where}: '{key}' must be a number, 0 or more")
        trial = r.get("trial", 1)
        tin, tout = r.get("tokens_in", 0), r.get("tokens_out", 0)
        for key, v in (("trial", trial), ("tokens_in", tin), ("tokens_out", tout)):
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ContractError(f"{where}: '{key}' must be a whole number, 0 or more")
        err = r.get("error")
        if err is not None and not isinstance(err, str):
            raise ContractError(f"{where}: 'error' must be a string or null")
        runs.append(Run(r["run_id"], r["agent_version"], r["task_id"], trial,
                        tuple(_parse_step(s, where) for s in steps),
                        output, float(r["cost_usd"]), float(r["latency_s"]), tin, tout, err or None))
    if unknown:
        tid, ln = next(iter(unknown.items()))
        more = f" and {len(unknown) - 1} more" if len(unknown) > 1 else ""
        raise ContractError(f"{name}, line {ln}: task_id '{tid}'{more} not found in the task suite")
    if not runs:
        raise ContractError(f"{name} has no runs")
    return runs


# ---------- files ----------

def _read(path) -> str:
    return Path(path).read_text(encoding="utf-8-sig")


def resolve_paths(path, tasks=None, policy=None):
    """Accept a folder holding runs.jsonl, tasks.json and (optionally) policy.json, or explicit files."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    if p.is_dir():
        runs_p = p / "runs.jsonl"
        tasks_p = Path(tasks) if tasks else p / "tasks.json"
        pol = Path(policy) if policy else (p / "policy.json" if (p / "policy.json").exists() else None)
        for need in (runs_p, tasks_p):
            if not need.exists():
                raise FileNotFoundError(str(need))
        return runs_p, tasks_p, pol
    if not tasks:
        raise ContractError("give the task suite with --tasks tasks.json, or point to a folder that holds runs.jsonl and tasks.json")
    return p, Path(tasks), (Path(policy) if policy else None)


def load(path, tasks=None, policy=None):
    runs_p, tasks_p, pol_p = resolve_paths(path, tasks, policy)
    suite = parse_suite(_read(tasks_p), tasks_p.name)
    pol = parse_policy(_read(pol_p), pol_p.name) if pol_p else None
    runs = parse_runs(_read(runs_p), suite, runs_p.name)
    return runs, suite, pol
