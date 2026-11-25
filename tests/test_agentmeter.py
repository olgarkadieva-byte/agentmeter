import hashlib
import json
import math
from pathlib import Path

import pytest

from agentmeter import ContractError, Guard, Recorder, Thresholds, audit, gate, load, parse_policy, parse_runs, parse_suite, score
from agentmeter.__main__ import main
from agentmeter.demo import generate
from agentmeter.grade import grade_run

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

SUITE = json.dumps({"suite": "t", "tasks": [
    {"id": "A", "category": "c1", "expect": {"output_contains": ["shipped"], "tools_required": ["lookup"],
                                             "tools_forbidden": ["refund"]}},
    {"id": "B", "category": "c2", "expect": {"output_contains": ["only help"], "output_excludes": ["sorry"]}},
]})
POLICY = json.dumps({"tools": {"lookup": {}, "refund": {"max": {"amount": 500}, "approval_above": {"amount": 100}},
                               "delete": {"allowed": False}}})


def run(**kw):
    base = {"run_id": "r1", "agent_version": "v1", "task_id": "A", "steps": [{"tool": "lookup", "args": {}}],
            "output": "It has shipped.", "cost_usd": 0.01, "latency_s": 2.0}
    base.update(kw)
    return json.dumps(base)


def grade(line, policy=POLICY):
    suite = parse_suite(SUITE)
    return grade_run(parse_runs(line, suite)[0], suite, parse_policy(policy) if policy else None)


# ---------- data contract ----------

def test_sample_data_loads():
    runs, suite, policy = load(DATA)
    assert len(runs) == 1920 and len(suite.tasks) == 60 and policy is not None


@pytest.mark.parametrize("line,msg", [
    ("{not json", "line 1: not valid JSON"),
    (run(run_id=""), "'run_id' must be a non-empty string"),
    (run(task_id="ZZ"), "task_id 'ZZ' not found"),
    (run(cost_usd=-1), "'cost_usd' must be a number"),
    (run(latency_s="2"), "'latency_s' must be a number"),
    (run(trial=0.5), "'trial' must be a whole number"),
    (run(steps=[{"args": {}}]), "needs a 'tool' name"),
    (run(steps=[{"tool": "x", "ok": "yes"}]), "'ok' and 'blocked' must be true or false"),
    (run(output=5), "'output' must be a string"),
])
def test_bad_runs_are_explained(line, msg):
    with pytest.raises(ContractError, match=msg):
        parse_runs(line, parse_suite(SUITE))


def test_duplicate_run_id_and_empty_file():
    with pytest.raises(ContractError, match="duplicate run_id"):
        parse_runs(run() + "\n" + run(), parse_suite(SUITE))
    with pytest.raises(ContractError, match="has no runs"):
        parse_runs("\n\n", parse_suite(SUITE))


def test_suite_and_policy_errors():
    with pytest.raises(ContractError, match="'tasks' list"):
        parse_suite('{"tests": []}')
    with pytest.raises(ContractError, match="duplicate task id"):
        parse_suite('{"tasks": [{"id": "a"}, {"id": "a"}]}')
    with pytest.raises(ContractError, match="list of strings"):
        parse_suite('{"tasks": [{"id": "a", "expect": {"tools_required": "lookup"}}]}')
    with pytest.raises(ContractError, match="not valid JSON"):
        parse_policy("{")
    with pytest.raises(ContractError, match="map argument names to numbers"):
        parse_policy('{"tools": {"refund": {"max": {"amount": "100"}}}}')


def test_excel_style_bom_is_accepted(tmp_path):
    for name in ("runs.jsonl", "tasks.json", "policy.json"):
        (tmp_path / name).write_bytes(b"\xef\xbb\xbf" + (DATA / name).read_bytes())
    runs, suite, policy = load(tmp_path)
    assert len(runs) == 1920


# ---------- permissions ----------

def test_policy_decisions():
    g = Guard(parse_policy(POLICY))
    assert g.check("lookup", {}).kind == "allowed"
    assert g.check("unknown", {}).reason == "unknown: tool is not in the policy"
    assert g.check("delete", {}).reason == "delete: tool is not allowed"
    assert g.check("refund", {"amount": 50}).permitted
    d = g.check("refund", {"amount": 250})
    assert not d.permitted and d.reason == "refund: amount 250 is over 100 without approval"
    assert g.check("refund", {"amount": 250}, approved_by="maria").kind == "approved"
    assert g.check("refund", {"amount": 900}, approved_by="maria").reason == "refund: amount 900 is over the hard limit of 500"
    assert g.check("refund", {"amount": 250.5}).reason == "refund: amount 250.5 is over 100 without approval"


def test_audit_marks_blocked_calls_but_not_as_violations():
    steps = [{"tool": "lookup"}, {"tool": "refund", "args": {"amount": 300}, "blocked": True}]
    g = grade(run(steps=steps))
    assert g.violations == () and g.success
    entries = audit(parse_runs(run(steps=steps), parse_suite(SUITE)), parse_policy(POLICY))
    assert [e.decision for e in entries] == ["allowed", "blocked"]


