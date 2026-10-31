"""The token analysis, shared by every input format.

This is the same computation the web tool runs. It stays in the standard library so
it cannot drift from the figures it reports. It produces three things: the share of
total tokens that is input resent on each step, the mean input per step by position
in the run, and an estimate of what compressing the context at goal boundaries would
save, which is an upper bound rather than a result that is safe to apply.

Each reader turns its format into a list of Step, so the analysis never needs to know
where a log came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Step:
    idx: int
    tin: int            # input tokens for this step (the context re-sent)
    tout: int           # output tokens generated this step
    goal: int | None = None
    boundary: bool = False


@dataclass
class Run:
    steps: list[Step]
    # Authoritative totals when the source has them (they include plan + bookkeeping
    # turns that have no step row). None => fall back to summing the steps.
    total_in: int | None = None
    total_out: int | None = None


@dataclass
class Analysis:
    total_in: int
    total_out: int
    total: int
    resend_pct: float
    curve: list[dict] = field(default_factory=list)   # [{label, mean, n}]
    have_boundaries: bool = False
    projected_total: int = 0
    saving_pct: float = 0.0            # % of TOTAL tokens the projection removes
    n_steps: int = 0
    n_runs: int = 0


_BUCKETS = [(0, 4), (5, 9), (10, 19), (20, 39), (40, 10 ** 9)]


def analyze(runs: list[Run]) -> Analysis:
    steps = [s for r in runs for s in r.steps]
    if not steps:
        raise ValueError("No steps to analyze.")

    # Headline totals: prefer authoritative run totals; else sum step tokens.
    have_auth = all(r.total_in is not None for r in runs)
    if have_auth:
        tin = sum(r.total_in for r in runs)
        tout = sum(r.total_out for r in runs)
    else:
        tin = sum(s.tin for s in steps)
        tout = sum(s.tout for s in steps)
    total = tin + tout

    # Mean input per step, by position in the run.
    curve = []
    for a, b in _BUCKETS:
        sel = [s.tin for s in steps if a <= s.idx <= b]
        if sel:
            label = f"{a}-{b}" if b < 10 ** 9 else f"{a}+"
            curve.append({"label": label, "mean": sum(sel) / len(sel), "n": len(sel)})

    # Compression projection, computed on the step-input basis (what compression
    # actually touches), then expressed as a % and applied to the true total so it
    # stays consistent with the headline figure.
    have_boundaries = any(s.boundary for s in steps)
    step_in = sum(s.tin for s in steps)
    saving_frac = 0.0
    if have_boundaries and step_in:
        # The floor is the "fresh context" level a boundary reset returns to, so it must
        # come from the smallest-context steps, not the run average. Use the mean of the
        # cheapest few steps (the run's early/just-after-reset context).
        lows = sorted(s.tin for s in steps)[:max(1, len(steps) // 5)]
        floor = sum(lows) / len(lows)
        cap = max(floor * 2, floor + 1500)   # summaries + current goal's own transcript
        seen = False
        proj_step_in = 0
        for s in steps:
            if s.boundary:
                seen = True
            proj_step_in += min(s.tin, cap) if seen else s.tin
        saving_frac = (step_in - proj_step_in) / step_in   # fraction of INPUT removed

    projected_total = round(total - tin * saving_frac)
    saving_pct = (100 * (total - projected_total) / total) if total else 0.0

    return Analysis(
        total_in=tin, total_out=tout, total=total,
        resend_pct=(100 * tin / total) if total else 0.0,
        curve=curve, have_boundaries=have_boundaries,
        projected_total=projected_total, saving_pct=saving_pct,
        n_steps=len(steps), n_runs=len(runs),
    )
