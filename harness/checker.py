"""checker.py - the INDEPENDENT global success check.

WHY THIS FILE EXISTS AND WHY IT IS MODEL-FREE
---------------------------------------------
The project measures the gap between two signals:
  * LOCAL:  the agent's own claim that each step achieved its subgoal (logged by harness.py)
  * GLOBAL: whether the finished artifact actually does what the task asked (this file)

If the global verdict came from a model - the agent grading itself, or another LLM reading
the agent's transcript or self-reports - it would share failure modes with the thing it is
grading (same blind spots, same persuadable reading of logs). Then "local passed, global
failed" would just be one model disagreeing with another, and every result would be
circular. So the global verdict here comes from one thing only: the EXIT CODE of a hidden
pytest suite run against the files the agent left in its working directory.

RULES FOR THIS FILE (keep them when editing):
  1. No import of anthropic, harness, or any model client. Ever.
  2. Never read the run JSON, the plan, the steps, or any agent self-report.
  3. The verdict is `pytest exit code == 0`. Do not parse output to decide anything.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PYTEST_TIMEOUT_S = 300


def global_success(task, workdir, show_output=False) -> bool:
    """Run `task.HIDDEN_TESTS` against the agent's output in `workdir`. True iff pytest exits 0.

    `task` only needs two attributes: TASK_ID (str) and HIDDEN_TESTS (pytest source as a str).
    `show_output` prints pytest's output for a human; it never affects the verdict.
    """
    workdir = Path(workdir).resolve()

    # The tests are written to a fresh temp dir, NOT into the agent's workdir:
    #   - the agent never had a chance to see or edit them;
    #   - pytest does not load any conftest.py / pytest.ini the agent wrote (an agent-written
    #     conftest.py could otherwise rewrite results and force a pass).
    # The tests find the agent's files through the AGENT_WORKDIR env var and run the CLI
    # as a subprocess, i.e. they test real end-to-end behavior, not importable functions.
    with tempfile.TemporaryDirectory(prefix="hidden_tests_") as tmp:
        testdir = Path(tmp)
        (testdir / f"test_{task.TASK_ID}.py").write_text(task.HIDDEN_TESTS, encoding="utf-8")
        # An empty pytest.ini makes testdir the rootdir, so pytest stops searching parent
        # directories for config.
        (testdir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

        # The code under test runs with no model credentials, so it cannot phone a model.
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
        env["AGENT_WORKDIR"] = str(workdir)

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(testdir)],
                cwd=testdir,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PYTEST_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            if show_output:
                print(f"hidden suite timed out after {PYTEST_TIMEOUT_S}s -> FAIL")
            return False

    if show_output:
        print(proc.stdout + proc.stderr)
    # Exit code only. (pytest: 0 = all passed, 1 = failures, 5 = nothing collected, ...)
    return proc.returncode == 0
