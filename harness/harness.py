"""harness.py - a minimal, instrumented ReAct coding agent that executes its plan in order.

  1. PLAN  Ask the agent for a plan tree (goals -> ordered substeps). Frozen after this.
  2. LOOP  Walk the plan's substeps IN ORDER. For the current substep the agent does
           reason -> act (exactly one tool call) -> observe, until it calls
           `finish_substep` (or the substep's action budget runs out); then the harness
           moves to the next substep. The run ends after the last substep.
  3. LOG   One record per action; see run_task().
  Optional (off by default): compress_at_boundaries replaces finished goals' transcripts
  with one-line summaries when a new goal starts; see run_task().

Why force the order: left alone, agents write the whole program in their first action,
so the executed structure has nothing to do with the plan and "boundary" steps mean
nothing. Here the harness owns the substep cursor, so every step's (goal, substep) is a
fact about execution, not the agent's claim. And write_file is capped at one logical
unit per call, so the agent can't dump the full solution early (see _units_changed).

This file never decides whether the task succeeded. That is checker.py's job, and
checker.py never reads anything this file produces.

Swapping backends: subclass BaseAgent. run_task() only talks to BaseAgent and executes
the tools itself, so a GPT/Gemini/local backend never touches the loop or the tool code.
"""
from __future__ import annotations

import ast
import json
import os
import platform
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import anthropic


def _load_windows_user_env(names=("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")) -> None:
    """A process only sees env vars that existed when it started, and VS Code / Jupyter
    often keep running in the background. On Windows, fill any missing var from the
    values saved for the user account (HKCU\\Environment), so a newly set key works
    without restarting anything. Never writes the key anywhere."""
    if os.name != "nt":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg:
        for name in names:
            if not os.environ.get(name):
                try:
                    os.environ[name] = winreg.QueryValueEx(reg, name)[0]
                except FileNotFoundError:
                    pass


_load_windows_user_env()  # runs on `import harness`, i.e. before the notebook's env check

REPO_ROOT = Path(__file__).resolve().parent
MAX_OBS_CHARS = 4000        # longer tool output is cut (head + tail) before the model sees it
SHELL_TIMEOUT_S = 60
MAX_UNITS_PER_WRITE = 1     # top-level functions/classes one write_file may add or change


# ---------------------------------------------------------------------------
# What a backend hands back to the loop. Backend-neutral on purpose.
# ---------------------------------------------------------------------------

@dataclass
class Action:
    tool: str                  # "write_file", "run_shell" or "finish_substep"
    args: dict                 # {"path", "content"}, {"cmd"} or {"summary"}
    intent: str                # this step's own stated subgoal ("" for finish_substep)


@dataclass
class AgentTurn:
    action: Action | None      # None when the agent stopped
    end_reason: str | None     # None = keep going; else "no_tool_call", "refusal", "max_tokens"
    prev_step_ok: bool | None  # agent's verdict on its PREVIOUS action (untrusted self-report)
    thought: str               # visible text before the tool call; often empty on newer models
    tokens_in: int
    tokens_out: int


class BaseAgent(ABC):
    """A backend: plans once, then produces one action per turn.

    Contract every backend must keep so that logs stay comparable across backends:
      - make_plan returns [{"goal": str, "substeps": [str, ...]}, ...] and token counts.
      - next_turn returns at most ONE action (one tool call = one turn).
      - every action reports prev_step_ok about the action before it.
      - the observation passed to next_turn is plain text. It is the result of the last
        action, and may carry harness notices (e.g. "now on substep 1.0"); on the first
        turn it is the first substep notice. Backends just relay it to the model.
    """
    name = "base"

    @abstractmethod
    def make_plan(self, spec: str) -> tuple[list[dict], int, int]:
        """Return (plan_tree, tokens_in, tokens_out)."""

    @abstractmethod
    def start(self, spec: str, plan_tree: list[dict], env_note: str) -> None:
        """Reset conversation state for a new solve."""

    @abstractmethod
    def next_turn(self, observation: str) -> AgentTurn:
        """Relay the observation; return the agent's next turn."""

    # --- Only needed for run_task(..., compress_at_boundaries=True). ---

    def summarize(self, prompt: str) -> tuple[str, int, int]:
        """One standalone model call (no tools, no conversation): (text, tokens_in, tokens_out)."""
        raise NotImplementedError(f"{type(self).__name__} does not support compression")

    def compress_context(self, progress: str) -> None:
        """Drop the conversation so far. The next next_turn() starts a fresh conversation
        whose first message is: original task + plan, then `progress`, then the observation."""
        raise NotImplementedError(f"{type(self).__name__} does not support compression")


