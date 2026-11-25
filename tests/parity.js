// Checks that the browser version (docs/app.js) and the Python package give identical results.
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");
const root = path.join(__dirname, "..");
const app = require(path.join(root, "docs", "app.js"));
const read = f => fs.readFileSync(path.join(root, "data", f), "utf8");
const suite = app.parseSuite(read("tasks.json"));
const policy = app.parsePolicy(read("policy.json"));
const runs = app.parseRuns(read("runs.jsonl"), suite);
const scores = app.score(runs, suite, policy);
let bad = 0, checked = 0;
function same(a, b, where) {
  if (typeof a === "number" && typeof b === "number") {
    checked++;
    if (Math.abs(a - b) > 1e-12) { bad++; console.error(`${where}: js ${a} vs py ${b}`); }
    return;
  }
  if (a === null || b === null || typeof a !== "object" || typeof b !== "object") {
    checked++;
    if (a !== b) { bad++; console.error(`${where}: js ${JSON.stringify(a)} vs py ${JSON.stringify(b)}`); }
    return;
  }
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const k of keys) same(a[k], b[k], `${where}.${k}`);
}
for (const baseline of ["v1.0", "v1.2"]) {
  const py = JSON.parse(execFileSync(process.env.PYTHON || "python", ["-m", "agentmeter", "score", "data", "--json", "--baseline", baseline], { cwd: root }));
  same(scores, py.scores, "scores");
  same(app.gateAll(scores, baseline), py.gates, `gates(${baseline})`);
}
if (bad) { console.error(`${bad} differences`); process.exit(1); }
console.log(`Identical results: ${checked} values compared for ${scores.length} versions.`);
