"""The tokenlens command.

    tokenlens analyze <log.json>          read a log file, format detected automatically
    tokenlens analyze <log.json> --json   print the numbers as JSON
    tokenlens analyze -                   read a log from standard input

It reports where an agent's tokens went: context resent on each step against new
output, how the context grew over the run, and, when the log marks goal boundaries,
an estimate of what compressing at those boundaries would save.
"""
from __future__ import annotations

import argparse
import json
import sys

from .analysis import analyze
from . import readers

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
AMBER, GREEN = "\033[33m", "\033[32m"


def _fmt(n: float) -> str:
    n = round(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def _color(s: str) -> bool:
    return s and sys.stdout.isatty()


def cmd_analyze(args) -> int:
    try:
        if args.path == "-":
            runs = readers.from_json_or_text(sys.stdin.read())
        else:
            runs = readers.load(args.path)
        a = analyze(runs)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"tokenlens: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            "runs": a.n_runs, "steps": a.n_steps,
            "total_tokens": a.total, "input_tokens": a.total_in, "output_tokens": a.total_out,
            "resend_pct": round(a.resend_pct, 1),
            "curve": a.curve,
            "has_boundaries": a.have_boundaries,
            "projected_total": a.projected_total if a.have_boundaries else None,
            "projected_saving_pct": round(a.saving_pct, 1) if a.have_boundaries else None,
        }, indent=2))
        return 0

    use = _color(not args.no_color)
    amber = AMBER if use else ""
    green = GREEN if use else ""
    bold = BOLD if use else ""
    dim = DIM if use else ""
    r = RESET if use else ""

    print()
    print(f"  {bold}TokenLens{r}  {dim}{a.n_runs} run(s), {a.n_steps} steps{r}")
    print()
    print(f"  {amber}context resend (input){r}  {bold}{a.resend_pct:.1f}%{r}  "
          f"{dim}({_fmt(a.total_in)} tokens){r}")
    print(f"  {green}new output{r}              {100 - a.resend_pct:.1f}%  "
          f"{dim}({_fmt(a.total_out)} tokens){r}")
    print(f"  total                   {_fmt(a.total)}")
    print()
    print("  context size by step position (mean input tokens/step):")
    _print_curve(a.curve, amber, r)
    print()
    if a.have_boundaries:
        print(f"  boundary-compression projection  {dim}(upper bound, not safe-to-apply){r}")
        print(f"    current    {_fmt(a.total)}")
        print(f"    projected  {green}{_fmt(a.projected_total)}{r}   "
              f"(~{a.saving_pct:.0f}% fewer tokens)")
        print(f"    {dim}Compression can cut task success; verify against an independent{r}")
        print(f"    {dim}check before applying. See the HVA study.{r}")
    else:
        print(f"  {dim}no goal boundaries in this log -> no compression projection.{r}")
        print(f"  {dim}(Claude Code telemetry has no plan structure; add goal/boundary{r}")
        print(f"  {dim}fields in a generic log to enable it.){r}")
    print()
    return 0


def _print_curve(curve, color, reset):
    if not curve:
        print("    (no steps)")
        return
    mx = max(c["mean"] for c in curve) or 1
    for c in curve:
        bar = "#" * max(1, round(28 * c["mean"] / mx))
        print(f"    steps {c['label']:>6}  {color}{bar}{reset} {_fmt(c['mean'])}  {reset}(n={c['n']})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tokenlens", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    an = sub.add_parser("analyze", help="analyze a run log")
    an.add_argument("path", help="path to a log file, or - for stdin")
    an.add_argument("--json", action="store_true", help="machine-readable output")
    an.add_argument("--no-color", action="store_true", help="disable ANSI color")
    an.set_defaults(func=cmd_analyze)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
