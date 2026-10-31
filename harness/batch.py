"""batch.py - run each task n times and aggregate. Orchestration only.

harness.py and checker.py are used as-is. Each run gets:
  - its own fresh sandbox outside the repo;
  - its own seed (seed_base + i). The seed changes ONLY the data files the task puts in
    the sandbox (task.setup_files(seed), e.g. a sample CSV). Task spec, starter code and
    hidden tests are the same for every seed.

Outputs:
  runs/<task>_<batch id>_run<i>_seed<seed>.json   raw run dict per run (or the error)
  batch_summary.json    one combined file for analyze.ipynb; every run and every step
                        carries task_id. Rewritten after every run, so an interrupted
                        batch still leaves the runs finished so far.

Usage (from the repo root, with ANTHROPIC_API_KEY / ANTHROPIC_MODEL set):
  py -3.12 batch.py --n 20 --seed-base 1000                 # all three tasks
  py -3.12 batch.py --n 5 --tasks calc refactor             # a subset
  py -3.12 batch.py --tasks refactor --n 20 --seed-base 1000 --compress --summary batch_refactor_compressed.json
  py -3.12 batch.py --tasks refactor --seeds 1009 1012 1014 --compress --summary batch_refactor_compressed_fix.json
"""
from __future__ import annotations

import json
import re
import tempfile
import time
import traceback
from pathlib import Path

import checker
import harness

REPO_ROOT = Path(__file__).resolve().parent


class SetupFilesAgent(harness.BaseAgent):
    """Wraps ANY backend and puts the task's setup files into the sandbox.

    Why a wrapper: run_task() insists on an empty workdir (so files from another run
    can't leak in), and harness.py stays unchanged. make_plan() is the first call
    run_task makes after it has checked the workdir is empty, so the files are written
    there. Everything else is passed straight through to the real backend.
    """

    def __init__(self, inner: harness.BaseAgent, workdir: Path, files: dict[str, str]):
        self.inner, self.workdir, self.files = inner, Path(workdir), files
        self.name = inner.name
        # Same text for every seed (only file CONTENTS differ), so the prompt is fixed.
        self.note = (f"\n\nYour working directory already contains: {', '.join(sorted(files))}."
                     if files else "")

    def make_plan(self, spec):
        for rel, content in self.files.items():
            (self.workdir / rel).write_text(content, encoding="utf-8")
        return self.inner.make_plan(spec + self.note)

    def start(self, spec, plan_tree, env_note):
        if self.files:
            # The harness's env note says the workdir "starts empty"; here it doesn't.
            # (If that wording ever changes, this is a no-op and self.note still applies.)
            env_note = env_note.replace("Your working directory starts empty; use paths",
                                        "Use paths")
        self.inner.start(spec + self.note, plan_tree, env_note)

    def next_turn(self, observation):
        return self.inner.next_turn(observation)

    def summarize(self, prompt):
        return self.inner.summarize(prompt)

    def compress_context(self, progress):
        self.inner.compress_context(progress)


# ---------------------------------------------------------------------------
# Objective per-step failure, and recovery steps. Uses only what the harness observed,
# never the model's words or self-reports.
# ---------------------------------------------------------------------------

_EXIT_CODE = re.compile(r"^exit code: (-?\d+)")


def step_failed(step: dict) -> bool:
    """True if the step objectively failed:
      - the harness rejected it (jump-ahead write, unknown tool): executed == False
      - a tool error (bad path, timeout, ...): observation starts with "ERROR"
      - run_shell exited non-zero (failing tests, tracebacks, ...)
    """
    obs = step["observation"]
    if not step["executed"] or obs.startswith("ERROR"):
        return True
    m = _EXIT_CODE.match(obs)
    return bool(m) and int(m.group(1)) != 0


def mark_recovery(steps: list[dict]) -> list[bool]:
    """is_recovery_step for each step: it comes AFTER an objectively failed step within
    the same substep. Once a substep has failed, every later action in that substep is
    diagnose / fix / retry work. The failed step itself is not a recovery step (unless
    an earlier failure in the substep already made it one)."""
    flags, failed_in = [], set()
    for s in steps:
        key = (s["goal_index"], s["substep_index"])
        flags.append(key in failed_in)
        if step_failed(s):
            failed_in.add(key)
    return flags


def local_all_ok(steps: list[dict]) -> bool:
    # Same rule as explore.ipynb: every step reported AND reported True.
    reports = [s["local_check_self_report_untrusted"] for s in steps]
    return bool(reports) and all(r is True for r in reports)


# ---------------------------------------------------------------------------
# The batch
# ---------------------------------------------------------------------------

