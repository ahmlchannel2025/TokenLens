# The tasks directory

The harness imports its task definitions from a `tasks/` package that is not in this
repository, because the files were not available when it was assembled. Until they are
added here, under `harness/tasks/`, the harness will not run.

Four files are expected:

    harness/tasks/__init__.py
    harness/tasks/task_pipeline.py
    harness/tasks/task_calc.py
    harness/tasks/task_refactor.py

Each task module defines `TASK_ID`, a string; `SPEC`, the task description the agent
is shown; `HIDDEN_TESTS`, a string of pytest source that the agent never sees and that
the checker runs to decide success; and optionally `setup_files(seed)`, which returns
a mapping of filenames to contents to place in the working directory before the agent
starts, so that a seed varies the input data without changing the task.
