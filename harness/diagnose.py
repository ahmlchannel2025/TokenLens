"""diagnose.py - forensics on compressed refactor runs that failed global success.

Pure log analysis: reads batch_refactor_compressed.json, batch_summary.json (baseline)
and the raw run JSONs they point to, plus the task's SPEC text. No model calls, no
checker runs, nothing written.

    py -3.12 diagnose.py            (or in a notebook:  %run diagnose.py)

What "dropped" means here. When the harness compresses at a goal boundary, the agent
loses the transcripts of the finished goals and instead sees: the task spec + plan
(verbatim), the one-line summaries, and the list of files on disk. So, for the goals
compressed away before the step under examination:
  DROPPED      = identifiers (file names, function/class names, def signatures) in
                 those goals' transcripts that are NOT in what the agent was shown
                 instead (spec + plan + summaries + file names on disk).
  RE-ACQUIRED  = dropped identifiers that reappear in the CURRENT goal's own transcript
                 before the step (e.g. the agent cat'ed the file again), so the agent
                 had them back.
Only dropped-and-not-re-acquired identifiers can support "LIKELY BREAKAGE".
"""
import json
import re
from pathlib import Path

from tasks import task_refactor

REPO = Path(__file__).resolve().parent
OBS_CHARS = 500

FILE_RE = re.compile(r"\b[\w\-]+\.(?:py|csv|json|txt|ini|cfg|md)\b")
DEF_RE = re.compile(r"^[ \t]*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)\s*(?:->\s*([^:\n]+))?:", re.M)
CLASS_RE = re.compile(r"^[ \t]*class\s+(\w+)", re.M)


def identifiers(text: str) -> dict[str, set[str]]:
    sigs = {f"{n}({' '.join(a.split())})" + (f" -> {r.strip()}" if r else "")
            for n, a, r in DEF_RE.findall(text)}
    return {"files": set(FILE_RE.findall(text)),
            "functions": {n for n, _, _ in DEF_RE.findall(text)} | set(CLASS_RE.findall(text)),
            "signatures": sigs}


def present(ident: str, kind: str, text: str) -> bool:
    if kind == "signatures":   # a signature counts as present only if its parameter list is
        name, rest = ident.split("(", 1)   # there; the bare name being mentioned isn't enough
        params = " ".join(rest.split(")")[0].split())
        return name in text and (not params or params in " ".join(text.split()))
    return re.search(rf"(?<![\w.]){re.escape(ident)}(?!\w)", text) is not None


def step_text(st: dict, with_observation: bool = True) -> str:
    a = st["action"]["args"]
    parts = [st["intent"], st.get("thought") or "", str(a.get("path", "")), str(a.get("content", "")),
             str(a.get("cmd", ""))]
    if with_observation:
        parts.append(st["observation"])
    return "\n".join(parts)


def short(s: str, n: int = OBS_CHARS) -> str:
    s = s.replace("\r", "")
    return s if len(s) <= n else s[:n] + f" ...[{len(s) - n} more chars]"


def load(path):
    return json.loads((REPO / path).read_text(encoding="utf-8"))


