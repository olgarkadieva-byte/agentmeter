"""Graders, per-version scorecards and the release gate.

Statistics notes
- Trials of the same task are not independent, so success-rate ranges are clustered by task:
  the mean of per-task success rates, with a standard error from the spread across tasks.
- The release gate compares two versions on the tasks both ran (a paired comparison), which
  removes most of the noise that comes from some tasks simply being harder than others.
- pass^k is the chance that all k attempts at a task succeed (k = the smallest number of trials
  any task has). It measures reliability: an agent that succeeds 80% of the time on every task
  is very different from one that always solves 80% of tasks and never the rest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .contract import Policy, Run, Suite
from .policy import VIOLATION, check_step

Z95 = 1.959963984540054

FAIL_AGENT_ERROR = "agent error"
FAIL_FORBIDDEN = "forbidden tool called"
FAIL_MISSING = "required tool not called"
FAIL_ANSWER = "wrong or incomplete answer"
FAIL_JUDGE = "failed the judge's rubric"
FAILURE_ORDER = (FAIL_AGENT_ERROR, FAIL_FORBIDDEN, FAIL_MISSING, FAIL_ANSWER, FAIL_JUDGE)


@dataclass(frozen=True)
class Interval:
    estimate: float
    low: float
    high: float

    def to_dict(self):
        return {"estimate": self.estimate, "low": self.low, "high": self.high}


@dataclass(frozen=True)
class RunGrade:
    run: Run
    success: bool
    failure: str | None
    tools_ok: bool
    violations: tuple[str, ...]
    tool_calls: int
    tool_errors: int
    unjudged: bool = False  # the task has a rubric but no judge verdict was recorded


def grade_run(run: Run, suite: Suite, policy: Policy | None) -> RunGrade:
    exp = suite.tasks[run.task_id].expect
    out = run.output.lower()
    answer_ok = all(s.lower() in out for s in exp.output_contains) and not any(
        s.lower() in out for s in exp.output_excludes)
    executed = [s for s in run.steps if not s.blocked]
    succeeded = {s.tool for s in executed if s.ok}
    called = {s.tool for s in executed}
    missing = [t for t in exp.tools_required if t not in succeeded]
    forbidden = [t for t in exp.tools_forbidden if t in called]
    tools_ok = not missing and not forbidden
    if run.error:
        failure = FAIL_AGENT_ERROR
    elif forbidden:
        failure = FAIL_FORBIDDEN
    elif missing:
        failure = FAIL_MISSING
    elif not answer_ok:
        failure = FAIL_ANSWER
    elif exp.rubric and run.judge_pass is False:
        failure = FAIL_JUDGE
    else:
        failure = None
    violations = []
    for s in run.steps:
        d = check_step(policy, s, run.agent_id)
        if d.kind == VIOLATION:
            violations.append(d.reason)
    return RunGrade(run, failure is None, failure, tools_ok, tuple(violations),
                    len(executed), sum(1 for s in executed if not s.ok),
                    bool(exp.rubric) and run.judge_pass is None)


def _mean(xs):
    return sum(xs) / len(xs)


def _clustered(rates: list[float]) -> Interval:
    m = _mean(rates)
    n = len(rates)
    if n < 2:
        return Interval(m, m, m)
    sd = math.sqrt(sum((r - m) ** 2 for r in rates) / (n - 1))
    se = sd / math.sqrt(n)
    return Interval(m, max(0.0, m - Z95 * se), min(1.0, m + Z95 * se))


def _percentile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    idx = max(0, math.ceil(q * len(s)) - 1)
    return s[idx]


def _pass_k(c: int, n: int, k: int) -> float:
    # chance that k attempts drawn from the n recorded ones all succeeded
    return math.comb(c, k) / math.comb(n, k)


@dataclass
class VersionScore:
    version: str
    runs: int
    tasks: int
    k: int
    success: Interval
    pass_k: float
    tool_accuracy: float
    tool_error_rate: float | None
    violations: int
    violation_run_share: float
    violation_examples: list
    blocked_calls: int
    unjudged_runs: int
    cost_per_run: float
    cost_per_success: float | None
    total_cost: float
    tokens_in: int
    tokens_out: int
    latency_p50: float
    latency_p95: float
    avg_steps: float
    by_category: dict = field(default_factory=dict)
    failures: dict = field(default_factory=dict)
    task_rates: dict = field(default_factory=dict)

    def to_dict(self):
        d = {k: v for k, v in self.__dict__.items()}
        d["success"] = self.success.to_dict()
        return d


def versions_in(runs: list[Run]) -> list[str]:
    seen = []
    for r in runs:
        if r.agent_version not in seen:
            seen.append(r.agent_version)
    return seen


def score_version(version: str, grades: list[RunGrade], suite: Suite) -> VersionScore:
    gs = [g for g in grades if g.run.agent_version == version]
    if not gs:
        raise ValueError(f"no runs for version '{version}'")
    per_task: dict[str, list[bool]] = {}
    for tid in suite.tasks:
        per_task[tid] = []
    for g in gs:
        per_task[g.run.task_id].append(g.success)
    per_task = {t: v for t, v in per_task.items() if v}
    task_rates = {t: sum(v) / len(v) for t, v in per_task.items()}
    k = min(len(v) for v in per_task.values())
    pass_k = _mean([_pass_k(sum(v), len(v), k) for v in per_task.values()])

    by_cat: dict[str, list[float]] = {}
    for t, rate in task_rates.items():
        by_cat.setdefault(suite.tasks[t].category, []).append(rate)
    by_category = {c: _mean(v) for c, v in by_cat.items()}

    failures = {f: 0 for f in FAILURE_ORDER}
    for g in gs:
        if g.failure:
            failures[g.failure] += 1
    failures = {f: n for f, n in failures.items() if n}

    calls = sum(g.tool_calls for g in gs)
    successes = sum(1 for g in gs if g.success)
    total_cost = sum(g.run.cost_usd for g in gs)
    viol = [v for g in gs for v in g.violations]
    examples = []
    for v in viol:
        if v not in examples:
            examples.append(v)
        if len(examples) == 3:
            break
    lat = [g.run.latency_s for g in gs]
    return VersionScore(
        version=version,
        runs=len(gs),
        tasks=len(per_task),
        k=k,
        success=_clustered(list(task_rates.values())),
        pass_k=pass_k,
        tool_accuracy=sum(1 for g in gs if g.tools_ok) / len(gs),
        tool_error_rate=(sum(g.tool_errors for g in gs) / calls) if calls else None,
        violations=len(viol),
        violation_run_share=sum(1 for g in gs if g.violations) / len(gs),
        violation_examples=examples,
        blocked_calls=sum(1 for g in gs for st in g.run.steps if st.blocked),
        unjudged_runs=sum(1 for g in gs if g.unjudged),
        cost_per_run=total_cost / len(gs),
        cost_per_success=(total_cost / successes) if successes else None,
        total_cost=total_cost,
        tokens_in=sum(g.run.tokens_in for g in gs),
        tokens_out=sum(g.run.tokens_out for g in gs),
        latency_p50=_percentile(lat, 0.5),
        latency_p95=_percentile(lat, 0.95),
        avg_steps=sum(len(g.run.steps) for g in gs) / len(gs),
        by_category=by_category,
        failures=failures,
        task_rates=task_rates,
    )


def score(runs: list[Run], suite: Suite, policy: Policy | None) -> list[VersionScore]:
    grades = [grade_run(r, suite, policy) for r in runs]
    return [score_version(v, grades, suite) for v in versions_in(runs)]


# ---------- release gate ----------

@dataclass(frozen=True)
class Thresholds:
    max_success_drop: float = 0.05      # non-inferiority margin, in success-rate points (0.05 = 5 points)
    max_cost_increase: float = 0.20     # cost per successful run, relative
    max_latency_increase: float = 0.25  # p95 latency, relative


@dataclass
class GateResult:
    baseline: str
    candidate: str
    common_tasks: int
    success_diff: Interval
    cost_change: float | None
    latency_change: float | None
    candidate_violations: int
    baseline_violations: int
    verdict: str  # ship | hold | block
    reasons: list

    def to_dict(self):
        d = dict(self.__dict__)
        d["success_diff"] = self.success_diff.to_dict()
        return d


def _pts(x: float) -> str:
    return f"{x * 100:+.1f}"


def _pct(x: float) -> str:
    return f"{x * 100:+.0f}%"


def gate(base: VersionScore, cand: VersionScore, t: Thresholds = Thresholds()) -> GateResult:
    common = [tid for tid in base.task_rates if tid in cand.task_rates]
    if not common:
        raise ValueError(f"'{base.version}' and '{cand.version}' have no tasks in common")
    # paired by task; not clipped, because a difference can be negative
    diffs = [cand.task_rates[tid] - base.task_rates[tid] for tid in common]
    m = _mean(diffs)
    if len(diffs) > 1:
        se = math.sqrt(sum((d - m) ** 2 for d in diffs) / (len(diffs) - 1)) / math.sqrt(len(diffs))
    else:
        se = 0.0
    diff = Interval(m, m - Z95 * se, m + Z95 * se)

    cost_change = None
    if base.cost_per_success and cand.cost_per_success is not None:
        cost_change = cand.cost_per_success / base.cost_per_success - 1
    latency_change = (cand.latency_p95 / base.latency_p95 - 1) if base.latency_p95 > 0 else None

    block, hold = [], []
    if cand.violations:
        ex = f" (for example: {cand.violation_examples[0]})" if cand.violation_examples else ""
        block.append(f"{cand.violations} policy violation{'s' if cand.violations != 1 else ''} in "
                     f"{cand.violation_run_share * 100:.1f}% of runs{ex}.")
    rng = f"95% range {_pts(diff.low)} to {_pts(diff.high)}"
    if diff.high < 0:
        block.append(f"Success is significantly lower: {_pts(diff.estimate)} points ({rng}).")
    elif diff.low < -t.max_success_drop:
        hold.append(f"A success drop of more than {t.max_success_drop * 100:g} points can't be ruled out "
                    f"({_pts(diff.estimate)} points, {rng}). Add tasks or trials to narrow it.")
    if cand.cost_per_success is None:
        block.append("No run succeeded.")
    elif cost_change is not None and cost_change > t.max_cost_increase:
        hold.append(f"Cost per successful run is up {cost_change * 100:.0f}% (${base.cost_per_success:.4f} to "
                    f"${cand.cost_per_success:.4f}), over the {t.max_cost_increase * 100:g}% budget.")
    if cand.unjudged_runs:
        hold.append(f"{cand.unjudged_runs} run{'s' if cand.unjudged_runs != 1 else ''} with a rubric "
                    f"{'have' if cand.unjudged_runs != 1 else 'has'} no judge verdict yet; run agentmeter judge first.")
    if latency_change is not None and latency_change > t.max_latency_increase:
        hold.append(f"p95 latency is up {latency_change * 100:.0f}% ({base.latency_p95:.1f}s to "
                    f"{cand.latency_p95:.1f}s), over the {t.max_latency_increase * 100:g}% budget.")

    if block:
        verdict, reasons = "block", block + hold
    elif hold:
        verdict, reasons = "hold", hold
    else:
        verdict = "ship"
        reasons = [f"Success {_pts(diff.estimate)} points ({rng}), within the "
                   f"{t.max_success_drop * 100:g}-point margin.", "No policy violations."]
        if cost_change is not None:
            reasons.append(f"Cost per successful run {_pct(cost_change)}.")
        if latency_change is not None:
            reasons.append(f"p95 latency {_pct(latency_change)}.")
    if not cand.violations and base.violations:
        reasons.append(f"Fixes the baseline's {base.violations} policy violation"
                       f"{'s' if base.violations != 1 else ''}.")
    if diff.low > 0:
        reasons.append(f"Success is significantly higher: {_pts(diff.estimate)} points ({rng}).")
    return GateResult(base.version, cand.version, len(common), diff, cost_change, latency_change,
                      cand.violations, base.violations, verdict, reasons)


def gate_all(scores: list[VersionScore], baseline: str | None = None,
             t: Thresholds = Thresholds()) -> list[GateResult]:
    if not scores:
        return []
    by = {s.version: s for s in scores}
    base = by[baseline] if baseline else scores[0]
    return [gate(base, s, t) for s in scores if s.version != base.version]
