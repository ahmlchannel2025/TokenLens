"""otel.py - read Claude Code OpenTelemetry output into the common Run/Step shape.

Field names are from the official Claude Code monitoring docs
(code.claude.com/docs/en/monitoring-usage). The `claude_code.api_request` event
carries, per API call:

    input_tokens           input tokens, EXCLUDING prompt-cache tokens
    output_tokens          output tokens
    cache_read_tokens      tokens read from cache
    cache_creation_tokens  tokens written to cache
    model, cost_usd, cost_usd_micros, duration_ms, session.id

One `api_request` event == one API call == one step, which is exactly the Step the
analysis wants.

IMPORTANT LIMITATION (by design, not a bug): Claude Code's telemetry has no plan or
goal structure. There are no goal boundaries in the stream, so a boundary-compression
projection cannot be made from a Claude Code log -- only the resend-vs-output split
and the per-call context-growth curve. Steps are returned with boundary=False and
goal=None, and the caller reports "no boundaries" rather than inventing them.

INPUT SHAPES handled:
  - JSON Lines from the `console` / file exporter: one record per line.
  - A parsed OTLP logs payload: {"resourceLogs": [...]} with nested scopeLogs/logRecords.
  - A plain list of event dicts (already extracted).

STATUS: built against the documented schema; validate against a real Claude Code
telemetry dump before relying on it. `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` emits
protobuf, which this reader does not parse -- use the JSON/console exporter, or an
OTLP/HTTP+JSON collector, to produce text this can read.
"""
from __future__ import annotations

import json

from .analysis import Run, Step

EVENT_NAME = "claude_code.api_request"

# How tokens_in is defined for the resend curve. Claude Code separates cache-read
# tokens from fresh input; both are context the model is billed to re-read each call,
# so by default we count input + cache_read as "context re-sent" to match what the
# harness measures (its input_tokens already include everything resent).
def _step_tokens(attrs: dict) -> tuple[int, int]:
    tin = _int(attrs.get("input_tokens")) + _int(attrs.get("cache_read_tokens"))
    tout = _int(attrs.get("output_tokens"))
    return tin, tout


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _is_api_request(name) -> bool:
    return name in (EVENT_NAME, "api_request")


def read_otel_jsonl(text: str) -> list[Run]:
    """Console/file exporter: one JSON record per line (lines that aren't JSON are skipped)."""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        attrs = _attrs_if_api_request(rec)
        if attrs is not None:
            events.append(attrs)
    return _runs_from_attr_dicts(events)


def read_otel_events(data) -> list[Run]:
    """A parsed OTLP payload ({'resourceLogs': ...}) or a plain list of event dicts."""
    events = []
    if isinstance(data, dict) and "resourceLogs" in data:
        for rl in data.get("resourceLogs", []):
            for sl in rl.get("scopeLogs", []):
                for rec in sl.get("logRecords", []):
                    attrs = _attrs_if_api_request(rec)
                    if attrs is not None:
                        events.append(attrs)
    elif isinstance(data, list):
        for rec in data:
            attrs = _attrs_if_api_request(rec)
            if attrs is not None:
                events.append(attrs)
    else:
        raise ValueError("OTel input is neither a resourceLogs payload nor a list of events.")
    return _runs_from_attr_dicts(events)


def _attrs_if_api_request(rec: dict) -> dict | None:
    """Return a flat {attr_name: value} dict if `rec` is an api_request event, else None.

    Handles both the flat console shape ({"name":..., "input_tokens":...}) and the
    OTLP log-record shape ({"name"/"body":..., "attributes":[{"key","value":{...}}]}).
    """
    if not isinstance(rec, dict):
        return None
    name = rec.get("name") or rec.get("event.name") or _body_str(rec.get("body"))
    # OTLP attributes come as a list of {key, value:{stringValue|intValue|...}}.
    if isinstance(rec.get("attributes"), list):
        flat = {a.get("key"): _otlp_val(a.get("value")) for a in rec["attributes"] if isinstance(a, dict)}
        name = name or flat.get("event.name")
        if not _is_api_request(name):
            return None
        return flat
    # Flat console record: the fields sit directly on the object.
    if _is_api_request(name) or ("input_tokens" in rec and "output_tokens" in rec):
        return rec
    return None


def _body_str(body) -> str | None:
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        return body.get("stringValue")
    return None


def _otlp_val(v):
    if not isinstance(v, dict):
        return v
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return v[k]
    return None


def _runs_from_attr_dicts(events: list[dict]) -> list[Run]:
    if not events:
        raise ValueError(
            f"No '{EVENT_NAME}' events found. Enable telemetry with "
            "CLAUDE_CODE_ENABLE_TELEMETRY=1 and OTEL_LOGS_EXPORTER=console (or otlp to a "
            "JSON collector), then capture the output. Protobuf (grpc) output is not readable here."
        )
    # The stream may interleave sessions; split by session.id so each run is one session.
    by_session: dict[str, list[dict]] = {}
    order: list[str] = []
    for a in events:
        sid = str(a.get("session.id", a.get("session_id", "default")))
        if sid not in by_session:
            by_session[sid] = []
            order.append(sid)
        by_session[sid].append(a)

    runs = []
    for sid in order:
        steps = []
        for i, a in enumerate(by_session[sid]):
            tin, tout = _step_tokens(a)
            steps.append(Step(idx=i, tin=tin, tout=tout, goal=None, boundary=False))
        if steps:
            runs.append(Run(steps=steps))   # no authoritative total; sum of steps is the total
    return runs
