# Add your tasks/ directory here

The harness imports task definitions from a `tasks/` package that is NOT in this
commit (it lives on the author's machine). Before the harness will run, add:

    harness/tasks/__init__.py
    harness/tasks/task_pipeline.py
    harness/tasks/task_calc.py
    harness/tasks/task_refactor.py

Each task module defines: TASK_ID (str), SPEC (str, shown to the agent),
HIDDEN_TESTS (str of pytest source, never shown to the agent), and optionally
setup_files(seed) -> dict[str, str].
