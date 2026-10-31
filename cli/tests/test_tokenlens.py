"""Smoke tests: each reader produces sane analysis, and the headline math is stable."""
import json
import subprocess
import sys

from tokenlens import readers, analyze


def _run(steps):
    return readers.from_json(steps)


def test_generic_basic():
    log = [
        {"step": 0, "input_tokens": 3000, "output_tokens": 150, "goal": 0, "boundary": False},
        {"step": 1, "input_tokens": 5000, "output_tokens": 300, "goal": 0, "boundary": False},
        {"step": 2, "input_tokens": 8000, "output_tokens": 400, "goal": 1, "boundary": True},
    ]
    a = analyze(_run(log))
    assert a.n_steps == 3
    assert 90 < a.resend_pct < 99
    assert a.have_boundaries
    assert a.saving_pct > 0            # a boundary exists, so some saving is projected


def test_generic_no_boundary_no_projection():
    log = [{"step": i, "input_tokens": 3000 + i * 1000, "output_tokens": 200} for i in range(6)]
    a = analyze(_run(log))
    assert not a.have_boundaries
    assert a.saving_pct == 0.0
    assert a.projected_total == a.total


def test_harness_shape():
    run = {"task_id": "t", "end_reason": "plan_complete",
           "tokens_total": {"input": 100000, "output": 2000},
           "steps": [{"step_idx": i, "goal_index": 0, "tag": "boundary" if i == 2 else "internal",
                      "tokens_in": 3000 + i * 1500, "tokens_out": 200} for i in range(5)]}
    a = analyze(readers.from_json(run))
    # headline uses authoritative tokens_total, not the step sum
    assert a.total == 102000
    assert round(a.resend_pct, 1) == round(100 * 100000 / 102000, 1)


def test_otel_jsonl():
    lines = "\n".join(json.dumps(e) for e in [
        {"name": "claude_code.api_request", "input_tokens": 3000, "cache_read_tokens": 0, "output_tokens": 200, "session.id": "s1"},
        {"name": "claude_code.api_request", "input_tokens": 5000, "cache_read_tokens": 1000, "output_tokens": 300, "session.id": "s1"},
        {"name": "unrelated.event", "foo": 1},
    ])
    a = analyze(readers.from_json_or_text(lines))
    assert a.n_steps == 2
    # cache_read counts as resent context: step 2 input = 5000 + 1000
    assert a.total_in == 3000 + 6000
    assert not a.have_boundaries   # Claude Code telemetry has no plan structure


def test_unknown_format_errors():
    try:
        analyze(readers.from_json({"nonsense": True}))
    except ValueError:
        return
    assert False, "expected ValueError on unknown format"


def test_cli_runs():
    import tempfile, os
    log = [{"step": i, "input_tokens": 3000 + i * 800, "output_tokens": 150, "boundary": i == 2} for i in range(5)]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(log, f)
        path = f.name
    try:
        out = subprocess.run([sys.executable, "-m", "tokenlens.cli", "analyze", path, "--json"],
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        d = json.loads(out.stdout)
        assert d["steps"] == 5 and d["resend_pct"] > 90
    finally:
        os.unlink(path)
