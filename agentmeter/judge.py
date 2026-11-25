"""Grade open-ended answers with a model, against each task's rubric.

For every run whose task has a rubric and no verdict yet, the judge model reads the task prompt, the
rubric and the agent's answer, and returns pass or fail with a one-sentence reason. Verdicts are
written into runs.jsonl (judge_pass, judge_reason); a backup of the original file is kept.

A model judge is itself a measurement with error. Spot-check a sample of verdicts by hand, and keep
the judge model and prompt fixed when you compare versions.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

JUDGE_SYSTEM = ("You are a strict, consistent evaluator of an AI assistant's answers. Judge only against the "
                "rubric. Reply with JSON only, no other text: {\"pass\": true or false, \"reason\": \"one sentence\"}")


def _verdict(text: str):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d.get("pass"), bool):
        return None
    return d["pass"], str(d.get("reason", ""))[:300]


def judge_runs(runs_path, suite, model, redo: bool = False, progress=None) -> dict:
    """Add judge verdicts to runs.jsonl in place. Returns counts."""
    path = Path(runs_path)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    judged = skipped = failed = 0
    out = []
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        r = json.loads(line)
        task = suite.tasks.get(r.get("task_id"))
        rubric = task.expect.rubric if task else ""
        if not rubric or (r.get("judge_pass") is not None and not redo):
            skipped += 1
            out.append(line)
            continue
        prompt = (f"Task given to the assistant:\n{task.prompt}\n\nRubric:\n{rubric}\n\n"
                  f"The assistant's final answer:\n{r.get('output', '')}")
        turn = model.complete(JUDGE_SYSTEM, [{"role": "user", "content": prompt}], [])
        v = _verdict(turn.text)
        if v is None:
            failed += 1
        else:
            r["judge_pass"], r["judge_reason"] = v
            judged += 1
        out.append(json.dumps(r, separators=(",", ":")))
        if progress:
            progress(f"{judged + failed} judged")
    if judged:
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return {"judged": judged, "unparsed": failed, "skipped": skipped}
