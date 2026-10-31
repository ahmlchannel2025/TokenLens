# Hierarchy-Aware Context Compression for Coding Agents

A study of where a step-wise coding agent's tokens actually go, and what happens
when you compress its context at plan boundaries.

**TL;DR** — Across three task shapes and 63.4M tokens, **97.9% of an agent's
tokens are context being re-sent on every step**, not new work. Replacing completed
goals' transcripts with short summaries at each goal boundary cut total tokens
**54%** on a maintenance task — but it dropped task success from 20/20 to 18/20 by
inducing a specific, characterized failure (the agent stops early while aware it
isn't done). The saving is real; "at equal success" is not yet.

Live demo: **[TokenLens](#tokenlens-the-demo)** — drop in a run log, see the split.

---

## The finding, in four results

1. **Context resend dominates.** Input (context re-sent each call) is **97.9%** of
   all tokens, nearly constant across task shapes (pipeline 98.0%, calc 97.8%,
   refactor 97.8%). Per-step input climbs ~5k → ~40k across a run because each step
   re-pays for the whole history before it. Verification and recovery are a few
   percent. **The cost to attack is context resend, not self-verification.**

2. **Boundary-concentrated failure does not generalize.** We hypothesized agent
   failures cluster at goal boundaries. They don't: the boundary/internal failure-rate
   ratio is 0.45 (pipeline), 0.00 (calc), 1.11 (refactor). On the one task where it
   leans the right way, the confidence intervals overlap — not established.

3. **Agents did not game their own checks.** Across 60 runs, zero cases of an agent
   passing all its own step checks while failing an independent test suite. On these
   tasks the self-reports were honest.

4. **Boundary compression: a large saving with a specific cost.** −54.2% tokens on
   refactor (20 seeds, summary-call cost included, cheaper on every seed), but global
   success fell 20/20 → 18/20. The lost runs are **not** information loss — the agent
   retained its instructions, tools, and plan, and re-acquired every signature it
   referenced. The failure is premature termination: a compressed context reads as
   finished, and the model ends its turn. It is a low-rate *stochastic* failure
   (the failing seed moves between re-runs), and it persists even with an explicit
   "you are not done, call a tool" footer in the prompt.

Full write-up: see the preprint PDF in this repo.

---

## How it works

A minimal, instrumented ReAct coding agent:

- **`harness.py`** — the agent loop. The model plans (goals → ordered substeps),
  then executes substeps in order, one tool call per step (`write_file`,
  `run_shell`, `finish_substep`). Execution is forced to follow the plan so that
  "boundary" (first step of a new goal) is a fact about execution, not the model's
  claim. Logs per-step token counts, tags, and the agent's (untrusted) self-report.
  Optional `compress_at_boundaries` replaces finished goals' transcripts with
  one-line summaries.

- **`checker.py`** — the independent success check. Runs a hidden pytest suite
  against the agent's output, judged by **exit code only**. It never calls a model
  and never reads the agent's logs or self-reports. This separation is the point:
  an agent that grades itself can't be trusted to report that compression preserved
  correctness, so the arbiter of correctness lives entirely outside the agent.

- **`batch.py`** — runs N seeds per task, aggregates to `batch_summary.json`.

- **`tasks/`** — three self-authored tasks with hidden test suites, chosen so
  "boundary" means a different kind of seam in each: `pipeline` (module splits +
  shared schema), `calc` (one file, state-linked capabilities), `refactor` (edits
  to existing code — the maintenance shape).

### Run it

```bash
pip install -r requirements.txt           # anthropic, pytest, pyyaml, matplotlib
export ANTHROPIC_API_KEY=...  ANTHROPIC_MODEL=claude-...
python batch.py --n 20                     # all three tasks -> batch_summary.json
python batch.py --tasks refactor --n 20 --compress --summary compressed.json
```

Then open `analyze.ipynb` (per-task failure rates, token splits) or
`analyze_compression.ipynb` (the compression comparison).

---

## TokenLens (the demo)

A web tool that reads an agent run log and shows where the tokens go: context
resend vs new output, the per-step context-growth curve, and a boundary-compression
projection. It reproduces the paper's numbers exactly when fed the harness logs.

It accepts two formats:

- **This harness** — drop a `batch_summary.json` or any single `runs/*.json`.
- **Generic step log** — a JSON array any Claude API agent loop can emit:
  ```json
  [{"step":0,"input_tokens":3100,"output_tokens":160,"goal":0,"boundary":false}]
  ```
  Append one row per step from `response.usage`; the optional `goal`/`boundary`
  fields unlock the compression projection.

**Claude Code** users: per-call tokens come through OpenTelemetry (the
`claude_code.api_request` event), not a file you can drop into the web tool. The
companion CLI (below) reads that stream directly.

The tool labels context resend as **resend**, not "wasted" — resend is how a
stateless model remembers what it did. Only the portion a boundary summary could
replace is marked compressible, and the projection is an upper bound, not a
safe-to-apply result (see result #4).

---

## tokenlens (the CLI)

`pip install tokenlens` — runs the same analysis over your own logs from the
command line, and (for Claude Code) reads the OpenTelemetry stream directly instead
of a dropped file.

```bash
tokenlens analyze batch_summary.json       # harness or generic JSON
tokenlens analyze run_log.json --format generic
```

Status: in development. The OpenTelemetry reader for Claude Code is built against
the documented `claude_code.api_request` schema and needs validation against a real
Claude Code telemetry dump before it's load-bearing.

---

## What this is, and isn't

This is a small, honest study on one model (Claude Haiku) with self-authored tasks,
run on a student budget. The context-cost result is robust and task-invariant. The
compression saving is measured but **not** established at equal success — the gate
failed, and confirming whether the mitigation restores parity needs more runs than
the budget allowed. The boundary-failure-location question (result #2) remains open
on maintenance tasks: the point estimate leans the right way but the intervals
overlap. Confirming the context-cost fraction and the termination failure on a
public agent benchmark is the natural next step.
