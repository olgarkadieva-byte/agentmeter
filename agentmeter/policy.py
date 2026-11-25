"""Permission checks for tool calls.

The same rules run in two places:
- at runtime, through Guard, to stop a call before it runs;
- after the fact, through audit(), to check every call an agent made.
"""

from __future__ import annotations

from dataclasses import dataclass

from .contract import Policy, Run, Step

ALLOWED, APPROVED, BLOCKED, VIOLATION, UNCHECKED = "allowed", "approved", "blocked", "violation", "unchecked"


@dataclass(frozen=True)
class Decision:
    kind: str     # allowed | approved | blocked | violation | unchecked
    reason: str = ""

    @property
    def permitted(self) -> bool:
        return self.kind in (ALLOWED, APPROVED, UNCHECKED)


def _fmt(v) -> str:
    # shortest round-trip form, the same as JavaScript's String(number)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check(policy: Policy | None, tool: str, args: dict, approved_by: str | None = None,
          agent_id: str | None = None) -> Decision:
    """Decide whether one tool call is permitted under the policy."""
    if policy is None:
        return Decision(UNCHECKED)
    if agent_id is not None and policy.agents is not None:
        scope = policy.agents.get(agent_id)
        if scope is None:
            return Decision(VIOLATION, f"{tool}: agent '{agent_id}' is not in the policy")
        if tool not in scope:
            return Decision(VIOLATION, f"{tool}: agent '{agent_id}' is not permitted to use this tool")
    rule = policy.tools.get(tool)
    if rule is None:
        return Decision(VIOLATION, f"{tool}: tool is not in the policy")
    if not rule.allowed:
        return Decision(VIOLATION, f"{tool}: tool is not allowed")
    for arg, limit in (rule.max or {}).items():
        v = args.get(arg)
        if _is_num(v) and v > limit:
            return Decision(VIOLATION, f"{tool}: {arg} {_fmt(v)} is over the hard limit of {_fmt(limit)}")
    needs = []
    for arg, limit in (rule.approval_above or {}).items():
        v = args.get(arg)
        if _is_num(v) and v > limit:
            needs.append(f"{arg} {_fmt(v)} is over {_fmt(limit)}")
    if needs:
        if approved_by:
            return Decision(APPROVED, f"{tool}: {'; '.join(needs)}, approved by {approved_by}")
        return Decision(VIOLATION, f"{tool}: {'; '.join(needs)} without approval")
    return Decision(ALLOWED)


def check_step(policy: Policy | None, step: Step, agent_id: str | None = None) -> Decision:
    d = check(policy, step.tool, step.args, step.approved_by, agent_id)
    if step.blocked:
        # The guard stopped it, so it never ran: record what it would have been.
        return Decision(BLOCKED, d.reason or f"{step.tool}: stopped by the runtime guard")
    return d


class Guard:
    """Runtime permission layer: call before executing a tool.

        guard = Guard(policy)
        decision = guard.check("issue_refund", {"amount": 250})
        if not decision.permitted:
            ...  # don't run it; escalate or ask for approval instead
    """

    def __init__(self, policy: Policy, agent_id: str | None = None):
        self.policy = policy
        self.agent_id = agent_id  # the identity this guard enforces, if the policy scopes agents

    def check(self, tool: str, args: dict, approved_by: str | None = None) -> Decision:
        return check(self.policy, tool, args, approved_by, self.agent_id)


@dataclass(frozen=True)
class AuditEntry:
    run_id: str
    agent_version: str
    task_id: str
    step: int
    tool: str
    args: dict
    decision: str
    reason: str


def audit(runs: list[Run], policy: Policy | None) -> list[AuditEntry]:
    """Every tool call, with the policy decision for it."""
    out = []
    for r in runs:
        for i, s in enumerate(r.steps, 1):
            d = check_step(policy, s, r.agent_id)
            out.append(AuditEntry(r.run_id, r.agent_version, r.task_id, i, s.tool, s.args, d.kind, d.reason))
    return out