def main():
    comp = load("batch_refactor_compressed.json")
    base = {r["seed"]: r for r in load("batch_summary.json")["runs"] if r["task_id"] == "refactor"}
    failed = [r for r in comp["runs"] if r["global_ok"] is not True]

    print(f"compressed batch {comp['batch_id']}: {len(comp['runs'])} runs, "
          f"{len(failed)} not globally OK\n")
    print("1. FAILED RUNS")
    for r in failed:
        print(f"   seed {r['seed']}  end_reason={r['end_reason']}  n_steps={r['n_steps']}  {r['raw_json']}")

    verdicts = []
    for r in failed:
        seed = r["seed"]
        print("\n" + "=" * 90 + f"\nSEED {seed}   (end_reason={r['end_reason']}, {r['n_steps']} steps)\n" + "=" * 90)
        if r["end_reason"] == "error":
            print(f"   errored run, nothing to analyse: {(r.get('error') or '').strip().splitlines()[-1:]}")
            verdicts.append((seed, "UNCLEAR: run errored (no steps logged)"))
            continue
        run = load(r["raw_json"])
        steps, plan, summaries = run["steps"], run["plan_tree"], run["compression"]["summaries"]

        # ---- 2. boundaries and injected summaries ----
        print("\n2. GOAL BOUNDARIES AND INJECTED SUMMARIES (verbatim)")
        for c in summaries:
            print(f"   goal {c['goal_index']} ({plan[c['goal_index']]['goal']})\n"
                  f"      compressed before step {c['before_step_idx']}; summary:\n      {c['summary']}")

        # ---- 3. where it went wrong ----
        failed_steps = [s for s in steps if s.get("executed") is False or s["observation"].startswith("ERROR")
                        or re.match(r"exit code: (?!0\b)", s["observation"])]
        # same definition as batch.step_failed (raw steps don't carry the flag)
        if failed_steps:
            focus = [failed_steps[-1]]
            why = f"last step with step_failed=True (step {focus[0]['step_idx']})"
        else:
            focus = steps[-3:]
            why = "no step_failed=True in this run; showing the last 3 steps"
        print(f"\n3. WHERE IT WENT WRONG: {why}")
        for st in focus:
            print(f"   [step {st['step_idx']}  goal {st['goal_index']}.{st['substep_index']}  {st['tag']}]  "
                  f"tool={st['action']['tool']}  self-report={st['local_check_self_report_untrusted']}\n"
                  f"      intent: {st['intent']}\n"
                  f"      observation: {short(st['observation'])}")
        last = steps[-1]
        if r["end_reason"] == "no_tool_call":
            print(f"   NOTE: the run ENDED because, after step {last['step_idx']} (goal {last['goal_index']}."
                  f"{last['substep_index']}), the model replied without calling a tool. The harness does not "
                  "store that final reply, so the logs cannot show what it said.")
            if failed_steps and failed_steps[-1]["step_idx"] != last["step_idx"]:
                print(f"   NOTE: step {failed_steps[-1]['step_idx']} failed but the run continued after it; "
                      "it is not the step the run ended on.")

        # ---- 4. the key diff ----
        at = focus[0]["step_idx"]
        dropped_goals = sorted({c["goal_index"] for c in summaries if c["before_step_idx"] <= at})
        last_reset = max((c["before_step_idx"] for c in summaries if c["before_step_idx"] <= at), default=None)
        print(f"\n4. KEY DIFF for step {at}")
        print(f"   the agent was trying to: {focus[0]['intent']}")
        if not dropped_goals:
            print("   no compression had happened yet at this step: nothing was dropped.")
            used_lost, reacq_used = {}, {}
        else:
            dropped_text = "\n".join(step_text(s) for s in steps if s["goal_index"] in dropped_goals)
            dropped_text += "\n" + "\n".join(x["summary"] for x in run["substeps"] if x["goal_index"] in dropped_goals)
            # what the agent was shown instead: spec + plan (verbatim), summaries, file names on
            # disk (reconstructed: setup files + files written before the reset)
            on_disk = set(task_refactor.setup_files(seed)) | {
                str(s["action"]["args"].get("path")) for s in steps
                if s["action"]["tool"] == "write_file" and s.get("executed") and s["step_idx"] < last_reset}
            shown = "\n".join([task_refactor.SPEC, json.dumps(plan)]
                              + [c["summary"] for c in summaries if c["before_step_idx"] <= at] + sorted(on_disk))
            reacq_text = "\n".join(step_text(s) for s in steps if last_reset <= s["step_idx"] < at)
            failing_text = "\n".join(step_text(s, with_observation=False) for s in focus)

            ids = identifiers(dropped_text)
            print(f"   goals compressed away before this step: {dropped_goals} "
                  f"(last reset before step {last_reset})")
            used_lost, reacq_used = {}, {}
            for kind in ("files", "functions", "signatures"):
                dropped = sorted(i for i in ids[kind] if not present(i, kind, shown))
                reacq = [i for i in dropped if present(i, kind, reacq_text)]
                lost = [i for i in dropped if i not in reacq]
                used_lost[kind] = [i for i in lost if present(i.split("(")[0], "functions", failing_text)]
                reacq_used[kind] = [i for i in reacq if present(i.split("(")[0], "functions", failing_text)]
                print(f"   {kind:10} in dropped transcript but NOT in summary/spec/plan/file list: "
                      f"{dropped or '-'}")
                print(f"   {'':10}   of those, re-acquired by the agent before step {at}: {reacq or '-'}")
                print(f"   {'':10}   still missing AND referenced by the failing step: {used_lost[kind] or '-'}")

        # ---- 5. baseline, same seed ----
        b = base.get(seed)
        print(f"\n5. BASELINE, same seed {seed}")
        if b is None:
            print("   no baseline run for this seed")
        else:
            print(f"   baseline global_ok={b['global_ok']}  end_reason={b['end_reason']}  n_steps={b['n_steps']}  "
                  f"{b['raw_json']}")
            if b["global_ok"] is not True:
                print("   " + "!" * 70 + "\n   !!  BASELINE ALSO FAILED THIS SEED: not compression's fault.\n   "
                      + "!" * 70)
            braw = load(b["raw_json"])
            print("   (plans differ between runs, so these are matched by POSITION only)")
            for st in focus:
                bs = next((x for x in braw["steps"] if x["step_idx"] == st["step_idx"]), None)
                if bs is None:
                    print(f"   step {st['step_idx']}: baseline run had no step at this position")
                    continue
                g = bs["goal_index"]
                print(f"   step {bs['step_idx']} [goal {g}: {braw['plan_tree'][g]['goal']}] "
                      f"tool={bs['action']['tool']}\n      intent: {bs['intent']}\n"
                      f"      observation: {short(bs['observation'], 250)}")

        # ---- classification ----
        lost_any = sorted({i for v in used_lost.values() for i in v})
        reacq_any = sorted({i for v in reacq_used.values() for i in v})
        if b is not None and b["global_ok"] is not True:
            v = "NOISE: baseline also failed this seed"
        elif lost_any:
            v = f"LIKELY BREAKAGE: summary dropped {lost_any} that the failing step used"
        else:
            v = "UNCLEAR: failing step doesn't obviously reference dropped context"
            if reacq_any:
                v += f" (it references {reacq_any}, but the agent had re-read those after the reset)"
            if r["end_reason"] == "no_tool_call":
                v += "; run ended on an unlogged no-tool reply"
        verdicts.append((seed, v))
        print(f"\n>>> seed {seed}: {v}")

    print("\n" + "=" * 90 + "\nCLASSIFICATION")
    for seed, v in verdicts:
        print(f"   seed {seed}: {v}")


if __name__ == "__main__":
    main()