# ---------- graders ----------

def test_success_and_each_failure_reason():
    assert grade(run()).success
    assert grade(run(output="Your order is on its way")).failure == "wrong or incomplete answer"
    assert grade(run(steps=[])).failure == "required tool not called"
    assert grade(run(steps=[{"tool": "lookup", "ok": False}])).failure == "required tool not called"
    assert grade(run(steps=[{"tool": "lookup"}, {"tool": "refund", "args": {"amount": 20}}])).failure == "forbidden tool called"
    assert grade(run(error="Timeout")).failure == "agent error"
    assert grade(run(task_id="B", steps=[], output="Sorry, I can ONLY HELP with orders")).failure == "wrong or incomplete answer"
    assert grade(run(task_id="B", steps=[], output="I can only help with orders.")).success


def test_violation_is_recorded_even_when_the_answer_is_right():
    g = grade(run(task_id="B", steps=[{"tool": "refund", "args": {"amount": 250}}], output="I can only help with orders."))
    assert g.success and g.violations == ("refund: amount 250 is over 100 without approval",)
    assert grade(run(steps=[{"tool": "lookup"}]), policy=None).violations == ()


# ---------- statistics ----------

def _runs(spec):
    """spec: list of (version, task, success) -> runs.jsonl text for the test suite."""
    lines = []
    for i, (v, t, ok, cost, lat) in enumerate(spec):
        out = ("It has shipped." if t == "A" else "I can only help with orders.") if ok else "no idea"
        steps = [{"tool": "lookup"}] if t == "A" else []
        lines.append(run(run_id=f"r{i}", agent_version=v, task_id=t, steps=steps, output=out, cost_usd=cost, latency_s=lat))
    return "\n".join(lines)


def test_scorecard_math():
    spec = [("v1", "A", True, .01, 1), ("v1", "A", True, .01, 2), ("v1", "A", False, .01, 3), ("v1", "A", True, .01, 4),
            ("v1", "B", False, .02, 5), ("v1", "B", False, .02, 6), ("v1", "B", True, .02, 7), ("v1", "B", False, .02, 8)]
    suite = parse_suite(SUITE)
    s = score(parse_runs(_runs(spec), suite), suite, None)[0]
    assert s.task_rates == {"A": .75, "B": .25}
    assert s.success.estimate == pytest.approx(.5)
    se = math.sqrt(((.75 - .5) ** 2 + (.25 - .5) ** 2) / 1) / math.sqrt(2)
    assert s.success.low == pytest.approx(max(0, .5 - 1.959963984540054 * se))
    # pass^4: task A has 3 of 4 successes, so the chance all 4 succeed is 0
    assert s.k == 4 and s.pass_k == 0
    assert s.cost_per_success == pytest.approx(.12 / 4)
    assert s.latency_p50 == 4 and s.latency_p95 == 8
    assert s.failures == {"wrong or incomplete answer": 4}


def test_pass_k_uses_the_smallest_trial_count():
    spec = [("v1", "A", True, .01, 1), ("v1", "A", True, .01, 1), ("v1", "A", False, .01, 1),
            ("v1", "B", True, .01, 1), ("v1", "B", True, .01, 1)]
    suite = parse_suite(SUITE)
    s = score(parse_runs(_runs(spec), suite), suite, None)[0]
    # k = 2. Task A: C(2,2)/C(3,2) = 1/3. Task B: 1.
    assert s.k == 2 and s.pass_k == pytest.approx((1 / 3 + 1) / 2)


# ---------- release gate ----------

@pytest.fixture(scope="module")
def sample_scores():
    runs, suite, policy = load(DATA)
    return {s.version: s for s in score(runs, suite, policy)}


def test_sample_verdicts(sample_scores):
    s = sample_scores
    assert gate(s["v1.0"], s["v1.1"]).verdict == "block"
    assert gate(s["v1.0"], s["v1.2"]).verdict == "ship"
    held = gate(s["v1.0"], s["v1.2+memory"])
    assert held.verdict == "hold" and held.reasons[0].startswith("p95 latency is up 37%")
    assert gate(s["v1.2"], s["v1.2+memory"]).reasons[0].startswith("Cost per successful run is up 25%")


def test_thresholds_change_the_verdict(sample_scores):
    s = sample_scores
    assert gate(s["v1.0"], s["v1.2+memory"], Thresholds(max_latency_increase=0.5)).verdict == "ship"
    # v1.2's success range reaches -0.1 points, so a zero-tolerance margin can't be met
    assert gate(s["v1.0"], s["v1.2"], Thresholds(max_success_drop=0.0)).verdict == "hold"


def test_significantly_worse_blocks():
    spec = []
    for t in ("A", "B"):
        for i in range(10):
            spec.append(("base", t, True, .01, 1))
            spec.append(("cand", t, i < (2 if t == "A" else 3), .01, 1))
    suite = parse_suite(SUITE)
    s = {x.version: x for x in score(parse_runs(_runs(spec), suite), suite, None)}
    g = gate(s["base"], s["cand"])
    assert g.verdict == "block" and "significantly lower" in g.reasons[0]