# ---------------------------------------------------------------------------
# Prompts and tool schemas. Plain JSON Schema, so other backends can reuse them.
# ---------------------------------------------------------------------------

PLAN_PROMPT = """\
Before writing any code, plan how you will solve the task below. Return the high-level
goals in the order you will pursue them; each goal has an ordered list of concrete
substeps. You will later execute the substeps one at a time, in this order, and may
only work on the current one - so make each substep small (one function, one test run).
Do not solve the task yet.

TASK:
{spec}"""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "substeps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["goal", "substeps"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["goals"],
    "additionalProperties": False,
}

SOLVE_SYSTEM = f"""\
You are a coding agent. You execute YOUR PLAN one substep at a time, in order. The
harness tells you which substep is current. Work ONLY on the current substep; when it is
done, call `finish_substep` and the harness will give you the next one.

Each response makes exactly ONE tool call, then you see its result.
Rule: one write_file call may add or change at most {MAX_UNITS_PER_WRITE} top-level
function or class (imports and constants don't count; test files are exempt). To grow
a file, rewrite it with the existing code plus the one new unit. Calls that break this
rule are rejected.

Every call also carries:
  intent        one sentence: what this single action should achieve (not on finish_substep)
  prev_step_ok  did your PREVIOUS call achieve its intent, judging from its result?
                "yes" or "no"; "n/a" on your very first call

{{env_note}}"""

# prev_step_ok is a string enum rather than a nullable boolean because enums behave the
# same across model providers.
_PREV_OK = {"type": "string", "enum": ["yes", "no", "n/a"],
            "description": "Did your previous call achieve its intent?"}
_INTENT = {"type": "string", "description": "What this single action should achieve."}

TOOLS = [
    {
        "name": "write_file",
        "description": "Create or overwrite a text file. `path` is relative to your working directory. "
                       f"At most {MAX_UNITS_PER_WRITE} new or changed top-level function/class per call.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"},
                           "intent": _INTENT, "prev_step_ok": _PREV_OK},
            "required": ["path", "content", "intent", "prev_step_ok"],
        },
    },
    {
        "name": "run_shell",
        "description": f"Run a shell command in your working directory (timeout {SHELL_TIMEOUT_S}s). "
                       "Returns exit code, stdout and stderr.",
        "input_schema": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}, "intent": _INTENT, "prev_step_ok": _PREV_OK},
            "required": ["cmd", "intent", "prev_step_ok"],
        },
    },
    {
        "name": "finish_substep",
        "description": "Declare the CURRENT substep complete and move to the next one.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}, "prev_step_ok": _PREV_OK},
            "required": ["summary", "prev_step_ok"],
        },
    },
]


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------

