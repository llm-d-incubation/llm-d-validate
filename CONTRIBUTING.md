# Contributing to llm-d-validate

Please read the
[llm-d organization contributing guide](https://github.com/llm-d/llm-d/blob/main/CONTRIBUTING.md)
for the overall contributing guidelines: process, code review, commit style, DCO sign-off and
community channels. This document covers only what is specific to this repository.

## Maintainers

The core maintainers are listed in [OWNERS](OWNERS). Comment `/lgtm` and `/approve` on a pull
request to review and approve it; a pull request merges once it has both.

## Pull requests

These apply to every PR, whether a person or an AI agent wrote it. They keep contributions
traceable and easy to review.

### Every PR starts with an issue

Before opening a PR, make sure an issue in this repository describes the work. A PR does not
need to close its issue, but it must make clear progress toward it. If no issue exists, open one
first.

### Keep it small

A PR should be small enough for a person to read and understand in a short time. Several small
PRs that chip away at one issue are easier to review, test and revert than one large one. When a
change cannot reasonably be split, say why in the description.

### Writing the description

Link the issue (`Closes #123` or `Part of #123`), then cover, briefly:

- **What it solves:** which issue, or which part of it, this PR addresses.
- **What is still open:** anything not yet tested or resolved, and why.
- **Problems encountered:** development or testing problems, whether they are resolved, and
  whether any blocked validation. If unresolved, suggest a path forward.

### AI-assisted contributions

If you use AI coding tools, keep the PR description and comments they write concise, focused on
the issue, and limited to the work done.

Avoid:

- line counts, file counts or other change statistics
- promotional language ("comprehensive", "robust", "production-ready")
- summaries that repeat what the diff already shows

Do mention:

- any APIs added or changed, in a few sentences rather than file by file
- changes or areas that are not confidently tested
- new tests, and the gaps they close
- how to verify: the exact command, test or steps a reviewer can run

### Communicating progress

After opening the PR, post a short comment on the linked issue summarizing the progress, so
everyone following the issue stays informed.

## Development and testing

```sh
./install.sh --dev && source .venv/bin/activate
python -m pytest tests/ -q                                  # no cluster or gateway needed
ruff check src/ tests/ && ruff format --check src/ tests/
pre-commit install                                          # optional: formats with ruff on every commit
```

The offline suite runs every case against `tests/fakes.py`, a real HTTP server on a local socket
with flags for the defects the suite exists to catch. CI runs it on Python 3.10, 3.11 and 3.12.

[docs/architecture.md](docs/architecture.md) describes the layout, and the invariants a change must keep.

## Adding a case

- **Prove it can fail.** Add the defect to `tests/fakes.py` and assert that the case fails
  against it, including that a recorded check is marked not-ok. A case that cannot fail checks
  nothing.
- **Classify every skip.** Use one of the [six skip classes](docs/manual.md#skip-classes), never a
  bare skip.
- **Assert what the API defines.** A case checks the behaviour the API specifies, not what one
  deployment happens to do today.
- **Update [docs/coverage.md](docs/coverage.md)** with the suite's case count.

## Adding a profile example

Start from the closest file in [`profiles/examples/`](profiles/examples/) and follow
[profiles/README.md](profiles/README.md). Declare what a correct stack does, and record what you
measured in a comment beside the line.

**Never commit credentials, hostnames, namespaces or other cluster detail.** A committed profile
names its URL and key by environment variable. Keep machine-specific copies as `*.local.yaml`,
which is gitignored.

## Finding an issue

- [`good first issue`](https://github.com/llm-d-incubation/llm-d-validate/issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22+no%3Aassignee):
  self-contained tasks that run with the offline suite alone.
- [`help wanted`](https://github.com/llm-d-incubation/llm-d-validate/issues?q=is%3Aopen+is%3Aissue+label%3A%22help+wanted%22+no%3Aassignee):
  work that needs familiarity with a surface, an ingress kind or the profile schema.

Comment `/assign` on an issue to take it, and `/unassign` to release it.

## Security

See [SECURITY.md](SECURITY.md) to report a vulnerability.
