/* AgentMeter in the browser. Same graders, statistics and release gate as the Python package
   (checked for identical results by tests/parity.js). Nothing is uploaded: files are read locally. */
(function () {
"use strict";
const Z95 = 1.959963984540054;
const FAILURE_ORDER = ["agent error", "forbidden tool called", "required tool not called", "wrong or incomplete answer"];

class ContractError extends Error {}

// ---------- data contract ----------
const isNum = v => typeof v === "number" && Number.isFinite(v);
const isWhole = v => Number.isInteger(v) && v >= 0;
function loadJson(text, name) {
  try { return JSON.parse(text.replace(/^﻿/, "")); }
  catch (e) { throw new ContractError(`${name} is not valid JSON: ${e.message}`); }
}
function strList(obj, key, where) {
  const v = obj[key] === undefined ? [] : obj[key];
  if (!Array.isArray(v) || !v.every(x => typeof x === "string")) throw new ContractError(`${where}: '${key}' must be a list of strings`);
  return v;
}
function numMap(obj, key, where) {
  const v = obj[key];
  if (v === undefined || v === null) return null;
  if (typeof v !== "object" || Array.isArray(v) || !Object.values(v).every(isNum)) throw new ContractError(`${where}: '${key}' must map argument names to numbers`);
  return v;
}

function parseSuite(text, name = "tasks.json") {
  const data = loadJson(text, name);
  if (!data || typeof data !== "object" || !Array.isArray(data.tasks)) throw new ContractError(`${name} must be an object with a 'tasks' list`);
  const tasks = new Map();
  data.tasks.forEach((t, i) => {
    let where = `${name}, task ${i + 1}`;
    if (!t || typeof t !== "object" || Array.isArray(t)) throw new ContractError(`${where}: must be an object`);
    if (typeof t.id !== "string" || !t.id) throw new ContractError(`${where}: 'id' must be a non-empty string`);
    if (tasks.has(t.id)) throw new ContractError(`${where}: duplicate task id '${t.id}'`);
    where = `${name}, task '${t.id}'`;
    const exp = t.expect === undefined ? {} : t.expect;
    if (!exp || typeof exp !== "object" || Array.isArray(exp)) throw new ContractError(`${where}: 'expect' must be an object`);
    const cat = t.category === undefined ? "general" : t.category, prompt = t.prompt === undefined ? "" : t.prompt;
    if (typeof cat !== "string" || typeof prompt !== "string") throw new ContractError(`${where}: 'category' and 'prompt' must be strings`);
    tasks.set(t.id, { id: t.id, category: cat || "general", prompt, expect: {
      output_contains: strList(exp, "output_contains", where), output_excludes: strList(exp, "output_excludes", where),
      tools_required: strList(exp, "tools_required", where), tools_forbidden: strList(exp, "tools_forbidden", where) } });
  });
  if (!tasks.size) throw new ContractError(`${name} has no tasks`);
  return { name: typeof data.suite === "string" ? data.suite : "", tasks };
}

function parsePolicy(text, name = "policy.json") {
  const data = loadJson(text, name);
  if (!data || typeof data !== "object" || !data.tools || typeof data.tools !== "object" || Array.isArray(data.tools))
    throw new ContractError(`${name} must be an object with a 'tools' object`);
  const tools = new Map();
  for (const [tool, r] of Object.entries(data.tools)) {
    const where = `${name}, tool '${tool}'`;
    if (!r || typeof r !== "object" || Array.isArray(r)) throw new ContractError(`${where}: must be an object`);
    const allowed = r.allowed === undefined ? true : r.allowed;
    if (typeof allowed !== "boolean") throw new ContractError(`${where}: 'allowed' must be true or false`);
    tools.set(tool, { allowed, max: numMap(r, "max", where), approval_above: numMap(r, "approval_above", where) });
  }
  return { tools };
}

function parseStep(s, where) {
  if (!s || typeof s !== "object" || Array.isArray(s)) throw new ContractError(`${where}: each step must be an object`);
  if (typeof s.tool !== "string" || !s.tool) throw new ContractError(`${where}: each step needs a 'tool' name`);
  let args = s.args === undefined || s.args === null ? {} : s.args;
  if (typeof args !== "object" || Array.isArray(args)) throw new ContractError(`${where}: 'args' of '${s.tool}' must be an object`);
  const ok = s.ok === undefined ? true : s.ok, blocked = s.blocked === undefined ? false : s.blocked;
  if (typeof ok !== "boolean" || typeof blocked !== "boolean") throw new ContractError(`${where}: 'ok' and 'blocked' must be true or false`);
  const approved = s.approved_by === undefined ? null : s.approved_by;
  if (approved !== null && typeof approved !== "string") throw new ContractError(`${where}: 'approved_by' must be a string or null`);
  return { tool: s.tool, args, ok, approved_by: approved || null, blocked };
}

function parseRuns(text, suite, name = "runs.jsonl") {
  const runs = [], seen = new Set(), unknown = new Map();
  text.replace(/^﻿/, "").split(/\r\n|\n|\r/).forEach((raw, i) => {
    const line = raw.trim();
    if (!line) return;
    const where = `${name}, line ${i + 1}`;
    let r;
    try { r = JSON.parse(line); } catch (e) { throw new ContractError(`${where}: not valid JSON`); }
    if (!r || typeof r !== "object" || Array.isArray(r)) throw new ContractError(`${where}: must be a JSON object`);
    for (const k of ["run_id", "agent_version", "task_id"])
      if (typeof r[k] !== "string" || !r[k]) throw new ContractError(`${where}: '${k}' must be a non-empty string`);
    if (seen.has(r.run_id)) throw new ContractError(`${where}: duplicate run_id '${r.run_id}'`);
    seen.add(r.run_id);
    if (!suite.tasks.has(r.task_id)) { if (!unknown.has(r.task_id)) unknown.set(r.task_id, i + 1); return; }
    const steps = r.steps === undefined ? [] : r.steps;
    if (!Array.isArray(steps)) throw new ContractError(`${where}: 'steps' must be a list`);
    const output = r.output === undefined || r.output === null ? "" : r.output;
    if (typeof output !== "string") throw new ContractError(`${where}: 'output' must be a string`);
    for (const k of ["cost_usd", "latency_s"])
      if (!isNum(r[k]) || r[k] < 0) throw new ContractError(`${where}: '${k}' must be a number, 0 or more`);
    const trial = r.trial === undefined ? 1 : r.trial, tin = r.tokens_in === undefined ? 0 : r.tokens_in, tout = r.tokens_out === undefined ? 0 : r.tokens_out;
    for (const [k, v] of [["trial", trial], ["tokens_in", tin], ["tokens_out", tout]])
      if (!isWhole(v)) throw new ContractError(`${where}: '${k}' must be a whole number, 0 or more`);
    const err = r.error === undefined ? null : r.error;
    if (err !== null && typeof err !== "string") throw new ContractError(`${where}: 'error' must be a string or null`);
    runs.push({ run_id: r.run_id, agent_version: r.agent_version, task_id: r.task_id, trial,
      steps: steps.map(s => parseStep(s, where)), output, cost_usd: r.cost_usd, latency_s: r.latency_s,
      tokens_in: tin, tokens_out: tout, error: err || null });
  });
  if (unknown.size) {
    const [tid, ln] = unknown.entries().next().value;
    const more = unknown.size > 1 ? ` and ${unknown.size - 1} more` : "";
    throw new ContractError(`${name}, line ${ln}: task_id '${tid}'${more} not found in the task suite`);
  }
  if (!runs.length) throw new ContractError(`${name} has no runs`);
  return runs;
}

// ---------- policy ----------
const fmtArg = v => String(v);
function check(policy, tool, args, approvedBy) {
  if (!policy) return { kind: "unchecked", reason: "" };
  const rule = policy.tools.get(tool);
  if (!rule) return { kind: "violation", reason: `${tool}: tool is not in the policy` };
  if (!rule.allowed) return { kind: "violation", reason: `${tool}: tool is not allowed` };
  for (const [arg, limit] of Object.entries(rule.max || {})) {
    const v = args[arg];
    if (isNum(v) && v > limit) return { kind: "violation", reason: `${tool}: ${arg} ${fmtArg(v)} is over the hard limit of ${fmtArg(limit)}` };
  }
  const needs = [];
  for (const [arg, limit] of Object.entries(rule.approval_above || {})) {
    const v = args[arg];
    if (isNum(v) && v > limit) needs.push(`${arg} ${fmtArg(v)} is over ${fmtArg(limit)}`);
  }
  if (needs.length) {
    if (approvedBy) return { kind: "approved", reason: `${tool}: ${needs.join("; ")}, approved by ${approvedBy}` };
    return { kind: "violation", reason: `${tool}: ${needs.join("; ")} without approval` };
  }
  return { kind: "allowed", reason: "" };
}
function checkStep(policy, s) {
  const d = check(policy, s.tool, s.args, s.approved_by);
  if (s.blocked) return { kind: "blocked", reason: d.reason || `${s.tool}: stopped by the runtime guard` };
  return d;
}

// ---------- graders and scorecards ----------
function gradeRun(run, suite, policy) {
  const exp = suite.tasks.get(run.task_id).expect, out = run.output.toLowerCase();
  const answerOk = exp.output_contains.every(s => out.includes(s.toLowerCase())) && !exp.output_excludes.some(s => out.includes(s.toLowerCase()));
  const executed = run.steps.filter(s => !s.blocked);
  const succeeded = new Set(executed.filter(s => s.ok).map(s => s.tool)), called = new Set(executed.map(s => s.tool));
  const missing = exp.tools_required.filter(t => !succeeded.has(t)), forbidden = exp.tools_forbidden.filter(t => called.has(t));
  const toolsOk = !missing.length && !forbidden.length;
  const failure = run.error ? "agent error" : forbidden.length ? "forbidden tool called" : missing.length ? "required tool not called" : !answerOk ? "wrong or incomplete answer" : null;
  const violations = [];
  for (const s of run.steps) { const d = checkStep(policy, s); if (d.kind === "violation") violations.push(d.reason); }
  return { run, success: failure === null, failure, toolsOk, violations, toolCalls: executed.length, toolErrors: executed.filter(s => !s.ok).length };
}
const sum = xs => { let t = 0; for (const x of xs) t += x; return t; };
const mean = xs => sum(xs) / xs.length;
function clustered(rates) {
  const m = mean(rates), n = rates.length;
  if (n < 2) return { estimate: m, low: m, high: m };
  const se = Math.sqrt(sum(rates.map(r => (r - m) ** 2)) / (n - 1)) / Math.sqrt(n);
  return { estimate: m, low: Math.max(0, m - Z95 * se), high: Math.min(1, m + Z95 * se) };
}
function percentile(xs, q) {
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.max(0, Math.ceil(q * s.length) - 1)];
}
function comb(n, k) { let r = 1n; for (let i = 1n; i <= BigInt(k); i++) r = r * (BigInt(n) - BigInt(k) + i) / i; return r; }
const passK = (c, n, k) => Number(comb(c, k)) / Number(comb(n, k));

function versionsIn(runs) { const v = []; for (const r of runs) if (!v.includes(r.agent_version)) v.push(r.agent_version); return v; }

function scoreVersion(version, grades, suite) {
  const gs = grades.filter(g => g.run.agent_version === version);
  const perTask = new Map();
  for (const tid of suite.tasks.keys()) perTask.set(tid, []);
  for (const g of gs) perTask.get(g.run.task_id).push(g.success);
  for (const [t, v] of [...perTask]) if (!v.length) perTask.delete(t);
  const taskRates = {};
  for (const [t, v] of perTask) taskRates[t] = sum(v.map(Number)) / v.length;
  const k = Math.min(...[...perTask.values()].map(v => v.length));
  const pass_k = mean([...perTask.values()].map(v => passK(sum(v.map(Number)), v.length, k)));
  const byCat = new Map();
  for (const [t, rate] of Object.entries(taskRates)) {
    const c = suite.tasks.get(t).category;
    if (!byCat.has(c)) byCat.set(c, []);
    byCat.get(c).push(rate);
  }
  const by_category = {};
  for (const [c, v] of byCat) by_category[c] = mean(v);
  const failures = {};
  for (const f of FAILURE_ORDER) { const n = gs.filter(g => g.failure === f).length; if (n) failures[f] = n; }
  const calls = sum(gs.map(g => g.toolCalls)), successes = gs.filter(g => g.success).length;
  const totalCost = sum(gs.map(g => g.run.cost_usd));
  const viol = gs.flatMap(g => g.violations), examples = [];
  for (const v of viol) { if (!examples.includes(v)) examples.push(v); if (examples.length === 3) break; }
  const lat = gs.map(g => g.run.latency_s);
  return {
    version, runs: gs.length, tasks: perTask.size, k,
    success: clustered(Object.values(taskRates)), pass_k,
    tool_accuracy: gs.filter(g => g.toolsOk).length / gs.length,
    tool_error_rate: calls ? sum(gs.map(g => g.toolErrors)) / calls : null,
    violations: viol.length, violation_run_share: gs.filter(g => g.violations.length).length / gs.length,
    violation_examples: examples,
    blocked_calls: sum(gs.map(g => g.run.steps.filter(s => s.blocked).length)),
    cost_per_run: totalCost / gs.length, cost_per_success: successes ? totalCost / successes : null, total_cost: totalCost,
    tokens_in: sum(gs.map(g => g.run.tokens_in)), tokens_out: sum(gs.map(g => g.run.tokens_out)),
    latency_p50: percentile(lat, 0.5), latency_p95: percentile(lat, 0.95),
    avg_steps: sum(gs.map(g => g.run.steps.length)) / gs.length,
    by_category, failures, task_rates: taskRates,
  };
}
function score(runs, suite, policy) {
  const grades = runs.map(r => gradeRun(r, suite, policy));
  return versionsIn(runs).map(v => scoreVersion(v, grades, suite));
}

// ---------- release gate ----------
const DEFAULTS = { max_success_drop: 0.05, max_cost_increase: 0.20, max_latency_increase: 0.25 };
const pts = x => { const s = (x * 100).toFixed(1); return s.startsWith("-") ? s : "+" + s; };
const pctSigned = x => { const s = (x * 100).toFixed(0); return (s.startsWith("-") ? s : "+" + s) + "%"; };
const g6 = x => String(Number(x.toPrecision(6)));

function gate(base, cand, t = DEFAULTS) {
  const common = Object.keys(base.task_rates).filter(tid => tid in cand.task_rates);
  if (!common.length) throw new ContractError(`'${base.version}' and '${cand.version}' have no tasks in common`);
  const diffs = common.map(tid => cand.task_rates[tid] - base.task_rates[tid]);
  const m = mean(diffs);
  const se = diffs.length > 1 ? Math.sqrt(sum(diffs.map(d => (d - m) ** 2)) / (diffs.length - 1)) / Math.sqrt(diffs.length) : 0;
  const diff = { estimate: m, low: m - Z95 * se, high: m + Z95 * se };
  const costChange = base.cost_per_success && cand.cost_per_success !== null ? cand.cost_per_success / base.cost_per_success - 1 : null;
  const latencyChange = base.latency_p95 > 0 ? cand.latency_p95 / base.latency_p95 - 1 : null;
  const block = [], hold = [];
  if (cand.violations) {
    const ex = cand.violation_examples.length ? ` (for example: ${cand.violation_examples[0]})` : "";
    block.push(`${cand.violations} policy violation${cand.violations !== 1 ? "s" : ""} in ${(cand.violation_run_share * 100).toFixed(1)}% of runs${ex}.`);
  }
  const rng = `95% range ${pts(diff.low)} to ${pts(diff.high)}`;
  if (diff.high < 0) block.push(`Success is significantly lower: ${pts(diff.estimate)} points (${rng}).`);
  else if (diff.low < -t.max_success_drop)
    hold.push(`A success drop of more than ${g6(t.max_success_drop * 100)} points can't be ruled out (${pts(diff.estimate)} points, ${rng}). Add tasks or trials to narrow it.`);
  if (cand.cost_per_success === null) block.push("No run succeeded.");
  else if (costChange !== null && costChange > t.max_cost_increase)
    hold.push(`Cost per successful run is up ${(costChange * 100).toFixed(0)}% ($${base.cost_per_success.toFixed(4)} to $${cand.cost_per_success.toFixed(4)}), over the ${g6(t.max_cost_increase * 100)}% budget.`);
  if (latencyChange !== null && latencyChange > t.max_latency_increase)
    hold.push(`p95 latency is up ${(latencyChange * 100).toFixed(0)}% (${base.latency_p95.toFixed(1)}s to ${cand.latency_p95.toFixed(1)}s), over the ${g6(t.max_latency_increase * 100)}% budget.`);
  let verdict, reasons;
  if (block.length) { verdict = "block"; reasons = block.concat(hold); }
  else if (hold.length) { verdict = "hold"; reasons = hold; }
  else {
    verdict = "ship";
    reasons = [`Success ${pts(diff.estimate)} points (${rng}), within the ${g6(t.max_success_drop * 100)}-point margin.`, "No policy violations."];
    if (costChange !== null) reasons.push(`Cost per successful run ${pctSigned(costChange)}.`);
    if (latencyChange !== null) reasons.push(`p95 latency ${pctSigned(latencyChange)}.`);
  }
  if (!cand.violations && base.violations) reasons.push(`Fixes the baseline's ${base.violations} policy violation${base.violations !== 1 ? "s" : ""}.`);
  if (diff.low > 0) reasons.push(`Success is significantly higher: ${pts(diff.estimate)} points (${rng}).`);
  return { baseline: base.version, candidate: cand.version, common_tasks: common.length, success_diff: diff,
    cost_change: costChange, latency_change: latencyChange, candidate_violations: cand.violations,
    baseline_violations: base.violations, verdict, reasons };
}
function gateAll(scores, baseline, t = DEFAULTS) {
  if (!scores.length) return [];
  const base = baseline ? scores.find(s => s.version === baseline) : scores[0];
  return scores.filter(s => s.version !== base.version).map(s => gate(base, s, t));
}

const api = { ContractError, parseSuite, parsePolicy, parseRuns, check, gradeRun, score, gate, gateAll, DEFAULTS };
if (typeof module !== "undefined" && module.exports) { module.exports = api; return; }

// ---------- page ----------
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = x => x === null || x === undefined ? "n/a" : (x * 100).toFixed(1) + "%";
const money = x => x === null ? "n/a" : "$" + x.toFixed(4);
const chg = x => x === null ? "n/a" : pctSigned(x);
const LABEL = { ship: "Ship", hold: "Hold", block: "Block" };
let state = null;
const files = { runs: null, tasks: null, policy: null };

function thresholds() {
  const read = (id, d) => { const v = parseFloat($(id).value); return Number.isFinite(v) && v >= 0 ? v / 100 : d; };
  return { max_success_drop: read("t-drop", DEFAULTS.max_success_drop), max_cost_increase: read("t-cost", DEFAULTS.max_cost_increase),
    max_latency_increase: read("t-lat", DEFAULTS.max_latency_increase) };
}

function chart(scores) {
  const W = 720, rowH = 34, top = 26, left = 150, right = 30, H = top + scores.length * rowH + 30;
  const lo = Math.max(0, Math.floor(Math.min(...scores.map(s => s.success.low)) * 10) / 10 - 0.1);
  const x = v => left + (v - lo) / (1 - lo) * (W - left - right);
  let g = "";
  for (let v = lo; v <= 1.0001; v += 0.1) g += `<line class="grid" x1="${x(v)}" x2="${x(v)}" y1="${top - 8}" y2="${H - 24}"/><text class="tick" x="${x(v)}" y="${H - 8}" text-anchor="middle">${Math.round(v * 100)}%</text>`;
  scores.forEach((s, i) => {
    const y = top + i * rowH + rowH / 2, cls = s.violations ? "bad" : "ok";
    g += `<text class="label" x="${left - 12}" y="${y + 4}" text-anchor="end">${esc(s.version)}</text>
      <line class="ci ${cls}" x1="${x(s.success.low)}" x2="${x(s.success.high)}" y1="${y}" y2="${y}"/>
      <circle class="dot ${cls}" cx="${x(s.success.estimate)}" cy="${y}" r="6"><title>${esc(s.version)}: ${pct(s.success.estimate)} (${pct(s.success.low)} to ${pct(s.success.high)})</title></circle>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Task success rate by agent version, with 95% ranges">${g}</svg>`;
}

function render() {
  if (!state) return;
  const { runs, suite, policy, source } = state;
  let scores, gates;
  try {
    scores = score(runs, suite, policy);
    const sel = $("baseline").value;
    gates = gateAll(scores, scores.some(s => s.version === sel) ? sel : null, thresholds());
  } catch (e) { return showError(e); }
  const base = gates.length ? gates[0].baseline : scores[0].version;
  const opts = scores.map(s => `<option${s.version === base ? " selected" : ""}>${esc(s.version)}</option>`).join("");
  const k = scores[0].k;
  const heads = scores.map(s => `<th class="num">${esc(s.version)}</th>`).join("");
  const cats = [...new Set(scores.flatMap(s => Object.keys(s.by_category)))];
  const fails = [...new Set(scores.flatMap(s => Object.keys(s.failures)))];
  $("results").innerHTML = `
  <h2>${esc(suite.name || "Your task suite")}</h2>
  <p class="sub">${source} · ${suite.tasks.size} tasks · ${scores.length} version${scores.length !== 1 ? "s" : ""} · ${runs.length.toLocaleString("en-US")} runs${policy ? "" : " · <b>no policy file, so tool calls were not checked against permissions</b>"}</p>
  ${gates.length ? `<div class="gatehead"><h3 class="h3">Release gate</h3><label>Compare against <select id="baseline-view">${opts}</select></label></div>
  <div class="cards">${gates.map(g => `<div class="card"><h4>${esc(g.candidate)} <span class="badge ${g.verdict}">${LABEL[g.verdict]}</span></h4>
    <div class="kpis"><div><b>${pts(g.success_diff.estimate)}</b><span>success, points</span><em>range ${pts(g.success_diff.low)} to ${pts(g.success_diff.high)}</em></div>
    <div><b>${g.candidate_violations}</b><span>policy violations</span></div>
    <div><b>${chg(g.cost_change)}</b><span>cost per success</span></div><div><b>${chg(g.latency_change)}</b><span>p95 latency</span></div></div>
    <ul>${g.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul></div>`).join("")}</div>` : `<p class="hint">Only one agent version is in this file. Add runs from a second version to get a release verdict.</p>`}
  <h3 class="h3">Task success, with 95% ranges</h3>
  <div class="chart">${chart(scores)}</div>
  <p class="legend"><span class="key ok-key"></span>no policy violations <span class="key bad-key"></span>has policy violations</p>
  <h3 class="h3">Scorecard</h3>
  <div class="scroll"><table><thead><tr><th>Version</th><th class="num">Success</th><th class="num">pass^${k}</th><th class="num">Right tools</th><th class="num">Violations</th><th class="num">Blocked by guard</th><th class="num">Cost / success</th><th class="num">p95 latency</th><th class="num">Runs</th></tr></thead><tbody>
  ${scores.map(s => `<tr><td>${esc(s.version)}</td><td class="num">${pct(s.success.estimate)}</td><td class="num">${(s.pass_k * 100).toFixed(0)}%</td><td class="num">${pct(s.tool_accuracy)}</td><td class="num">${s.violations}</td><td class="num">${s.blocked_calls}</td><td class="num">${money(s.cost_per_success)}</td><td class="num">${s.latency_p95.toFixed(1)}s</td><td class="num">${s.runs.toLocaleString("en-US")}</td></tr>`).join("")}
  </tbody></table></div>
  <div class="two">
    <div><h3 class="h3">Success by category</h3><div class="scroll"><table><thead><tr><th>Category</th>${heads}</tr></thead><tbody>
    ${cats.map(c => `<tr><td>${esc(c)}</td>${scores.map(s => `<td class="num">${pct(s.by_category[c])}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div>
    ${fails.length ? `<div><h3 class="h3">Why runs failed</h3><div class="scroll"><table><thead><tr><th>Failure</th>${heads}</tr></thead><tbody>
    ${fails.map(f => `<tr><td>${esc(f)}</td>${scores.map(s => `<td class="num">${s.failures[f] || 0}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div>` : ""}
  </div>
  `;
  $("baseline").innerHTML = opts;
  const bv = $("baseline-view");
  if (bv) bv.addEventListener("change", e => { $("baseline").value = e.target.value; render(); });
}

function showError(e) {
  $("results").innerHTML = `<div class="error"><b>That file doesn't fit the data contract.</b><br>${esc(e.message)}</div>`;
  $("results").scrollIntoView({ behavior: "smooth", block: "start" });
}

function analyze(texts, source) {
  try {
    const suite = parseSuite(texts.tasks, texts.tasksName || "tasks.json");
    const policy = texts.policy ? parsePolicy(texts.policy, texts.policyName || "policy.json") : null;
    const runs = parseRuns(texts.runs, suite, texts.runsName || "runs.jsonl");
    state = { runs, suite, policy, source };
    $("baseline").innerHTML = "";
    render();
    $("results").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { showError(e); }
}

function kindOf(name, text) {
  if (/\.jsonl$/i.test(name)) return "runs";
  try {
    const d = JSON.parse(text.replace(/^﻿/, ""));
    if (d && Array.isArray(d.tasks)) return "tasks";
    if (d && d.tools && typeof d.tools === "object") return "policy";
  } catch (e) { /* not a single JSON document: treat as runs */ return "runs"; }
  return null;
}

async function onFiles(list) {
  for (const f of list) {
    const text = await f.text();
    const kind = kindOf(f.name, text);
    if (!kind) { showError(new Error(`${f.name}: couldn't tell what this file is. A task suite has a "tasks" list, a policy has a "tools" object, and runs are one JSON object per line (.jsonl).`)); return; }
    files[kind] = { name: f.name, text };
  }
  $("file-list").innerHTML = ["runs", "tasks", "policy"].map(k => `<li>${k === "runs" ? "Runs" : k === "tasks" ? "Task suite" : "Policy (optional)"}: ${files[k] ? `<b>${esc(files[k].name)}</b>` : "<span class='muted'>not chosen</span>"}</li>`).join("");
  if (files.runs && files.tasks) {
    analyze({ runs: files.runs.text, runsName: files.runs.name, tasks: files.tasks.text, tasksName: files.tasks.name,
      policy: files.policy && files.policy.text, policyName: files.policy && files.policy.name },
      "Your files, analyzed in this browser");
  } else {
    $("results").innerHTML = `<p class="hint">Got it. Now choose <b>${files.runs ? "your task suite (tasks.json)" : "your runs (runs.jsonl)"}</b> to see the results.</p>`;
  }
}

$("files").addEventListener("change", e => onFiles([...e.target.files]));
for (const id of ["t-drop", "t-cost", "t-lat"]) $(id).addEventListener("input", render);
$("sample").addEventListener("click", async () => {
  const b = $("sample"); b.disabled = true; b.textContent = "Loading…";
  try {
    const get = async f => { const r = await fetch("sample/" + f); if (!r.ok) throw new Error(`couldn't load ${f}`); return r.text(); };
    const [runs, tasks, policy] = await Promise.all(["runs.jsonl", "tasks.json", "policy.json"].map(get));
    analyze({ runs, tasks, policy }, "Synthetic sample data");
  } catch (e) { showError(e); }
  b.disabled = false; b.textContent = "Run on synthetic sample data";
});
})();
