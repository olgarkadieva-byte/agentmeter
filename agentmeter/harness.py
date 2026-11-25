"""Run a real model as a tool-using agent, and record every run in the AgentMeter data contract.

Pieces:
  AnthropicModel, OpenAIModel  model adapters (OpenAIModel also works with OpenAI-compatible endpoints)
  FunctionToolbox              tools written as Python functions
  MCPToolbox                   tools served by any MCP server over stdio (see agentmeter.mcp)
  ReplayToolbox                answers tool calls from recorded runs, so a new version can be tested
                               against the same tool outputs without touching real systems
  MemoryToolbox                adds save_note / search_notes tools that persist across runs
  run_agent, run_suite         the agent loop, with step limits, timeouts and the runtime Guard

Only the standard library is used. Model calls go over HTTPS with urllib.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .policy import Guard
from .recorder import Recorder


class ModelError(RuntimeError):
    pass


class StepLimitExceeded(RuntimeError):
    pass


class AgentTimeout(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ModelTurn:
    text: str
    tool_calls: list = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


def _http_json(url: str, headers: dict, body: dict, timeout: float = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise ModelError(f"HTTP {e.code} from {url}: {e.read().decode(errors='replace')[:500]}") from None
    except urllib.error.URLError as e:
        raise ModelError(f"could not reach {url}: {e.reason}") from None


class _Model:
    def __init__(self, model: str, api_key: str | None, env_var: str, price_in_per_mtok: float,
                 price_out_per_mtok: float, max_tokens: int, transport):
        if not model:
            raise ValueError("give a model name")
        self.model = model
        self.api_key = api_key or os.environ.get(env_var, "")
        if not self.api_key and transport is None:
            raise ValueError(f"set {env_var} or pass api_key")
        self.price_in = price_in_per_mtok / 1_000_000
        self.price_out = price_out_per_mtok / 1_000_000
        self.max_tokens = max_tokens
        self.transport = transport or _http_json

    def _cost(self, tin: int, tout: int) -> float:
        return round(tin * self.price_in + tout * self.price_out, 8)


class AnthropicModel(_Model):
    """Claude models through the Anthropic Messages API (tool use)."""

    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, model: str, api_key: str | None = None, price_in_per_mtok: float = 0.0,
                 price_out_per_mtok: float = 0.0, max_tokens: int = 1024, transport=None):
        super().__init__(model, api_key, "ANTHROPIC_API_KEY", price_in_per_mtok, price_out_per_mtok,
                         max_tokens, transport)

    @staticmethod
    def _messages(messages):
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks = [{"type": "text", "text": m["text"]}] if m.get("text") else []
                blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in m["tool_calls"]]
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"], "is_error": r["is_error"]}
                    for r in m["results"]]})
        return out

    def complete(self, system: str | None, messages: list, tools: list) -> ModelTurn:
        body = {"model": self.model, "max_tokens": self.max_tokens, "messages": self._messages(messages),
                "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
                          for t in tools]}
        if system:
            body["system"] = system
        r = self.transport(self.URL, {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}, body)
        text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
        calls = [ToolCall(b["id"], b["name"], b.get("input") or {}) for b in r.get("content", [])
                 if b.get("type") == "tool_use"]
        u = r.get("usage", {})
        tin, tout = int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
        return ModelTurn(text, calls, tin, tout, self._cost(tin, tout))


class OpenAIModel(_Model):
    """OpenAI chat completions with function calling. Set base_url for any OpenAI-compatible endpoint."""

    def __init__(self, model: str, api_key: str | None = None, price_in_per_mtok: float = 0.0,
                 price_out_per_mtok: float = 0.0, max_tokens: int = 1024, transport=None,
                 base_url: str = "https://api.openai.com/v1"):
        super().__init__(model, api_key, "OPENAI_API_KEY", price_in_per_mtok, price_out_per_mtok,
                         max_tokens, transport)
        self.url = base_url.rstrip("/") + "/chat/completions"

    @staticmethod
    def _messages(system, messages):
        out = [{"role": "system", "content": system}] if system else []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg = {"role": "assistant", "content": m.get("text") or None}
                if m["tool_calls"]:
                    msg["tool_calls"] = [{"id": c.id, "type": "function",
                                          "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                                         for c in m["tool_calls"]]
                out.append(msg)
            else:
                out += [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]} for r in m["results"]]
        return out

    def complete(self, system: str | None, messages: list, tools: list) -> ModelTurn:
        body = {"model": self.model, "max_tokens": self.max_tokens, "messages": self._messages(system, messages),
                "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": t["input_schema"]}} for t in tools]}
        r = self.transport(self.url, {"authorization": f"Bearer {self.api_key}"}, body)
        msg = r["choices"][0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_unparsed": c["function"].get("arguments")}
            calls.append(ToolCall(c["id"], c["function"]["name"], args if isinstance(args, dict) else {"value": args}))
        u = r.get("usage", {})
        tin, tout = int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        return ModelTurn(msg.get("content") or "", calls, tin, tout, self._cost(tin, tout))


# ---------- toolboxes ----------

class FunctionToolbox:
    """Tools written as Python functions: {name: (function, description, input_schema)}."""

    def __init__(self, tools: dict):
        self.tools = tools

    def specs(self) -> list:
        return [{"name": n, "description": d, "input_schema": s} for n, (_, d, s) in self.tools.items()]

    def call(self, name: str, args: dict):
        if name not in self.tools:
            return False, f"unknown tool: {name}"
        try:
            return True, self.tools[name][0](**args)
        except Exception as e:  # a tool error is reported to the model, not raised
            return False, f"{type(e).__name__}: {e}"


class ReplayToolbox:
    """Answers tool calls with results recorded in earlier runs (steps must include 'result')."""

    def __init__(self, runs, specs: list):
        self._specs = specs
        self.recorded = {}
        for r in runs:
            for s in r.steps:
                if s.result is not None and not s.blocked:
                    self.recorded.setdefault(self.key(s.tool, s.args), (s.ok, s.result))
        self.misses = 0

    @staticmethod
    def key(tool, args):
        return tool + " " + json.dumps(args, sort_keys=True)

    def specs(self):
        return self._specs

    def call(self, name, args):
        hit = self.recorded.get(self.key(name, args))
        if hit is None:
            self.misses += 1
            return False, "no recorded result for this call (replay mode)"
        return hit


class MemoryToolbox:
    """Wraps another toolbox and adds a notes memory that persists across runs in a JSON file."""

    SPECS = [
        {"name": "save_note", "description": "Save a short lesson or fact to long-term memory for future conversations.",
         "input_schema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}},
        {"name": "search_notes", "description": "Search long-term memory for notes relevant to the current request.",
         "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    ]

    def __init__(self, inner, path):
        self.inner = inner
        self.path = Path(path)
        self.notes = json.loads(self.path.read_text()) if self.path.exists() else []

    def specs(self):
        return self.inner.specs() + self.SPECS

    def call(self, name, args):
        if name == "save_note":
            note = str(args.get("note", "")).strip()
            if not note:
                return False, "empty note"
            if note not in self.notes:
                self.notes.append(note)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.notes, indent=1))
            return True, "saved"
        if name == "search_notes":
            words = set(re.findall(r"[a-z0-9$]+", str(args.get("query", "")).lower()))
            scored = sorted(((len(words & set(re.findall(r"[a-z0-9$]+", n.lower()))), i, n)
                             for i, n in enumerate(self.notes)), reverse=True)
            return True, [n for s, _, n in scored[:3] if s > 0]
        return self.inner.call(name, args)


# ---------- the agent loop ----------

def _as_text(result) -> str:
    return result if isinstance(result, str) else json.dumps(result)


def run_agent(model, toolbox, prompt: str, recorder: Recorder, guard: Guard | None = None,
              system: str | None = None, max_steps: int = 8, timeout_s: float = 120) -> str:
    """One run: call the model, execute its tool calls (through the guard), repeat until it answers."""
    specs = toolbox.specs()
    messages = [{"role": "user", "content": prompt}]
    t0 = time.monotonic()
    for _ in range(max_steps):
        if time.monotonic() - t0 > timeout_s:
            raise AgentTimeout(f"no final answer within {timeout_s:g}s")
        turn = model.complete(system, messages, specs)
        recorder.usage(turn.tokens_in, turn.tokens_out, turn.cost_usd)
        if not turn.tool_calls:
            recorder.final(turn.text)
            return turn.text
        messages.append({"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls})
        results = []
        for c in turn.tool_calls:
            d = guard.check(c.name, c.args) if guard else None
            if d is not None and not d.permitted:
                recorder.tool_call(c.name, c.args, ok=False, blocked=True)
                results.append({"id": c.id, "is_error": True,
                                "content": f"Blocked by policy: {d.reason}. Do not retry this call."})
                continue
            ok, out = toolbox.call(c.name, c.args)
            recorder.tool_call(c.name, c.args, ok=ok, result=out)
            results.append({"id": c.id, "is_error": not ok, "content": _as_text(out)})
        messages.append({"role": "tool", "results": results})
    raise StepLimitExceeded(f"no final answer after {max_steps} model calls")


def run_suite(model, toolbox, tasks: list, out_path, version: str, trials: int = 1, guard: Guard | None = None,
              system: str | None = None, agent_id: str | None = None, max_steps: int = 8,
              timeout_s: float = 120, progress=None) -> dict:
    """Run every task `trials` times and append the runs to out_path. Errors are recorded, not raised."""
    done = errors = 0
    for task in tasks:
        for trial in range(1, trials + 1):
            try:
                with Recorder(out_path, version, task["id"], trial, agent_id=agent_id,
                              run_id=f"{version}-{task['id']}-{trial}") as rec:
                    run_agent(model, toolbox, task.get("prompt", ""), rec, guard, system, max_steps, timeout_s)
            except Exception as e:  # recorded as the run's error by the Recorder; keep going
                errors += 1
                if progress:
                    progress(f"{task['id']} trial {trial}: {e}")
            done += 1
            if progress:
                progress(f"{done} runs")
    return {"runs": done, "errors": errors}
