# Design decisions

Short records of the choices behind AgentMeter: what was decided, why, and what it costs.

## 1. Build a small harness instead of adopting an agent framework

**Decision.** The agent loop (`run_agent`) is about 40 lines on top of two model adapters, with no dependencies.

**Why.** An evaluation tool has to see and control every step: each tool call must pass through the permission guard and be recorded with its arguments and result. Frameworks hide parts of the loop, change quickly, and would make the tool's results depend on a framework version. Teams that already use a framework can still use AgentMeter by recording runs with the `Recorder`; the harness is optional.

**Trade-off.** No streaming, parallel tool calls run one after another, and only two adapter families (Anthropic, OpenAI-compatible) are included.

## 2. Grade outcomes and actions, not conversations

**Decision.** A run passes when the final answer meets the task's checks, the required tools ran, no forbidden tool ran, and (for open-ended tasks) a judge passed it against a rubric.

**Why.** Whether an agent is safe to release depends on what it did and what it told the user. Checking actions catches the failures that matter most for agents: a correct-sounding answer after a refund that should never have happened.

**Trade-off.** Text checks are strict and can miss correct paraphrases. That's why the task suite owns the phrasing (via the system prompt or the expected words), and open-ended tasks use a rubric instead.

## 3. Cluster uncertainty by task, and compare versions paired by task

**Decision.** Success ranges come from the spread of per-task success rates; the release gate compares candidate and baseline on the tasks both ran.

**Why.** Repeated attempts at one task are correlated, so treating 480 runs as 480 independent samples overstates certainty. Task difficulty is the biggest source of variance, and pairing removes it, which narrows the comparison without more runs.

**Trade-off.** Ranges are normal approximations, which are rough with few tasks. Thirty or more tasks are recommended.

## 4. Report pass^k, not just average success

**Decision.** Every scorecard includes pass^k: the chance that all k attempts at a task succeed.

**Why.** Users experience reliability, not averages. Two agents with 80% success can differ completely: one fails at random, the other always fails the same 20% of tasks. pass^k exposes the first.

## 5. Gate on non-inferiority, not on "better"

**Decision.** A candidate ships when a success drop beyond a margin (5 points by default) can be ruled out, with no policy violations and cost and latency within budget.

**Why.** Most releases (cheaper prompts, new models, refactors) aim to be "no worse", not "better". Requiring a significant improvement would block nearly every change; ignoring uncertainty would let regressions through.

**Trade-off.** With few tasks, the range is wide and the gate holds. That's intended: the remedy is more tasks or trials.

## 6. One permission policy, enforced at runtime and audited after the fact

**Decision.** The same `policy.json` drives the runtime `Guard` and the audit, and blocked calls are recorded rather than dropped.

**Why.** Controls written in a document drift from what the code enforces. With one policy, the evidence that a control works (blocked calls, zero violations) comes from the same rules that run in production. Agent identities let one policy scope different agents to different tools.

**Trade-off.** The guard checks tool names and numeric arguments. Richer conditions (time of day, customer tier) need a custom check before calling the guard.

## 7. Any policy violation blocks a release

**Decision.** A single violation in the candidate's runs is a Block, whatever its success rate.

**Why.** Permission failures are not averaged away by good performance elsewhere: one unapproved $500 refund matters more than a point of task success.

## 8. A model judge is an instrument with error

**Decision.** Rubric verdicts are stored on the run (`judge_pass`, `judge_reason`), the judge model is chosen explicitly, and unjudged runs hold the gate.

**Why.** Judges disagree with people some of the time. Storing verdicts makes them auditable and spot-checkable, and a fixed judge across versions keeps comparisons fair.

## 9. MCP for tools, implemented with the standard library

**Decision.** AgentMeter includes a minimal MCP stdio server and client (initialize, tools/list, tools/call).

**Why.** MCP lets the same evaluation run against tools a team already exposes, and lets AgentMeter's demo tools be used by other MCP clients. A small implementation keeps the project dependency-free and easy to audit.

**Trade-off.** Only the tools part of MCP is implemented: no resources, prompts, sampling or HTTP transport.

## 10. Record tool results, so runs can be replayed

**Decision.** Each step can carry the tool's `result`, and `ReplayToolbox` answers tool calls from recorded runs.

**Why.** Real tools have side effects and changing data. Replaying recorded results lets a new version be tested against exactly the same environment, without touching production systems.

**Trade-off.** A new version that makes calls the old one never made gets a "no recorded result" error, so replay suits versions that behave similarly. Misses are counted.

## 11. Synthetic data only in the repository

**Decision.** The sample data comes from a seeded simulation, and the same seed regenerates it exactly (a test checks this).

**Why.** Anyone can reproduce the numbers, nothing comes from a real organization, and the demo runs offline without an API key.