class AnthropicAgent(BaseAgent):
    def __init__(self, model: str | None = None, max_tokens: int = 16000):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        model = model or os.environ.get("ANTHROPIC_MODEL")
        if not api_key or not model:
            raise RuntimeError("Set ANTHROPIC_API_KEY and ANTHROPIC_MODEL in the environment.")
        # Don't accept brotli: some environments (e.g. Anaconda's old `brotlipy`) ship a
        # brotli module whose API the HTTP client can't use, which surfaces as a
        # misleading "Connection error". gzip works everywhere.
        self.client = anthropic.Anthropic(api_key=api_key,
                                          default_headers={"Accept-Encoding": "gzip, deflate"})
        self.model = model
        self.max_tokens = max_tokens
        self.name = f"anthropic:{model}"
        self.system = ""
        self.preamble = ""
        self.messages: list[dict] = []
        self.pending_tool_use_id: str | None = None

    def make_plan(self, spec):
        # Separate one-shot call; structured output guarantees parseable JSON.
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": PLAN_PROMPT.format(spec=spec)}],
            output_config={"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise RuntimeError(f"No plan returned (stop_reason={resp.stop_reason}).")
        return json.loads(text)["goals"], resp.usage.input_tokens, resp.usage.output_tokens

    def start(self, spec, plan_tree, env_note):
        self.system = SOLVE_SYSTEM.format(env_note=env_note)
        self.preamble = (f"TASK:\n{spec}\n\nYOUR PLAN (fixed; executed in order):\n"
                         f"{format_plan(plan_tree)}")
        self.base_preamble = self.preamble   # kept verbatim through any compression
        self.messages = []
        self.pending_tool_use_id = None

    def summarize(self, prompt):
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4000,  # room for any default thinking; the summary itself is short
            messages=[{"role": "user", "content": prompt}],
        )
        text = " ".join(b.text for b in resp.content if b.type == "text").strip()
        return text, resp.usage.input_tokens, resp.usage.output_tokens

    def compress_context(self, progress):
        # A fresh conversation, not an edit of the old one: earlier turns (and their
        # thinking blocks) are dropped whole, which every model accepts.
        self.preamble = f"{self.base_preamble}\n\n{progress}"
        self.messages = []
        self.pending_tool_use_id = None

    def next_turn(self, observation):
        if not self.messages:  # first turn: task + plan + first substep notice
            self.messages.append({"role": "user", "content": f"{self.preamble}\n\n{observation}"})
        elif self.pending_tool_use_id is not None:
            self.messages.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": self.pending_tool_use_id, "content": observation}]})
        else:  # previous turn had no tool call
            self.messages.append({"role": "user", "content": observation})
        self.pending_tool_use_id = None

        # No `thinking` parameter: the model comes from an env var, and settings valid on one
        # model are rejected by others. Each model runs with its own default.
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system,
            tools=TOOLS,
            # At most one tool call per response => one API call == one action, so the
            # per-step token counts below belong to exactly one action.
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            messages=self.messages,
        )
        # Append the content unchanged (thinking blocks included): newer models reject
        # histories whose earlier assistant turns were edited.
        self.messages.append({"role": "assistant", "content": resp.content})

        thought = "\n".join(b.text for b in resp.content if b.type == "text")
        # input_tokens is the WHOLE context resent this turn, so it grows step by step.
        # output_tokens includes any thinking tokens.
        usage = (resp.usage.input_tokens, resp.usage.output_tokens)
        tool_use = next((b for b in resp.content if b.type == "tool_use"), None)

        if resp.stop_reason in ("refusal", "max_tokens"):
            return AgentTurn(None, resp.stop_reason, None, thought, *usage)
        if tool_use is None:
            if not thought.strip():
                # Make "replied with no text" visible in the log instead of an empty string.
                thought = (f"[no text in reply; content block types: "
                           f"{[b.type for b in resp.content]}; stop_reason={resp.stop_reason}]")
            return AgentTurn(None, "no_tool_call", None, thought, *usage)

        self.pending_tool_use_id = tool_use.id
        args = dict(tool_use.input)
        prev_ok = {"yes": True, "no": False}.get(args.pop("prev_step_ok", None))
        intent = str(args.pop("intent", ""))
        return AgentTurn(Action(tool_use.name, args, intent), None, prev_ok, thought, *usage)


# ---------------------------------------------------------------------------
# The "one logical unit per write" rule
# ---------------------------------------------------------------------------

