"""How to evaluate your own agent: run it on the task suite, record each run, then score it.

Replace `my_agent` with your real agent loop (any framework or model API). The two pieces to keep:
  - Guard.check() before every tool call, so risky actions are stopped at runtime;
  - Recorder, so every run is saved in the AgentMeter data contract.

Run from the repository root:
    python examples/record_your_agent.py
    python -m agentmeter score example-output/runs.jsonl --tasks data/tasks.json --policy data/policy.json
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentmeter import Guard, Recorder, parse_policy  # noqa: E402

ORDERS = {"A1001": {"status": "shipped"}}  # stand-in for your real systems


def lookup_order(order_id):
    return ORDERS.get(order_id, {"status": "processing"})


def my_agent(prompt, guard, run):
    """A tiny rule-based stand-in. Swap in your model call and tool loop here."""
    m = re.search(r"\bA\d{4}\b", prompt)
    if not m:
        return "Sorry, I can only help with orders, refunds and deliveries."
    args = {"order_id": m.group(0)}
    decision = guard.check("lookup_order", args)
    if not decision.permitted:
        run.tool_call("lookup_order", args, ok=False, blocked=True)
        return "I can't look that up right now."
    order = lookup_order(**args)
    run.tool_call("lookup_order", args, ok=True)
    run.usage(tokens_in=900, tokens_out=60, cost_usd=0.0036)  # take these from your model's response
    return f"Your order {args['order_id']} is {order['status']}."


def main():
    root = Path(__file__).resolve().parents[1]
    tasks = json.loads((root / "data" / "tasks.json").read_text())["tasks"]
    guard = Guard(parse_policy((root / "data" / "policy.json").read_text()))
    out = root / "example-output" / "runs.jsonl"
    if out.exists():
        out.unlink()
    for task in tasks[:15] + tasks[45:]:  # order-status and off-topic tasks
        for trial in (1, 2):
            with Recorder(out, agent_version="my-agent-0.1", task_id=task["id"], trial=trial) as run:
                run.final(my_agent(task["prompt"], guard, run))
    print(f"Recorded {2 * 30} runs to {out.relative_to(root)}")


if __name__ == "__main__":
    main()
