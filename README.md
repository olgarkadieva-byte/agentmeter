# AgentMeter

**Test your AI agent before it ships.** AgentMeter scores every version of an agent on task success, reliability, tool use, permission-policy violations, cost and latency, and gives each new version a release verdict: **Ship**, **Hold** or **Block**. It runs in CI, so a regression fails the build.

**[Try the live demo](https://olgarkadieva-byte.github.io/agentmeter/)**. It runs in your browser, and your data never leaves the page.

[![tests](https://github.com/olgarkadieva-byte/agentmeter/actions/workflows/tests.yml/badge.svg)](https://github.com/olgarkadieva-byte/agentmeter/actions/workflows/tests.yml)
![License: CC BY-NC 4.0](https://img.shields.io/badge/license-CC%20BY--NC%204.0-lightgrey.svg)

## Why this exists

A new prompt, model or tool can make an agent cheaper and faster while quietly making it worse: it solves fewer tasks, gets less consistent, or starts taking actions it was never supposed to take on its own. Spot-checking a few conversations won't catch that.

AgentMeter runs the same task suite against each version and compares them with the statistics done properly: ranges clustered by task, a paired comparison against the baseline, and reliability across repeated attempts, not just an average. It checks every tool call against a permission policy, and the same policy can stop risky calls at runtime.

It works with real models: an agent loop for Claude (Anthropic API) and OpenAI-compatible models, tools from any **MCP** server, replay of recorded tool outputs, a model judge for open-ended answers, and a memory on/off experiment you can run in one command.

## How it fits together

```mermaid
flowchart LR
    T[tasks.json<br/>task suite + rubrics] --> H
    P[policy.json<br/>tools, limits, approvals,<br/>agent identities] --> G
    subgraph H[Agent harness]
      M[Model adapter<br/>Claude / OpenAI-compatible] <--> L[Agent loop<br/>step limit, timeout]
      L --> G[Runtime guard]
      G --> X[Tools<br/>Python, MCP server,<br/>replay, memory]
    end
    L --> R[Recorder]
    R --> J[runs.jsonl]
    J --> JU[Model judge<br/>rubric verdicts]
    JU --> J
    J --> S[Graders and scorecards<br/>success, pass^k, tools,<br/>violations, cost, latency]
    S --> GA{Release gate}
    GA -->|exit 0| SH[Ship]
    GA -->|exit 1| HB[Hold / Block]
    S --> RE[HTML report and<br/>browser demo]
```

Design choices and their trade-offs are written up in [DECISIONS.md](DECISIONS.md).

## What it reports, per agent version

| Area | Metrics |
|---|---|
| Quality | task success with a 95% range, success by category, why runs failed, judge verdicts on open-ended rubrics |
| Reliability | pass^k: the share of tasks the agent gets right on every one of k attempts |
| Tool use | share of runs that called the required tools and avoided forbidden ones, tool error rate |
| Permissions | policy violations (unknown tools, hard limits, actions without required approval, tools outside an agent identity's scope), calls stopped by the runtime guard, full audit log |
| Cost and speed | cost per run and per successful run, tokens, p50 and p95 latency |
| Decision | Ship / Hold / Block against a baseline version, with plain-language reasons |

## Example: the synthetic demo

The repo ships with a simulated customer-support agent (tools: `lookup_order`, `issue_refund`, `escalate_to_human`) run on 60 tasks, 8 times each, in four versions. Off-topic requests are also graded by a (simulated) judge against a rubric. Nothing in it comes from a real organization, and the simulated agent needs no API key. To run a real model on the same tasks, see [Run a real model](#run-a-real-model).

```text
$ python -m agentmeter score data
Customer support agent (synthetic): 60 tasks, 4 versions, 1,920 runs

v1.0         success 84.4% (80.9% to 87.9%)  pass^8 28%  violations 3  cost/success $0.0172  p95 7.3s
v1.1         success 76.7% (71.7% to 81.7%)  pass^8 23%  violations 28  cost/success $0.0134  p95 6.5s
v1.2         success 87.1% (83.5% to 90.7%)  pass^8 43%  violations 0  cost/success $0.0125  p95 6.7s
v1.2+memory  success 94.0% (91.6% to 96.3%)  pass^8 63%  violations 0  cost/success $0.0154  p95 10.0s

Release gate vs v1.0
BLOCK  v1.1: 28 policy violations in 5.8% of runs (for example: issue_refund: amount 258.49 is over 100 without approval).
SHIP   v1.2: Success +2.7 points (95% range -1.5 to +6.9), within the 5-point margin.
HOLD   v1.2+memory: p95 latency is up 37% (7.3s to 10.0s), over the 25% budget.
```

- **v1.1** uses a shorter prompt. It is 22% cheaper per successful task, but it sometimes issues refunds over $100 without the required approval. **Block.**
- **v1.2** keeps the cheaper prompt and adds a runtime guard. The guard stopped 37 unapproved refunds, and the agent escalated them to a person instead. Cost per success is down 27%. **Ship.**
- **v1.2+memory** adds memory. It is the most accurate version (pass^8 up from 28% to 63%), but answers are 37% slower at p95. **Hold** until that is tuned, or raise the latency budget if the trade is worth it.

## Quick start

Requires Python 3.10 or later. No dependencies beyond the standard library (tests use `pytest`).

```bash
git clone https://github.com/olgarkadieva-byte/agentmeter.git
cd agentmeter

python -m agentmeter score  data                                  # scorecards and gate verdicts
python -m agentmeter gate   data --baseline v1.0 --candidate v1.2  # one verdict; exit code 0 means ship
python -m agentmeter report data -o agent-report.html              # shareable HTML report
python -m agentmeter audit  data --csv audit.csv                   # every tool call with its policy decision
python -m agentmeter demo   my-data/                               # rerun the simulated agent
python -m agentmeter mcp-server                                    # serve the demo tools over MCP (stdio)

python -m pytest
```

`data` is a folder holding `runs.jsonl`, `tasks.json` and (optionally) `policy.json`. You can also pass files one by one: `agentmeter score runs.jsonl --tasks tasks.json --policy policy.json`. Install it as a command with `pip install git+https://github.com/olgarkadieva-byte/agentmeter.git`.

## Run a real model

The same 60 tasks, tools and policy, with a real model as the agent. You need your own API key; nothing else to install.

```bash
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY with --provider openai (add --base-url for compatible endpoints)

# a quick check on 5 tasks
python -m agentmeter run --provider anthropic --model <model-name> --version baseline --limit 5 --trials 1

# the memory experiment: same model, memory off vs on, 3 trials per task
python -m agentmeter run --provider anthropic --model <model-name> --version memory-off --trials 3 --out live
python -m agentmeter run --provider anthropic --model <model-name> --version memory-on  --trials 3 --out live --memory

python -m agentmeter judge live --provider anthropic --model <judge-model>   # rubric verdicts for open-ended tasks
python -m agentmeter score live                                              # memory-on vs memory-off, with a verdict
```

- `--price-in` and `--price-out` (USD per million tokens) turn token counts into cost. Without them, cost is 0.
- `--via-mcp` serves the tools from an MCP server subprocess instead of calling them in-process, to test the full MCP path.
- `--memory` gives the agent `save_note` and `search_notes` tools that persist across runs, so later runs can use lessons from earlier ones.
- The runtime guard is on by default (`--no-guard` turns it off, to see what the policy would have caught).
- Every run is recorded with tool results, so a new version can be tested against the same tool outputs with `ReplayToolbox`.

## Use it with your own agent

1. **Write a task suite** (`tasks.json`): for each task, the words a correct answer must contain and the tools it must or must not use.
2. **Write a policy** (`policy.json`, optional): the tools the agent may call, hard limits on arguments, and when a person must approve.
3. **Record runs** with the `Recorder`, which works with any agent framework or model API, and check calls with the `Guard`:

```python
from agentmeter import Guard, Recorder, parse_policy

guard = Guard(parse_policy(open("policy.json").read()))

with Recorder("runs.jsonl", agent_version="v2.1", task_id="R07", trial=1) as run:
    args = {"order_id": "A1007", "amount": 250}
    decision = guard.check("issue_refund", args)
    if decision.permitted:
        issue_refund(**args)
        run.tool_call("issue_refund", args)
    else:
        run.tool_call("issue_refund", args, ok=False, blocked=True)  # stopped before it ran
        escalate_to_human(order_id="A1007")
        run.tool_call("escalate_to_human", {"order_id": "A1007"})
    run.usage(tokens_in=1840, tokens_out=210, cost_usd=0.0087)  # from your model's response
    run.final("I've passed this to a specialist.")
```

4. **Or use the built-in harness** with your own tools: `run_agent` / `run_suite` with `AnthropicModel` or `OpenAIModel`, and tools from Python functions (`FunctionToolbox`), any MCP server (`MCPToolbox`), recorded runs (`ReplayToolbox`) or with memory (`MemoryToolbox`).
5. **Grade open-ended answers** by adding a `rubric` to a task and running `agentmeter judge`. Until a run is judged, the gate holds.
6. **Gate releases in CI.** [`examples/agent-gate.yml`](examples/agent-gate.yml) is a ready-made GitHub Actions workflow, and [`examples/record_your_agent.py`](examples/record_your_agent.py) is a runnable end-to-end example.

The full file formats are in the **[data contract](DATA_CONTRACT.md)**.

## How the release gate decides

| Verdict | When |
|---|---|
| **Ship** | No policy violations, success is no worse than the baseline beyond the margin (default 5 points), cost per successful run is up no more than 20%, and p95 latency is up no more than 25%. |
| **Hold** | Cost or latency is over budget, a drop in success beyond the margin can't be ruled out yet, or runs with a rubric haven't been judged. |
| **Block** | Any policy violation, or success is significantly lower than the baseline. |

Change the budgets with `--max-success-drop`, `--max-cost-increase` and `--max-latency-increase`. `--allow-hold` makes only Block fail the build.

## Repository layout

| Path | What it is |
|---|---|
| [`DATA_CONTRACT.md`](DATA_CONTRACT.md) | The three input files, field by field |
| [`agentmeter/`](agentmeter/) | Python package: data contract checks, graders, statistics, release gate, permission guard and audit, recorder, agent harness and model adapters, MCP server and client, model judge, HTML report, command line, demo agent |
| [`DECISIONS.md`](DECISIONS.md) | Why it's built this way: the design decisions and their trade-offs |
| [`data/`](data/) | Synthetic sample: 1,920 runs of the simulated agent |
| [`docs/`](docs/) | The browser demo, published with GitHub Pages. Same math as the Python package, checked for identical results. |
| [`examples/`](examples/) | Recording your own agent, and a CI workflow |
| [`tests/`](tests/) | Tests for the contract, graders, statistics, gate, guard, recorder, harness, MCP, replay, memory, judge and command line (model calls use fake transports, no network) |

## Method notes

- **Grading:** a run succeeds when the answer contains every expected phrase (and none of the excluded ones), every required tool ran successfully, no forbidden tool ran, the agent didn't crash, and, for tasks with a rubric, the judge passed it. Calls stopped by the guard don't count as having run.
- **Success ranges:** the mean of per-task success rates, with a 95% normal-approximation interval from the spread across tasks. Repeated attempts at the same task are not independent, so treating every run as a separate observation would make the range look narrower than it is.
- **Comparing versions:** paired by task. The gate uses the per-task difference in success rate between candidate and baseline on the tasks both ran, which removes most of the noise from some tasks simply being harder.
- **pass^k:** for each task with n attempts and c successes, C(c, k) / C(n, k), averaged over tasks, with k the smallest number of attempts any task has.
- **Latency:** p50 and p95 use the nearest-rank method.
- **Judges:** a model judge is a measurement with its own error. Keep the judge model and prompt fixed across versions, and spot-check a sample of verdicts by hand.
- **Limits:** the demo's tools are simulated, and tools run in the same process (or an MCP subprocess), not in a container sandbox.

## Related

- [AI Impact Measurement](https://github.com/olgarkadieva-byte/ai-impact-measurement): measures what AI tools changed in production (adoption, time saved, ROI) against a control group. AgentMeter tests an agent before release; AI Impact Measurement measures its effect after.
- [AI Governance Kit](https://github.com/olgarkadieva-byte/ai-governance-kit): AI inventory, risk scoring and 34 AI controls mapped to NIST AI RMF, ISO/IEC 42001 and the EU AI Act.
- [smartailayer.com](https://smartailayer.com/): free AI governance and measurement tools and articles.

## Author

Created by [Olga Arkadieva](https://olgarkadieva-byte.github.io/), IAPP AI Governance Professional (AIGP) and AI advisor to the University of San Francisco.

## License

[CC BY-NC 4.0](LICENSE): free to use, share and adapt for non-commercial purposes with attribution.
