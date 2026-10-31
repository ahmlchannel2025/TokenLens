"""tokenlens - see where an AI coding agent's tokens go.

Reads an agent run log (HVA harness JSON, a generic step log, or Claude Code
OpenTelemetry output) and reports context resend vs new output, the per-step
context-growth curve, and -- where the log has goal boundaries -- a
boundary-compression projection.

From the Hierarchy-Aware Context Compression study: across 63.4M tokens, ~98% of a
step-wise agent's tokens are context re-sent each step, not new work.
"""
from .analysis import analyze, Analysis, Run, Step
from . import readers

__version__ = "0.1.0"
__all__ = ["analyze", "Analysis", "Run", "Step", "readers", "__version__"]
