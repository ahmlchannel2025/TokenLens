# tokenlens

A command-line tool that reads an AI coding agent's run log and reports where its
tokens went: how much was context resent on each step against new output the model
generated, how the context grew over the run, and, when the log records goal
boundaries, how much compressing the context at those boundaries might save.

It exists because of a measurement from the study in the parent repository: across
63.4 million tokens of agent runs, about 98% of the tokens were input being resent
each step rather than new work. The analysis here is the same one that produces that
figure, kept to the standard library so it cannot drift from the result it reports.

## Install

Clone the repository and install this package from the `cli` directory:

    git clone https://github.com/ahmlchannel2025/TokenLens.git
    cd TokenLens/cli
    pip install .

Or install it directly from GitHub without cloning:

    pip install "git+https://github.com/ahmlchannel2025/TokenLens.git#subdirectory=cli"

## Use

    tokenlens analyze batch_summary.json
    tokenlens analyze run_log.json --json
    tokenlens analyze - < telemetry.jsonl

The format is detected from the file. Three are understood. The first is the harness
output from the parent repository, either a batch summary or a single run. The second
is a generic step log, a JSON array of objects carrying `input_tokens` and
`output_tokens` per step, with optional `goal` and `boundary` fields that let the tool
estimate a compression saving. The third is Claude Code's OpenTelemetry output, read
from its `claude_code.api_request` events.

Claude Code does not record a plan or goal structure in its telemetry, so for its logs
the tool reports the resend split and the growth curve but notes that there are no
boundaries to compress. The OpenTelemetry reader is built against the field names in
Claude Code's monitoring documentation and has not yet been checked against a real
capture; it reads the JSON or console exporter output, not the protobuf that the gRPC
exporter emits.

The compression figure the tool prints is an upper bound on what boundary compression
could remove, not a saving that is safe to apply. In the study, compression of this
kind cut tokens substantially but also lowered task success, so any real use of it has
to be checked against an independent measure of whether the task still succeeded.
