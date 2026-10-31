"""noise_check.py - is `no_tool_call` caused by compression, or background noise?

No model calls. Reads batch_summary.json (baseline) and batch_refactor_compressed.json,
the raw run JSONs, and, for item 3, the files each stopped run left in its sandbox (re-
running the hidden suite there is local pytest, model-free; the verdict is identical to
the global_ok already recorded, the point is to see WHICH tests fail).

    py -3.12 noise_check.py
"""
import io
import json
import re
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

import checker
from tasks import task_refactor

REPO = Path(__file__).resolve().parent
KNOWN = ("plan_complete", "no_tool_call", "max_steps", "error")


def load(p):
    return json.loads((REPO / p).read_text(encoding="utf-8"))


def tot(r):
    return r["tokens_total"]["input"] + r["tokens_total"]["output"]


def main():
    base = {r["seed"]: r for r in load("batch_summary.json")["runs"]
            if r["task_id"] == "refactor" and 1000 <= r["seed"] <= 1019}
    comp = {r["seed"]: r for r in load("batch_refactor_compressed.json")["runs"] if r["task_id"] == "refactor"}

    # ---- 1. end_reason by arm ----
    print("1. END REASONS BY ARM (refactor, seeds 1000-1019)")
    print(f"   {'arm':12}" + "".join(f"{k:>15}" for k in KNOWN) + f"{'other':>8}{'runs':>6}")
    for name, arm in (("baseline", base), ("compressed", comp)):
        c = Counter(r["end_reason"] for r in arm.values())
        other = sum(v for k, v in c.items() if k not in KNOWN)
        print(f"   {name:12}" + "".join(f"{c[k]:>15}" for k in KNOWN) + f"{other:>8}{len(arm):>6}")
    for name, arm in (("baseline", base), ("compressed", comp)):
        ntc = sorted(s for s, r in arm.items() if r["end_reason"] == "no_tool_call")
        print(f"   {name} no_tool_call seeds: {ntc or '-'}  "
              f"(global_ok: {[arm[s]['global_ok'] for s in ntc] or '-'})")

    # ---- 2. the final reply ----
    print("\n2. FINAL NO-TOOL REPLY")
    print("   NOT AVAILABLE. When a run ends with no_tool_call, harness.run_task stops without storing")
    print("   that reply (only replies that carried a tool call are logged, as step 'thought').")
    print("   Whether it was a sign-off, a question or a garbled reply cannot be read from these logs.")

    # ---- 3. were the files complete at the stop? ----
    print("\n3. STATE OF THE FILES WHEN THE RUN STOPPED (failed no_tool_call runs)")
    for s, r in sorted(comp.items()):
        if r["end_reason"] != "no_tool_call" or r["global_ok"] is True:
            continue
        wd = Path(r["workdir"])
        print(f"   --- seed {s}: sandbox {wd}")
        if not wd.is_dir():
            print("       sandbox no longer on disk; cannot inspect")
            continue
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = checker.global_success(task_refactor, wd, show_output=True)
        failed = sorted(set(re.findall(r"FAILED \S+::(\w+)", buf.getvalue())))
        summary = [l for l in buf.getvalue().splitlines() if re.search(r"\d+ (passed|failed)", l)][-1:]
        print(f"       hidden suite on the files as left: {summary[0].strip() if summary else '?'}")
        print(f"       failing tests: {failed or '-'}")
        src = (wd / "inventory.py").read_text(encoding="utf-8") if (wd / "inventory.py").is_file() else ""
        for fn in ("low_stock", "summarize"):
            m = re.search(rf"^def {fn}\(.*?(?=^def |^if __name__|\Z)", src, re.M | re.S)
            body = m.group(0).strip() if m else "(not found)"
            print(f"       {fn} as left on disk:\n          " + body.replace("\n", "\n          "))
        verdict = ("files appear complete at stop, lost only on format" if ok
                   else "task genuinely unfinished at stop")
        print(f"       >>> {verdict}")

    # ---- 4. headline both ways ----
    print("\n4. HEADLINE")
    pairs = sorted(set(base) & set(comp))
    b_ok = sum(base[s]["global_ok"] is True for s in pairs)
    c_ok = sum(comp[s]["global_ok"] is True for s in pairs)
    print(f"   (a) as-is:  compressed {c_ok}/{len(pairs)}  vs  baseline {b_ok}/{len(pairs)}")

    artifact = {s for s in pairs if "no_tool_call" in (base[s]["end_reason"], comp[s]["end_reason"])}
    keep = [s for s in pairs if s not in artifact
            and base[s]["end_reason"] != "error" and comp[s]["end_reason"] != "error"]
    b2 = sum(base[s]["global_ok"] is True for s in keep)
    c2 = sum(comp[s]["global_ok"] is True for s in keep)
    bt = sum(tot(base[s]) for s in keep) / len(keep)
    ct = sum(tot(comp[s]) for s in keep) / len(keep)
    print(f"   (b) excluding seeds with no_tool_call in EITHER arm ({sorted(artifact)}), treated as "
          f"harness artifacts:")
    print(f"       compressed {c2}/{len(keep)}  vs  baseline {b2}/{len(keep)}")
    print(f"       mean tokens/run on these {len(keep)} paired seeds: baseline {bt:,.0f}  compressed {ct:,.0f}"
          f"  ->  {(ct - bt) / bt:+.1%} (summary calls included)")
    print("   (b) drops seeds by an outcome that only occurred in one arm. It answers 'what if no_tool_call "
          "were fixed', not 'what did compression do'; (a) is the measured result.")


if __name__ == "__main__":
    main()
