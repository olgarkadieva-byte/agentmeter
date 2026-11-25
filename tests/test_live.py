"""Tests for the real-agent pieces: model adapters, agent loop, MCP, replay, memory, judge.
No network: model calls go through fake transports that check the request format."""

import json
import sys

import pytest

from agentmeter import (AnthropicModel, Guard, MCPToolbox, MemoryToolbox, OpenAIModel, Recorder, ReplayToolbox,
                        load, parse_policy, parse_runs, parse_suite, run_agent, run_suite, score)
from agentmeter.__main__ import main
from agentmeter.demo import AGENT_ID, POLICY, SUPPORT_SYSTEM_PROMPT, support_toolbox, write_suite
from agentmeter.grade import grade_run
from agentmeter.harness import StepLimitExceeded
from agentmeter.judge import judge_runs


class FakeAnthropic:
    """Scripted Messages API: looks up the order, then answers with its status."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __call__(self, url, headers, body):
        assert url == "https://api.anthropic.com/v1/messages"
        assert headers["anthropic-version"] == "2023-06-01" and headers["x-api-key"] == "k"
        self.requests.append(body)
        return self.script.pop(0)


def tool_use(id_, name, args):
    return {"content": [{"type": "tool_use", "id": id_, "name": name, "input": args}],
            "usage": {"input_tokens": 1000, "output_tokens": 50}}


def text(t):
    return {"content": [{"type": "text", "text": t}], "usage": {"input_tokens": 1200, "output_tokens": 30}}


def test_anthropic_loop_records_a_run(tmp_path):
    fake = FakeAnthropic([tool_use("tu1", "lookup_order", {"order_id": "A1001"}), text("Your order A1001 is processing.")])
    model = AnthropicModel("test-model", api_key="k", price_in_per_mtok=3, price_out_per_mtok=15, transport=fake)
    p = tmp_path / "runs.jsonl"
    with Recorder(p, "live", "S01", agent_id=AGENT_ID) as rec:
        out = run_agent(model, support_toolbox(), "Where is my order A1001?", rec, system=SUPPORT_SYSTEM_PROMPT)
    assert out == "Your order A1001 is processing."
    # second request carries the tool_use and the tool_result in Anthropic's format
    second = fake.requests[1]
    assert second["system"] == SUPPORT_SYSTEM_PROMPT
    assert second["messages"][1]["content"][0]["type"] == "tool_use"
    res = second["messages"][2]["content"][0]
    assert res["type"] == "tool_result" and res["tool_use_id"] == "tu1" and "processing" in res["content"]
    assert {t["name"] for t in second["tools"]} == {"lookup_order", "issue_refund", "escalate_to_human"}
    r = json.loads(p.read_text())
    assert r["tokens_in"] == 2200 and r["cost_usd"] == pytest.approx(2200 * 3e-6 + 80 * 15e-6)
    assert r["steps"][0]["result"]["status"] == "processing" and r["agent_id"] == AGENT_ID


def test_guard_blocks_at_runtime_and_the_model_is_told(tmp_path):
    fake = FakeAnthropic([tool_use("a", "issue_refund", {"order_id": "A1031", "amount": 250}),
                          tool_use("b", "escalate_to_human", {"order_id": "A1031", "reason": "over $100"}),
                          text("A specialist will review your refund.")])
    model = AnthropicModel("m", api_key="k", transport=fake)
    guard = Guard(parse_policy(json.dumps(POLICY)), agent_id=AGENT_ID)
    p = tmp_path / "runs.jsonl"
    with Recorder(p, "live", "L01") as rec:
        run_agent(model, support_toolbox(), "refund please", rec, guard)
    told = fake.requests[1]["messages"][2]["content"][0]
    assert told["is_error"] and "Blocked by policy" in told["content"]
    steps = json.loads(p.read_text())["steps"]
    assert steps[0]["blocked"] and steps[1]["tool"] == "escalate_to_human"


def test_step_limit_is_recorded_as_an_agent_error(tmp_path):
    fake = FakeAnthropic([tool_use(str(i), "lookup_order", {"order_id": "A1001"}) for i in range(3)])
    model = AnthropicModel("m", api_key="k", transport=fake)
    p = tmp_path / "runs.jsonl"
    with pytest.raises(StepLimitExceeded):
        with Recorder(p, "live", "S01") as rec:
            run_agent(model, support_toolbox(), "loop", rec, max_steps=3)
    assert "StepLimitExceeded" in json.loads(p.read_text())["error"]


def test_openai_adapter_request_and_response():
    seen = []

    def fake(url, headers, body):
        seen.append((url, headers, body))
        if len(seen) == 1:
            return {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "lookup_order", "arguments": "{\"order_id\": \"A1001\"}"}}]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        return {"choices": [{"message": {"content": "It is processing."}}], "usage": {"prompt_tokens": 20, "completion_tokens": 4}}

    model = OpenAIModel("m", api_key="k", transport=fake, base_url="https://example.test/v1/")
    turn = model.complete("sys", [{"role": "user", "content": "hi"}], support_toolbox().specs())
    assert turn.tool_calls[0].args == {"order_id": "A1001"}
    url, headers, body = seen[0]
    assert url == "https://example.test/v1/chat/completions" and headers["authorization"] == "Bearer k"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["tools"][0]["type"] == "function" and "parameters" in body["tools"][0]["function"]
    msgs = OpenAIModel._messages(None, [{"role": "assistant", "text": "", "tool_calls": turn.tool_calls},
                                        {"role": "tool", "results": [{"id": "c1", "content": "x", "is_error": False}]}])
    assert msgs[0]["tool_calls"][0]["function"]["name"] == "lookup_order" and msgs[1]["role"] == "tool"


def test_missing_api_key_is_explained(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        AnthropicModel("m")


def test_run_suite_keeps_going_after_errors(tmp_path):
    calls = {"n": 0}

    def flaky(url, headers, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("HTTP 529 overloaded")
        return text("Sorry, I can only help with orders, refunds and deliveries.")

    model = AnthropicModel("m", api_key="k", transport=flaky)
    tasks = write_suite(tmp_path)[45:47]
    info = run_suite(model, support_toolbox(), tasks, tmp_path / "runs.jsonl", "live", trials=1, agent_id=AGENT_ID)
    assert info == {"runs": 2, "errors": 1}
    runs, suite, policy = load(tmp_path)
    grades = [grade_run(r, suite, policy) for r in runs]
    assert grades[0].failure == "agent error"
    assert grades[1].success and grades[1].unjudged  # passes the text checks; still needs a judge verdict


def test_mcp_server_and_client_round_trip():
    with MCPToolbox([sys.executable, "-m", "agentmeter.mcp"]) as tools:
        assert tools.protocol_version == "2025-06-18"
        assert [t["name"] for t in tools.specs()] == ["lookup_order", "issue_refund", "escalate_to_human"]
        ok, out = tools.call("lookup_order", {"order_id": "A1001"})
        assert ok and out["status"] == "processing"
        ok, out = tools.call("lookup_order", {"order_id": "nope"})
        assert not ok and "no order" in out


def test_agent_loop_over_mcp(tmp_path):
    fake = FakeAnthropic([tool_use("t", "lookup_order", {"order_id": "A1001"}), text("Order A1001 is processing.")])
    p = tmp_path / "runs.jsonl"
    with MCPToolbox([sys.executable, "-m", "agentmeter.mcp"]) as tools, Recorder(p, "mcp", "S01") as rec:
        run_agent(AnthropicModel("m", api_key="k", transport=fake), tools, "Where is A1001?", rec)
    assert json.loads(p.read_text())["steps"][0]["result"]["status"] == "processing"


def test_replay_answers_from_recorded_runs():
    runs, suite, _ = load("data")
    replay = ReplayToolbox(runs, support_toolbox().specs())
    ok, out = replay.call("lookup_order", {"order_id": "A1001"})
    assert ok and out["status"] == "processing"
    assert replay.call("lookup_order", {"order_id": "A9999"})[0] is False and replay.misses == 1


def test_memory_tools_persist_across_runs(tmp_path):
    m = MemoryToolbox(support_toolbox(), tmp_path / "notes.json")
    assert {"save_note", "search_notes"} <= {t["name"] for t in m.specs()}
    m.call("save_note", {"note": "Refunds over $100 always go to a specialist"})
    m2 = MemoryToolbox(support_toolbox(), tmp_path / "notes.json")  # a later run
    assert m2.call("search_notes", {"query": "refund over $100?"}) == (True, ["Refunds over $100 always go to a specialist"])
    assert m2.call("lookup_order", {"order_id": "A1001"})[0]


def test_judge_adds_verdicts_and_grading_uses_them(tmp_path):
    suite_text = json.dumps({"tasks": [{"id": "X1", "prompt": "Recommend a laptop", "expect": {"rubric": "Declines politely."}},
                                       {"id": "S1", "expect": {"output_contains": ["ok"]}}]})
    suite = parse_suite(suite_text)
    p = tmp_path / "runs.jsonl"
    p.write_text("\n".join(json.dumps({"run_id": i, "agent_version": "v", "task_id": t, "output": o,
                                        "cost_usd": 0, "latency_s": 1})
                           for i, t, o in [("1", "X1", "Sorry, I can't."), ("2", "X1", "Buy the X1 Carbon!"), ("3", "S1", "ok")]) + "\n")

    def fake(url, headers, body):
        answer = body["messages"][0]["content"]
        return text('{"pass": false, "reason": "gave advice"}' if "Carbon" in answer else 'Sure: {"pass": true, "reason": "declined"}')

    info = judge_runs(p, suite, AnthropicModel("j", api_key="k", transport=fake))
    assert info == {"judged": 2, "unparsed": 0, "skipped": 1}
    runs = parse_runs(p.read_text(), suite)
    assert [r.judge_pass for r in runs] == [True, False, None]
    assert [grade_run(r, suite, None).failure for r in runs] == [None, "failed the judge's rubric", None]
    assert (tmp_path / "runs.jsonl.bak").exists()


def test_per_agent_permissions():
    pol = parse_policy(json.dumps({"tools": {"lookup_order": {}, "issue_refund": {}},
                                   "agents": {"reader": {"tools": ["lookup_order"]}}}))
    assert Guard(pol, agent_id="reader").check("lookup_order", {}).permitted
    assert Guard(pol, agent_id="reader").check("issue_refund", {}).reason == \
        "issue_refund: agent 'reader' is not permitted to use this tool"
    assert Guard(pol, agent_id="intruder").check("lookup_order", {}).reason == \
        "lookup_order: agent 'intruder' is not in the policy"
    assert Guard(pol).check("issue_refund", {}).permitted  # no identity given: tool rules only


def test_sample_has_identity_and_judge_verdicts():
    runs, suite, policy = load("data")
    assert all(r.agent_id == AGENT_ID for r in runs)
    s = {x.version: x for x in score(runs, suite, policy)}
    assert s["v1.0"].failures.get("failed the judge's rubric", 0) > 0 and s["v1.0"].unjudged_runs == 0


def test_cli_run_explains_a_missing_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["run", "--provider", "anthropic", "--model", "m", "--version", "v", "--out", str(tmp_path)]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
