"""Record agent runs in the AgentMeter data contract, from any agent framework.

    from agentmeter import Recorder

    with Recorder("runs.jsonl", agent_version="v2.1", task_id="T07", trial=1) as run:
        order = lookup_order("A1007")                        # your own tool
        run.tool_call("lookup_order", {"order_id": "A1007"}, ok=True)
        run.usage(tokens_in=1840, tokens_out=210, cost_usd=0.0087)
        run.final("Your order A1007 has shipped.")

Each `with` block appends one line to runs.jsonl. Latency is measured for you unless you
pass latency_s to usage(). If the block raises, the run is still written, with the error.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


class Recorder:
    def __init__(self, path, agent_version: str, task_id: str, trial: int = 1, run_id: str | None = None):
        self.path = Path(path)
        self.record = {
            "run_id": run_id or uuid.uuid4().hex[:12],
            "agent_version": agent_version,
            "task_id": task_id,
            "trial": trial,
            "steps": [],
            "output": "",
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "latency_s": 0.0,
        }
        self._latency_given = False

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def tool_call(self, tool: str, args: dict | None = None, ok: bool = True,
                  approved_by: str | None = None, blocked: bool = False):
        step = {"tool": tool, "args": args or {}, "ok": ok}
        if approved_by:
            step["approved_by"] = approved_by
        if blocked:
            step["blocked"] = True
        self.record["steps"].append(step)

    def usage(self, tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0,
              latency_s: float | None = None):
        """Add model usage. Call once per model call, or once with totals."""
        self.record["tokens_in"] += int(tokens_in)
        self.record["tokens_out"] += int(tokens_out)
        self.record["cost_usd"] = round(self.record["cost_usd"] + float(cost_usd), 8)
        if latency_s is not None:
            self.record["latency_s"] = round(float(latency_s), 4)
            self._latency_given = True

    def final(self, output: str):
        self.record["output"] = output

    def __exit__(self, exc_type, exc, tb):
        if not self._latency_given:
            self.record["latency_s"] = round(time.perf_counter() - self._t0, 4)
        if exc is not None:
            self.record["error"] = f"{exc_type.__name__}: {exc}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.record, separators=(",", ":")) + "\n")
        return False  # never swallow the agent's exception