def test_no_tasks_in_common():
    suite = parse_suite(SUITE)
    s = {x.version: x for x in score(parse_runs(_runs([("a", "A", True, .01, 1), ("b", "B", True, .01, 1)]), suite), suite, None)}
    with pytest.raises(ValueError, match="no tasks in common"):
        gate(s["a"], s["b"])


# ---------- recorder ----------

def test_recorder_round_trip(tmp_path):
    p = tmp_path / "runs.jsonl"
    with Recorder(p, "v9", "A", run_id="x1") as r:
        r.tool_call("lookup", {"order_id": "A1"})
        r.usage(tokens_in=100, tokens_out=20, cost_usd=0.001)
        r.usage(tokens_in=50, cost_usd=0.0005)
        r.final("It has shipped.")
    with pytest.raises(RuntimeError):
        with Recorder(p, "v9", "A", run_id="x2") as r:
            raise RuntimeError("model timed out")
    runs = parse_runs(p.read_text(), parse_suite(SUITE))
    assert runs[0].tokens_in == 150 and runs[0].cost_usd == pytest.approx(0.0015) and runs[0].latency_s >= 0
    assert runs[1].error == "RuntimeError: model timed out"
    suite = parse_suite(SUITE)
    assert grade_run(runs[0], suite, None).success and grade_run(runs[1], suite, None).failure == "agent error"


# ---------- demo and command line ----------

def test_demo_is_reproducible_and_matches_shipped_data(tmp_path):
    generate(tmp_path)
    for name in ("runs.jsonl", "tasks.json", "policy.json"):
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == \
            hashlib.sha256((DATA / name).read_bytes()).hexdigest(), name
        assert (DATA / name).read_bytes() == (ROOT / "docs" / "sample" / name).read_bytes(), name


def test_cli_score_and_json(capsys):
    assert main(["score", str(DATA)]) == 0
    out = capsys.readouterr().out
    assert "BLOCK  v1.1" in out and "SHIP   v1.2:" in out and "HOLD   v1.2+memory" in out
    assert main(["score", str(DATA), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [g["verdict"] for g in data["gates"]] == ["block", "ship", "hold"]


@pytest.mark.parametrize("args,code", [
    (["--candidate", "v1.2"], 0),
    (["--candidate", "v1.1"], 1),
    (["--candidate", "v1.2+memory"], 1),
    (["--candidate", "v1.2+memory", "--allow-hold"], 0),
    (["--candidate", "v1.1", "--allow-hold"], 1),
    (["--candidate", "v1.2+memory", "--max-latency-increase", "50"], 0),
    (["--candidate", "v9"], 2),
    (["--candidate", "v1.0"], 2),
])
def test_cli_gate_exit_codes(args, code, capsys):
    assert main(["gate", str(DATA)] + args) == code


def test_cli_errors_are_one_line(tmp_path, capsys):
    assert main(["score", str(tmp_path / "missing")]) == 2
    assert "file not found" in capsys.readouterr().err
    assert main(["score", str(DATA / "runs.jsonl")]) == 2
    assert "--tasks" in capsys.readouterr().err
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "tasks.json").write_text((DATA / "tasks.json").read_text())
    (bad / "runs.jsonl").write_text('{"run_id": "a"}\n')
    assert main(["score", str(bad)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: runs.jsonl, line 1:") and "Traceback" not in err


def test_cli_explicit_files_report_and_audit(tmp_path, capsys):
    assert main(["score", str(DATA / "runs.jsonl"), "--tasks", str(DATA / "tasks.json")]) == 0
    assert "violations 0" in capsys.readouterr().out  # no policy given, so nothing is checked
    out = tmp_path / "r.html"
    assert main(["report", str(DATA), "-o", str(out)]) == 0
    html = out.read_text()
    assert "Release gate" in html and "v1.2+memory" in html and "<script" not in html
    csv_path = tmp_path / "audit.csv"
    assert main(["audit", str(DATA), "--csv", str(csv_path)]) == 0
    text = capsys.readouterr().out
    assert "31 violation" in text and "73 blocked" in text
    assert len(csv_path.read_text().splitlines()) == 2363 + 1


def test_example_agent_runs_and_scores(tmp_path, monkeypatch, capsys):
    import runpy
    runpy.run_path(str(ROOT / "examples" / "record_your_agent.py"), run_name="__main__")
    out = ROOT / "example-output" / "runs.jsonl"
    try:
        assert main(["score", str(out), "--tasks", str(DATA / "tasks.json"), "--policy", str(DATA / "policy.json")]) == 0
        assert "1 version, 60 runs" in capsys.readouterr().out
    finally:
        out.unlink()
        out.parent.rmdir()


def test_audit_lists_a_limited_number_of_violations(capsys):
    assert main(["audit", str(DATA), "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert out.count("VIOLATION") == 5 and "... and 26 more (use --csv to save them all)" in out
