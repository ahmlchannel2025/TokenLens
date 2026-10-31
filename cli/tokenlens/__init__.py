"""tokenlens reports where an AI coding agent's tokens went.

It reads an agent run log, in the harness format, a generic step log, or Claude Code's
OpenTelemetry output, and reports the share that is context resent on each step against
new output, how the context grows over the run, and, where the log marks goal
boundaries, an estimate of what compressing at those boundaries would save.

The figure behind it, from the study in the repository, is that across 63.4 million
tokens about 98% of a step-wise agent's tokens were context resent each step rather
than new work.
"""
from .analysis import analyze, Analysis, Run, Step
from . import readers

__version__ = "0.1.0"
__all__ = ["analyze", "Analysis", "Run", "Step", "readers", "__version__"]
