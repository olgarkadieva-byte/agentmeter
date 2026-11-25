"""Model Context Protocol (MCP) over stdio: a small server and client, standard library only.

Server: expose any FunctionToolbox as an MCP server.
    python -m agentmeter.mcp            # serves the demo support tools (lookup_order, issue_refund, ...)

Client: use the tools of any MCP server (yours, or a third-party one) in the AgentMeter harness.
    with MCPToolbox(["python", "-m", "agentmeter.mcp"]) as tools:
        run_agent(model, tools, prompt, recorder, guard)

Messages are JSON-RPC 2.0, one per line, as in the MCP stdio transport. Supported methods:
initialize, ping, tools/list and tools/call.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class MCPError(RuntimeError):
    pass


# ---------- server ----------

def _reply(out, id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    out.write(json.dumps(msg) + "\n")
    out.flush()


def serve(toolbox, name: str = "agentmeter-tools", version: str = "1.1.0", stdin=None, stdout=None):
    """Serve a toolbox over stdio until stdin closes."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _reply(stdout, None, error={"code": -32700, "message": "parse error"})
            continue
        if not isinstance(msg, dict) or "method" not in msg:
            continue
        if "id" not in msg:  # a notification, such as notifications/initialized
            continue
        method, params, id_ = msg["method"], msg.get("params") or {}, msg["id"]
        if method == "initialize":
            asked = params.get("protocolVersion")
            _reply(stdout, id_, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": name, "version": version}})
        elif method == "ping":
            _reply(stdout, id_, {})
        elif method == "tools/list":
            _reply(stdout, id_, {"tools": [{"name": t["name"], "description": t["description"],
                                            "inputSchema": t["input_schema"]} for t in toolbox.specs()]})
        elif method == "tools/call":
            ok, out = toolbox.call(params.get("name", ""), params.get("arguments") or {})
            text = out if isinstance(out, str) else json.dumps(out)
            _reply(stdout, id_, {"content": [{"type": "text", "text": text}], "isError": not ok})
        else:
            _reply(stdout, id_, error={"code": -32601, "message": f"method not found: {method}"})


# ---------- client ----------

class MCPToolbox:
    """Tools from an MCP server started as a subprocess, usable wherever a toolbox is expected."""

    def __init__(self, command: list, env: dict | None = None, timeout_s: float = 30):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, env=env, bufsize=1)
        self.timeout_s = timeout_s
        self._id = 0
        init = self._request("initialize", {"protocolVersion": PROTOCOL_VERSIONS[0], "capabilities": {},
                                            "clientInfo": {"name": "agentmeter", "version": "1.1.0"}})
        self.server_info = init.get("serverInfo", {})
        self.protocol_version = init.get("protocolVersion")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._specs = [{"name": t["name"], "description": t.get("description", ""),
                        "input_schema": t.get("inputSchema") or {"type": "object"}}
                       for t in self._request("tools/list", {}).get("tools", [])]

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _readline(self):
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("line", self.proc.stdout.readline()), daemon=True)
        t.start()
        t.join(self.timeout_s)
        if "line" not in box:
            raise MCPError(f"MCP server did not answer within {self.timeout_s:g}s")
        if not box["line"]:
            raise MCPError("MCP server closed the connection")
        return box["line"]

    def _request(self, method, params):
        self._id += 1
        my_id = self._id
        self._send({"jsonrpc": "2.0", "id": my_id, "method": method, "params": params})
        while True:
            msg = json.loads(self._readline())
            if msg.get("id") != my_id:
                continue  # skip server notifications and requests we don't handle
            if "error" in msg:
                raise MCPError(f"{method}: {msg['error'].get('message')}")
            return msg.get("result") or {}

    def specs(self):
        return self._specs

    def call(self, name, args):
        try:
            r = self._request("tools/call", {"name": name, "arguments": args})
        except MCPError as e:
            return False, str(e)
        text = "".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text")
        try:
            out = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            out = text
        return not r.get("isError", False), out

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


if __name__ == "__main__":
    from .demo import support_toolbox
    serve(support_toolbox(), name="agentmeter-demo-support-tools")