def _units_changed(old: str, new: str) -> int:
    """How many top-level functions/classes `new` adds or changes relative to `old`.

    A "unit" is a top-level def / async def / class; its source text (decorators
    included) is compared with the previous version of the file. Imports, constants and
    `if __name__ == "__main__":` blocks are free, so file scaffolding is never blocked.
    If `new` doesn't parse, fall back to counting top-level def/class NAMES that weren't
    in `old`, so a broken full-program dump is still caught.
    """
    def units(src: str) -> dict[str, str] | None:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return None
        return {n.name: ast.get_source_segment(src, n) or "" for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}

    new_units = units(new)
    if new_units is None:
        pattern = r"^(?:async\s+def|def|class)\s+(\w+)"
        return len(set(re.findall(pattern, new, re.M)) - set(re.findall(pattern, old, re.M)))
    old_units = units(old) or {}
    return sum(1 for name, src in new_units.items() if old_units.get(name) != src)


def _is_test_file(path: str) -> bool:
    name = Path(path).name
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


# ---------------------------------------------------------------------------
# Tool execution. Lives here, not in the backend, so every backend runs identical tools.
#
# NOT a security sandbox: run_shell can still `cd ..` or use absolute paths. What it does:
# run everything inside `workdir`, refuse write_file paths outside it, and hide
# ANTHROPIC_* env vars from the agent's shell. Use a container if you need real isolation.
# ---------------------------------------------------------------------------

def _write_file(workdir: Path, path, content) -> str:
    if not isinstance(path, str) or not isinstance(content, str):
        return "ERROR: write_file needs string `path` and `content`."
    target = (workdir / path).resolve()
    if not target.is_relative_to(workdir):
        return f"ERROR: {path!r} is outside the working directory."
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {target.relative_to(workdir).as_posix()}"