def _one_run(task, agent, i, seed, batch_id, runs_dir, run_kwargs) -> dict:
    """One run of one task. Never raises: failures are recorded in the row."""
    workdir = Path(tempfile.mkdtemp(prefix=f"agentrun_{task.TASK_ID}_s{seed}_"))
    files = task.setup_files(seed) if hasattr(task, "setup_files") else {}
    wrapped = SetupFilesAgent(agent, workdir, files)
    t0 = time.time()
    run, error = None, None
    try:
        run = harness.run_task(task, wrapped, workdir, **run_kwargs)
    except Exception:
        error = traceback.format_exc()

    try:
        global_ok = checker.global_success(task, workdir)
    except Exception:
        global_ok = None
        error = (error or "") + "\nCHECKER ERROR:\n" + traceback.format_exc()

    raw_path = runs_dir / f"{task.TASK_ID}_{batch_id}_run{i:02d}_seed{seed}.json"
    harness.save_run(run if run is not None else
                     {"task_id": task.TASK_ID, "seed": seed, "workdir": str(workdir),
                      "end_reason": "error", "error": error}, raw_path)
    return _row(task.TASK_ID, i, seed, workdir, raw_path, run, error, global_ok,
                round(time.time() - t0, 1))


def _row(task_id, i, seed, workdir, raw_path, run, error, global_ok, seconds) -> dict:
    """One summary row. Shared by live batches and rebuild_summary()."""
    raw_path = Path(raw_path)
    steps = run["steps"] if run else []
    recovery = mark_recovery(steps)
    return {
        "task_id": task_id,
        "run": i,
        "seed": seed,
        "workdir": str(workdir),
        "raw_json": str(raw_path.relative_to(REPO_ROOT) if raw_path.is_relative_to(REPO_ROOT)
                        else raw_path),
        "seconds": seconds,
        "end_reason": "error" if run is None else run["end_reason"],
        "error": error,
        "global_ok": global_ok,                                        # independent checker
        "local_all_ok": None if run is None else local_all_ok(steps),  # UNTRUSTED
        "n_steps": len(steps),
        "tokens_total": run["tokens_total"] if run else None,   # incl. summary calls if compressed
        "compressed": bool(run and run.get("compression", {}).get("enabled")),
        "summary_tokens": (run or {}).get("compression", {}).get("summary_tokens"),
        "summaries": [c["summary"] for c in (run or {}).get("compression", {}).get("summaries", [])],
        "final_no_tool_reply": (run or {}).get("final_no_tool_reply"),   # only for no_tool_call
        "steps": [{
            "task_id": task_id,
            "step_idx": s["step_idx"],
            "goal_index": s["goal_index"],
            "substep_index": s["substep_index"],
            "tag": s["tag"],
            "tool": s["action"]["tool"],
            "tokens_in": s["tokens_in"],
            "tokens_out": s["tokens_out"],
            "local_self_report": s["local_check_self_report_untrusted"],  # UNTRUSTED
            "step_failed": step_failed(s),            # objective, from the observation
            "is_recovery_step": rec,
            "attempted_full_solution": s["attempted_full_solution"],
        } for s, rec in zip(steps, recovery)],
    }


def is_account_error(error_text: str | None) -> bool:
    """True for errors that will fail EVERY later run too (no credit, bad/revoked key),
    as opposed to one run's bad luck. Matched on the recorded traceback: the credit
    error is an ordinary 400, distinguishable only by its message."""
    if not error_text:
        return False
    return ("credit balance is too low" in error_text
            or "anthropic.AuthenticationError" in error_text
            or "anthropic.PermissionDeniedError" in error_text)


def run_batch(tasks, agent: harness.BaseAgent, n: int = None, seed_base: int = None,
              summary_path="batch_summary.json", runs_dir="runs", overwrite=False, seeds=None,
              **run_kwargs) -> dict:
    """Run each task in `tasks` (one task module or a list) n times; save and return the
    combined summary. Seeds seed_base .. seed_base+n-1 are reused for every task, or pass
    an explicit list `seeds=[...]` instead of n/seed_base (e.g. to re-run chosen seeds).

    A crashed run never aborts the batch: it is recorded with end_reason="error" and
    the traceback. The checker still runs on whatever that run left in its sandbox,
    since the checker only looks at files. ONE exception: an account-level error (no
    credit, invalid key) stops the batch, because every later run would fail the same
    way; the summary is then saved with complete=False and stopped_early set.

    Refuses to overwrite an existing summary file unless overwrite=True, so re-running
    a notebook cell can't silently replace earlier results.
    run_kwargs go to harness.run_task (e.g. max_steps, max_actions_per_substep).
    """
    tasks = tasks if isinstance(tasks, (list, tuple)) else [tasks]
    if seeds is None:
        if n is None or seed_base is None:
            raise ValueError("pass n and seed_base, or an explicit seeds=[...] list")
        seeds = [seed_base + i for i in range(n)]
    seeds = list(seeds)
    n = len(seeds)
    out = REPO_ROOT / summary_path
    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} already exists. Pass a new summary_path (CLI: --summary), "
                              "or overwrite=True if you really mean to replace it.")
    batch_id = time.strftime("batch%Y%m%d_%H%M%S")
    runs_dir = Path(REPO_ROOT / runs_dir)
    runs_dir.mkdir(exist_ok=True)
    summary = {
        "batch_id": batch_id,
        "task_ids": [t.TASK_ID for t in tasks],
        "backend": agent.name,
        "n_per_task": n,
        "seed_base": seed_base,
        "seeds": seeds,
        "run_kwargs": run_kwargs,
        "definitions": {
            "step_failed": step_failed.__doc__.strip(),
            "is_recovery_step": mark_recovery.__doc__.strip(),
            "local_all_ok": "every step has a self-report and all are True (UNTRUSTED)",
        },
        "complete": False,
        "runs": [],
    }

    total = n * len(tasks)
    for task in tasks:
        for i, seed in enumerate(seeds):
            row = _one_run(task, agent, i, seed, batch_id, runs_dir, run_kwargs)
            summary["runs"].append(row)
            out.write_text(json.dumps(summary, indent=2), encoding="utf-8")  # checkpoint
            print(f"[{len(summary['runs'])}/{total}] {task.TASK_ID} seed={row['seed']} "
                  f"end={row['end_reason']} steps={row['n_steps']} global_ok={row['global_ok']} "
                  f"local_all_ok={row['local_all_ok']}", flush=True)
            if is_account_error(row["error"]):
                summary["stopped_early"] = row["error"].strip().splitlines()[-1][:300]
                out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
                print(f"\nSTOPPED EARLY after {len(summary['runs'])}/{total} runs: account-level "
                      f"API error, every later run would fail too.\n  {summary['stopped_early']}\n"
                      f"partial summary (complete=False) -> {out}")
                return summary

    summary["complete"] = True
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"summary -> {out}")
    return summary


