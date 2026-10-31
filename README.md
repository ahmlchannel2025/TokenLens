# Hierarchy-Aware Context Compression for Coding Agents

A coding agent that works through a task over many steps has no memory between the
calls it makes to the model, so on every call it resends the whole transcript of
what it has done so far. This repository measures how much that costs, tests one way
to reduce it, and reports a failure the reduction introduces.

Across three task shapes and 63.4 million tokens, 97.9% of an agent's tokens were
input being resent on each step rather than new output, and this fraction barely
moved between task types. Replacing the transcript of completed goals with short
summaries at each goal boundary cut total tokens by 54% on a code-maintenance task,
but it lowered the task-success rate from 20 of 20 runs to 18 of 20 by making the
agent stop early while it still had work to do. The saving is real; whether it comes
at equal success is not something this work establishes.

The live demo, TokenLens, takes an agent run log and shows the same breakdown:
context resend against new output, how the context grows over a run, and how much
boundary compression might save.

## What the experiments found

The dominant cost is resend, not verification. A common assumption in work on agent
efficiency is that the waste worth attacking is the agent re-checking its own work.
On these tasks that is misdirected. Input, meaning the context resent on each call,
was 97.9% of all tokens, almost constant across task shapes (98.0% on the pipeline
task, 97.8% on calc, 97.8% on refactor), while verification and recovery together
came to a few percent. Within a run, input per step climbed from roughly five
thousand tokens early to tens of thousands late, because each step pays again for
everything before it.

We had expected failures to cluster at the boundaries between goals, and they did
not, at least not in general. The ratio of boundary to internal failure rate was
0.45 on the pipeline task and 0.00 on calc, so failures there were internal rather
than at the seams. On the refactor task the ratio was 1.11, in the direction we
expected, but the confidence intervals for the two rates overlap, so we do not treat
the difference as established.

We also looked for cases where the agent passed all of its own step checks while
failing an independent test suite, since an agent that satisfies each subgoal while
missing the overall goal is a known worry. Across 60 runs there were none. On these
tasks the agent's own reports of success tracked the independent check.

Boundary compression saved a large fraction of tokens and cost some task success. On
the refactor task over 20 matched seeds, compression reduced total tokens by 54.2%,
counting the cost of the summary-generating calls, and every one of the 20 runs was
cheaper. Global success, measured by a hidden test suite that never sees the agent's
own claims, fell from 20 of 20 to 18 of 20. The runs that failed did not fail from
lost information: the agent kept its instructions, tools and plan, and had re-read
the function signatures its later steps referred to. What the compressed context
changed was how the conversation read. A tidy block of completed-goal summaries looks
like a finished piece of work, and the model answered with a closing remark instead
of its next action. The failure is a low-rate random event rather than a property of
particular inputs; across two re-run batches the seed that failed moved. It also
persisted when the summary ended with an explicit instruction that the task was not
complete and the next move had to be a tool call.

The full account is in the preprint under `paper/`.

## How it works

The agent is a small ReAct loop in `harness/harness.py`. The model first produces a
plan of goals and ordered substeps, then executes the substeps in order, one tool
call per step, choosing among writing a file, running a shell command, and declaring
the current substep finished. Execution is forced to follow the plan so that a
boundary, the first step of a new goal, is a fact about what the agent did rather
than something it claims. Each step's token counts, its boundary-or-internal tag, and
the agent's own untrusted report of whether the step worked are logged. The optional
compression mode replaces finished goals' transcripts with one-line summaries when a
new goal begins.

The success check is in `harness/checker.py`, and it is deliberately kept apart from
the agent. It runs a hidden pytest suite against whatever files the agent left behind
and reports only the exit code. It never calls a model and never reads the agent's
logs or self-reports. The separation is the point of the method: an agent that grades
its own work cannot be trusted to report that compression preserved correctness, so
the judge of correctness sits entirely outside it.

`harness/batch.py` runs a number of seeds per task and writes the results to
`batch_summary.json`. The three tasks live in `harness/tasks/` and were written so
that a boundary means a different kind of seam in each: the pipeline task splits work
across modules joined by a shared schema, calc builds a stateful calculator in one
file where later operations depend on earlier state, and refactor changes existing,
already-passing code, which is the maintenance case.

To run it:

    pip install -r harness/requirements.txt
    export ANTHROPIC_API_KEY=...  ANTHROPIC_MODEL=claude-...
    python harness/batch.py --n 20
    python harness/batch.py --tasks refactor --n 20 --compress --summary compressed.json

The notebooks `analyze.ipynb` and `analyze_compression.ipynb` read the resulting
summaries and produce the failure-rate tables, the token splits, and the compression
comparison.

The `tasks/` directory is not included in this repository, because it was not among
the files available when the repository was assembled. The harness imports it, so the
code will not run until it is added. See `harness/tasks_MISSING.md` for the four files
it expects and what each one defines.

## TokenLens

`web/index.html` is a browser tool that reads an agent run log and shows where the
tokens went: context resend against new output, the mean input per step by position
in the run, and, when the log marks goal boundaries, a projection of what boundary
compression would save. Fed the harness logs, it reproduces the numbers above.

It reads two formats. The first is the harness output itself, either a
`batch_summary.json` or a single run under `runs/`. The second is a generic step log,
a JSON array of objects with `step`, `input_tokens` and `output_tokens`, and
optionally `goal` and `boundary`, which any agent loop built on the Claude API can
produce by recording `response.usage` after each call. The optional goal and boundary
fields are what make the compression projection possible.

The tool labels resent context as resend rather than waste. Resend is how a stateless
model keeps track of what it has done, so calling it waste would be wrong; only the
part a boundary summary could replace is marked compressible, and that figure is an
upper bound rather than a result that is safe to apply, for the reason the compression
experiment shows.

## The command-line tool

`cli/` holds a small package, installable with `pip install -e cli`, that runs the
same analysis from the command line. It reads the harness and generic formats, and it
reads Claude Code's OpenTelemetry output. Claude Code reports per-call token counts
through the `claude_code.api_request` event, which carries `input_tokens`,
`output_tokens`, `cache_read_tokens` and related fields; the reader is built against
that documented schema. It has not yet been checked against a real Claude Code
telemetry capture, which is the one thing needed before it should be relied on.
Claude Code's telemetry has no plan structure, so for its logs the tool shows the
resend split and the growth curve but reports that there are no boundaries to compress
rather than inventing them.

    pip install -e cli
    tokenlens analyze batch_summary.json
    tokenlens analyze run_log.json --json

## Scope

This is a small study. It uses one model, Claude Haiku, and three tasks written for
the purpose, run on a limited budget. The result about where the tokens go is robust
and holds across the three task shapes. The compression saving is measured but not
established at equal success, because the success rate dropped and confirming whether
the mitigation recovers it would take more runs than the budget allowed. The question
of whether failures concentrate at boundaries on maintenance tasks is left open: the
estimate points the expected way but the intervals overlap. Reproducing the resend
figure and the termination failure on an established public benchmark is the natural
next step.