def _run_shell(workdir: Path, cmd) -> str:
    if not isinstance(cmd, str):
        return "ERROR: run_shell needs a string `cmd`."
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    # Put this interpreter's directory first on PATH, so `python` in the agent's shell is
    # the same Python (with PyYAML/pytest) that runs the harness and the checker.
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    try:
        p = subprocess.run(cmd, shell=True, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=SHELL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {SHELL_TIMEOUT_S}s"
    return _truncate(f"exit code: {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")


def _truncate(s: str) -> str:
    # Keep head and tail; the tail is where test summaries and tracebacks end up.
    if len(s) <= MAX_OBS_CHARS:
        return s
    half = MAX_OBS_CHARS // 2
    return f"{s[:half]}\n...[{len(s) - MAX_OBS_CHARS} chars truncated]...\n{s[-half:]}"


def _prepare_workdir(workdir) -> Path:
    wd = Path(workdir).resolve()
    # Keep the agent outside the repo: from inside it, a relative path like ../tasks/
    # would reach the hidden tests. The workdir also must not contain the repo.
    if wd == REPO_ROOT or REPO_ROOT in wd.parents or wd in REPO_ROOT.parents:
        raise ValueError(f"workdir must be outside the repo ({REPO_ROOT}); got {wd}")
    wd.mkdir(parents=True, exist_ok=True)
    if any(wd.iterdir()):
        raise ValueError(f"workdir must be empty, so files from another run can't leak in: {wd}")
    return wd


# ---------------------------------------------------------------------------
# Boundary compression (only used when run_task(..., compress_at_boundaries=True))
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """\
Below is the work log of ONE completed goal of a coding task. Write ONE line (at most
about 60 words) stating: the goal; the files and functions/classes it created or changed;
and any contract facts a later goal must respect (names, signatures, field names, formats,
constants, behaviors). No narrative, no advice, no markdown.

LOG:
{log}"""

_LOG_CONTENT_CHARS = 1500   # per written file, in the summarizer's input
_LOG_OBS_CHARS = 600        # per observation
_LOG_MAX_CHARS = 20000      # whole log


def _goal_log(g: int, plan_tree: list[dict], steps: list[dict], substeps: list[dict]) -> str:
    """Plain-text log of goal g for the summarizer: its substeps and every action in it."""
    out = [f"GOAL {g}: {plan_tree[g]['goal']}"]
    for sub in substeps:
        if sub["goal_index"] == g:
            out.append(f"substep {g}.{sub['substep_index']} "
                       f"({plan_tree[g]['substeps'][sub['substep_index']]}) ended by {sub['ended_by']}; "
                       f"agent's summary: {sub['summary'] or '-'}")
    for st in steps:
        if st["goal_index"] != g:
            continue
        args = st["action"]["args"]
        if st["action"]["tool"] == "write_file":
            what = f"write {args.get('path')}:\n{str(args.get('content', ''))[:_LOG_CONTENT_CHARS]}"
        else:
            what = f"{st['action']['tool']}: {args.get('cmd', args)}"
        out.append(f"[step {st['step_idx']}] intent: {st['intent']}\n{what}\n"
                   f"result: {st['observation'][:_LOG_OBS_CHARS]}")
    text = "\n".join(out)
    if len(text) > _LOG_MAX_CHARS:
        half = _LOG_MAX_CHARS // 2
        text = f"{text[:half]}\n...[log truncated]...\n{text[-half:]}"
    return text


def _files_on_disk(workdir: Path) -> str:
    files = [p for p in sorted(workdir.rglob("*")) if p.is_file()
             and not any(part in ("__pycache__", ".pytest_cache") for part in p.parts)]
    return ", ".join(f"{p.relative_to(workdir).as_posix()} ({p.stat().st_size} B)" for p in files) or "(none)"


def _env_note() -> str:
    shell = "cmd.exe" if os.name == "nt" else "/bin/sh"
    return (f"Environment: {platform.system()}; run_shell uses {shell}. `python` is Python "
            f"{platform.python_version()} with pytest installed. "
            "Your working directory starts empty; use paths relative to it.")


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def run_task(task, agent: BaseAgent, workdir, max_steps: int = 60,
             max_actions_per_substep: int = 8, compress_at_boundaries: bool = False) -> dict:
    """Plan, then execute the plan's substeps in order. Returns the run dict.

    One STEP = one write_file / run_shell action, including actions the harness
    rejected (they cost tokens and are the jump-ahead data). `finish_substep` calls are
    not steps; they only move the cursor. Per-step fields:
      step_idx
      goal_index, substep_index  the substep the harness had the agent on (a fact, not
                   a claim: the agent can't choose it)
      tag          "boundary" = first step under a goal different from the previous
                   step's goal; "internal" = same goal as the previous step. Step 0 is
                   "internal": no goal precedes it, so N goals with steps = N-1 boundaries.
      attempted_full_solution
                   True if this write_file tried to add/change more than
                   MAX_UNITS_PER_WRITE top-level functions/classes in one go. Such
                   writes are rejected, not executed (see `executed`).
      units_in_write  the count behind that flag (None for non-writes), so you can
                   re-threshold later
      local_check_self_report_untrusted
                   True/False/None. The agent's OWN claim, made on its next turn after
                   seeing the result, that this action achieved its intent. UNTRUSTED:
                   nobody verified it. None = never reported (run ended first).

    A substep ends when the agent calls finish_substep or after max_actions_per_substep
    actions (then the harness moves on and says so). The run ends after the last
    substep, at max_steps, or if the model stops calling tools.

    compress_at_boundaries (default False = baseline, nothing below changes):
      each time the cursor enters a NEW goal, the goal just finished is summarized in
      one line by ONE extra model call (agent.summarize), and the agent's conversation
      is replaced by: original task + plan (verbatim; the task spec holds the contract),
      the one-line summaries of all completed goals, the list of files on disk, and the
      current observation. The new goal's own transcript then grows normally until the
      next boundary. Summary-call tokens are added to tokens_total AND reported
      separately under run["compression"], so savings are measured net of their cost.
    """
    workdir = _prepare_workdir(workdir)
    plan_tree, plan_in, plan_out = agent.make_plan(task.SPEC)
    agent.start(task.SPEC, plan_tree, _env_note())

    # Flatten the plan into an ordered work list of (goal_index, substep_index).
    cursor_list = [(g, s) for g, goal in enumerate(plan_tree) for s in range(len(goal["substeps"]))]

    def notice(i: int) -> str:
        if i >= len(cursor_list):
            return "[harness] That was the last substep. The run is over."
        g, s = cursor_list[i]
        return (f"[harness] CURRENT SUBSTEP {g}.{s} (goal {g}: {plan_tree[g]['goal']}): "
                f"{plan_tree[g]['substeps'][s]}\nWork only on this substep. "
                "Call finish_substep when it is done.")

    steps: list[dict] = []
    substeps: list[dict] = []   # one entry per substep visited, in order
    tokens_in, tokens_out = plan_in, plan_out
    cur = 0
    actions_here = 0
    observation = notice(0)
    end_reason = "max_steps"
    awaiting_report = False     # True right after a step, until the agent's next turn
    final_no_tool_reply = None  # set only if the run ends with end_reason="no_tool_call"
    compression_log: list[dict] = []

    def compress_if_boundary(finished_goal: int) -> None:
        """Called right after the cursor moved. Acts only in compression mode, only when
        the cursor has entered a new goal (not at the end of the plan)."""
        nonlocal tokens_in, tokens_out
        if not compress_at_boundaries or cur >= len(cursor_list) or cursor_list[cur][0] == finished_goal:
            return
        log = _goal_log(finished_goal, plan_tree, steps, substeps)
        summary, s_in, s_out = agent.summarize(SUMMARY_PROMPT.format(log=log))
        summary = " ".join(summary.split()) or "(empty summary)"   # force one line
        tokens_in += s_in
        tokens_out += s_out
        compression_log.append({"goal_index": finished_goal, "summary": summary,
                                "tokens_in": s_in, "tokens_out": s_out,
                                "before_step_idx": len(steps)})
        lines = "\n".join(f"- goal {c['goal_index']}: {c['summary']}" for c in compression_log)
        # A block that only lists COMPLETED work can read as "the task is finished", and the
        # model may sign off instead of acting. Always end with what is still to do.
        g_now, s_now = cursor_list[cur]
        remaining = "; ".join(f"goal {g}: {plan_tree[g]['goal']}" for g in range(g_now, len(plan_tree)))
        agent.compress_context(
            f"PROGRESS SO FAR. These goals are COMPLETE; their full transcripts were replaced by "
            f"one-line summaries (read the files with run_shell if you need details):\n{lines}\n"
            f"FILES ON DISK NOW: {_files_on_disk(workdir)}\n\n"
            f"STATUS: NOT COMPLETE. Goals remaining: {remaining}.\n"
            f"Your next action MUST be a tool call for the current substep {g_now}.{s_now}: "
            f"{plan_tree[g_now]['substeps'][s_now]}. Do not summarize or sign off; call a tool now.")

    while cur < len(cursor_list) and len(steps) < max_steps:
        turn = agent.next_turn(observation)
        tokens_in += turn.tokens_in
        tokens_out += turn.tokens_out

        # The verdict the agent gives in THIS turn is about its PREVIOUS call. Back-fill it
        # only if that call was a step (not a finish_substep, which isn't logged as one).
        if awaiting_report:
            steps[-1]["local_check_self_report_untrusted"] = turn.prev_step_ok
            awaiting_report = False

        if turn.end_reason is not None:
            end_reason = turn.end_reason
            if end_reason == "no_tool_call":
                final_no_tool_reply = turn.thought   # the reply that ended the run, verbatim
            break

        g, s = cursor_list[cur]
        a = turn.action

        if a.tool == "finish_substep":
            substeps.append({"goal_index": g, "substep_index": s, "actions": actions_here,
                             "ended_by": "agent", "summary": a.args.get("summary", "")})
            cur, actions_here = cur + 1, 0
            observation = notice(cur)
            compress_if_boundary(g)
            continue

        # ---- a real action (write_file / run_shell / unknown) ----
        units = None
        attempted_full = False
        executed = True
        if a.tool == "write_file":
            path, content = a.args.get("path"), a.args.get("content")
            if isinstance(path, str) and isinstance(content, str) and not _is_test_file(path):
                target = (workdir / path).resolve()
                old = target.read_text(encoding="utf-8") if target.is_file() else ""
                units = _units_changed(old, content)
                attempted_full = units > MAX_UNITS_PER_WRITE
            if attempted_full:
                executed = False
                observation = (f"REJECTED: this write adds/changes {units} top-level functions/"
                               f"classes; the limit is {MAX_UNITS_PER_WRITE} per call. Implement "
                               f"ONLY the current substep {g}.{s}: "
                               f"{plan_tree[g]['substeps'][s]}")
            else:
                observation = _write_file(workdir, path, content)
        elif a.tool == "run_shell":
            observation = _run_shell(workdir, a.args.get("cmd"))
        else:
            executed = False
            observation = f"ERROR: unknown tool {a.tool!r}"

        prev_goal = steps[-1]["goal_index"] if steps else None
        steps.append({
            "step_idx": len(steps),
            "goal_index": g,
            "substep_index": s,
            "tag": "boundary" if steps and g != prev_goal else "internal",
            "attempted_full_solution": attempted_full,
            "units_in_write": units,
            "executed": executed,
            "intent": a.intent,
            "action": {"tool": a.tool, "args": a.args},
            "observation": observation,
            "thought": turn.thought,
            "tokens_in": turn.tokens_in,
            "tokens_out": turn.tokens_out,
            "local_check_self_report_untrusted": None,  # filled in on the next turn
        })
        awaiting_report = True

        actions_here += 1
        if actions_here >= max_actions_per_substep:
            substeps.append({"goal_index": g, "substep_index": s, "actions": actions_here,
                             "ended_by": "action_budget", "summary": ""})
            cur, actions_here = cur + 1, 0
            observation += (f"\n\n[harness] Action budget for substep {g}.{s} is used up; "
                            f"moving on.\n{notice(cur)}")
            compress_if_boundary(g)

    if cur >= len(cursor_list):
        end_reason = "plan_complete"

    if compress_at_boundaries:
        extra = {"compression": {
            "enabled": True,
            "summaries": compression_log,
            # Already INCLUDED in tokens_total; listed separately so the cost is visible.
            "summary_tokens": {"input": sum(c["tokens_in"] for c in compression_log),
                               "output": sum(c["tokens_out"] for c in compression_log)},
        }}
    else:
        extra = {}   # baseline run dict is exactly as before
    if final_no_tool_reply is not None:
        extra["final_no_tool_reply"] = final_no_tool_reply

    return {
        "task_id": task.TASK_ID,
        "backend": agent.name,
        "workdir": str(workdir),
        "max_steps": max_steps,
        "end_reason": end_reason,
        "plan_tree": plan_tree,
        "substeps": substeps,
        "steps": steps,
        # Totals include the planning call and finish_substep turns, which have no step.
        "tokens_total": {"input": tokens_in, "output": tokens_out},
        "plan_tokens": {"input": plan_in, "output": plan_out},
        **extra,
    }


def save_run(run: dict, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Display helpers (used by the agent prompt and the notebook)
# ---------------------------------------------------------------------------

def format_plan(plan_tree: list[dict]) -> str:
    lines = []
    for g, goal in enumerate(plan_tree):
        lines.append(f"goal {g}: {goal['goal']}")
        lines += [f"  {g}.{s}  {sub}" for s, sub in enumerate(goal["substeps"])]
    return "\n".join(lines)


def format_steps(run: dict, intent_width: int = 50) -> str:
    ok = {True: "yes", False: "NO", None: "-"}
    rows = [f"{'step':>4} {'g.s':>5} {'tag':<9} {'tool':<10} {'jump':>4} {'tok_in':>7} "
            f"{'tok_out':>7} {'local_ok*':>9}  intent"]
    for s in run["steps"]:
        gs = f"{s['goal_index']}.{s['substep_index']}"
        jump = "JUMP" if s["attempted_full_solution"] else ""
        rows.append(f"{s['step_idx']:>4} {gs:>5} {s['tag']:<9} {s['action']['tool']:<10} {jump:>4} "
                    f"{s['tokens_in']:>7} {s['tokens_out']:>7} "
                    f"{ok[s['local_check_self_report_untrusted']]:>9}  {s['intent'][:intent_width]}")
    rows.append("* local_ok = agent's own UNTRUSTED self-report.  JUMP = write rejected for "
                "changing too many units at once.")
    return "\n".join(rows)