def rebuild_summary(batch_id: str, summary_path, runs_dir="runs", overwrite=False) -> dict:
    """Recreate a batch's summary from its raw run JSONs in runs/ (e.g. after the summary
    file was overwritten). global_ok is re-computed by running checker.py on each run's
    sandbox, which must still exist; the checker is model-free and only reads files, so
    this costs nothing and gives the same verdict. `seconds` is not recoverable (None).
    """
    import importlib
    out = REPO_ROOT / summary_path
    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} already exists (overwrite=True to replace it).")
    pat = re.compile(rf"^(\w+?)_{re.escape(batch_id)}_run(\d+)_seed(\d+)\.json$")
    found = sorted((m.group(1), int(m.group(2)), int(m.group(3)), p)
                   for p in Path(REPO_ROOT / runs_dir).iterdir() if (m := pat.match(p.name)))
    if not found:
        raise FileNotFoundError(f"no raw runs for {batch_id} in {runs_dir}/")
    task_ids = list(dict.fromkeys(t for t, *_ in found))
    rows, backend = [], None
    for task_id, i, seed, path in found:
        raw = json.loads(path.read_text(encoding="utf-8"))
        run = None if raw.get("end_reason") == "error" else raw
        backend = backend or (run or {}).get("backend")
        task = importlib.import_module(f"tasks.task_{task_id}")
        workdir = Path(raw["workdir"])
        global_ok = checker.global_success(task, workdir) if workdir.is_dir() else None
        rows.append(_row(task_id, i, seed, workdir, path, run, raw.get("error"), global_ok, None))
        print(f"[{len(rows)}/{len(found)}] {task_id} seed={seed} global_ok={global_ok}", flush=True)
    summary = {
        "batch_id": batch_id,
        "task_ids": task_ids,
        "backend": backend,
        "n_per_task": max(sum(r["task_id"] == t for r in rows) for t in task_ids),
        "seed_base": min(r["seed"] for r in rows),
        "run_kwargs": None,   # not stored in raw runs
        "definitions": {
            "step_failed": step_failed.__doc__.strip(),
            "is_recovery_step": mark_recovery.__doc__.strip(),
            "local_all_ok": "every step has a self-report and all are True (UNTRUSTED)",
        },
        "complete": True,
        "rebuilt_from_raw_runs": True,
        "runs": rows,
    }
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"rebuilt summary -> {out}")
    return summary


ALL_TASKS = ("pipeline", "calc", "refactor")


def load_tasks(ids=ALL_TASKS):
    import importlib
    return [importlib.import_module(f"tasks.task_{t}") for t in ids]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed-base", type=int, default=1000)
    ap.add_argument("--tasks", nargs="+", default=list(ALL_TASKS), choices=ALL_TASKS)
    ap.add_argument("--summary", default="batch_summary.json",
                    help="output file, relative to the repo root (an existing file is never replaced "
                         "unless --overwrite)")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--compress", action="store_true",
                    help="run_task(compress_at_boundaries=True); default is the baseline")
    ap.add_argument("--seeds", type=int, nargs="+",
                    help="explicit seeds to run (overrides --n / --seed-base)")
    args = ap.parse_args()
    extra = {"compress_at_boundaries": True} if args.compress else {}
    run_batch(load_tasks(args.tasks), harness.AnthropicAgent(), n=args.n, seed_base=args.seed_base,
              seeds=args.seeds, summary_path=args.summary, overwrite=args.overwrite, **extra)
