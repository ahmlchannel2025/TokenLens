"""Turn each supported log format into the common Run and Step shape.

Three formats are read, and detect() chooses among them from the parsed JSON. The
harness format is a batch summary with a runs list, or a single run with a steps list.
The generic format is a JSON array of per-step objects with input_tokens and
output_tokens, which an agent loop built on the Claude API can produce from
response.usage. The third is Claude Code's OpenTelemetry output, handled in otel.py.
"""
from __future__ import annotations

import json
from pathlib import Path

from .analysis import Run, Step


def load(path: str | Path) -> list[Run]:
    """Read a file, detect its format, and return a list of Run."""
    return from_json_or_text(Path(path).read_text(encoding="utf-8"))


def from_json_or_text(text: str) -> list[Run]:
    """Parse text that is either one JSON value or JSON Lines (OTel console output)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Not a single JSON value: it may be JSON Lines (one event per line), which is
        # how OTel console/file exporters write. Hand the raw text to the otel reader.
        from .otel import read_otel_jsonl
        return read_otel_jsonl(text)
    return from_json(data)


def from_json(data) -> list[Run]:
    fmt = detect(data)
    if fmt == "harness":
        return read_harness(data)
    if fmt == "generic":
        return read_generic(data)
    if fmt == "otel":
        from .otel import read_otel_events
        return read_otel_events(data)
    raise ValueError(
        "Unrecognized log. Expected an HVA harness summary (runs[]), a single run "
        "(steps[]), a generic step array ({step,input_tokens,output_tokens}), or "
        "Claude Code OpenTelemetry events."
    )


def detect(data) -> str:
    if isinstance(data, dict):
        if isinstance(data.get("runs"), list) or isinstance(data.get("steps"), list):
            return "harness"
        # A single OTel export is often {"resourceLogs"/"resourceMetrics": [...]}.
        if "resourceLogs" in data or "resourceMetrics" in data:
            return "otel"
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            if "input_tokens" in first or "tokens_in" in first:
                return "generic"
            # A list of OTel log records / events.
            if first.get("name") == "claude_code.api_request" or "attributes" in first:
                return "otel"
    return "unknown"


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

def read_harness(data: dict) -> list[Run]:
    if isinstance(data.get("runs"), list):
        raw_runs = [r for r in data["runs"] if r.get("end_reason") != "error" and isinstance(r.get("steps"), list)]
    elif isinstance(data.get("steps"), list):
        raw_runs = [data]
    else:
        raise ValueError("Harness log has neither runs[] nor steps[].")
    if not raw_runs:
        raise ValueError("Harness log has no usable runs (all errored or no steps).")

    runs = []
    for r in raw_runs:
        steps = [
            Step(idx=s.get("step_idx", i), tin=s["tokens_in"], tout=s["tokens_out"],
                 goal=s.get("goal_index"), boundary=(s.get("tag") == "boundary"))
            for i, s in enumerate(r["steps"])
            if isinstance(s.get("tokens_in"), int) and isinstance(s.get("tokens_out"), int)
        ]
        if not steps:
            continue
        tt = r.get("tokens_total")
        runs.append(Run(steps=steps,
                        total_in=tt.get("input") if isinstance(tt, dict) else None,
                        total_out=tt.get("output") if isinstance(tt, dict) else None))
    if not runs:
        raise ValueError("Harness runs found, but no steps carried tokens_in/tokens_out.")
    return runs


# --------------------------------------------------------------------------- #
# generic
# --------------------------------------------------------------------------- #

def read_generic(data: list) -> list[Run]:
    steps = []
    for i, s in enumerate(data):
        tin = s.get("input_tokens", s.get("tokens_in"))
        tout = s.get("output_tokens", s.get("tokens_out"))
        if not isinstance(tin, int) or not isinstance(tout, int):
            continue
        steps.append(Step(idx=s.get("step", i), tin=tin, tout=tout,
                          goal=s.get("goal"), boundary=bool(s.get("boundary"))))
    if not steps:
        raise ValueError("Generic log parsed, but no rows had integer input/output tokens.")
    return [Run(steps=steps)]   # generic logs have no separate authoritative total
