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

## Sign-off and signing

Every commit carries a DCO sign-off (`git commit -s`, per llm-d's
[PR sign-off guide](https://github.com/llm-d/llm-d/blob/main/PR_SIGNOFF.md)); CI checks each
commit in a PR for it. Sign your commits as well (`git commit -S -s`).

## Development and testing

```sh
./install.sh --dev && source .venv/bin/activate
python -m pytest tests/ -q                     # one test at a time; no cluster or gateway needed
python -m pytest tests/ -q -n auto             # in parallel (pytest-xdist, from --dev)
ruff check src/ tests/ && ruff format --check src/ tests/
pre-commit install                             # formats with ruff on every commit
```

Parallel is for these offline tests only. `llmdv verify` refuses `-n`, `--numprocesses`,
`--maxprocesses`, `--dist` and `--tx` with exit `2`, because live cases depend on order and a
rate-limited key must not be multiplied.

**Ruff is pinned exactly**, in `pyproject.toml` (the `dev` extra) and in
`.pre-commit-config.yaml`; keep the two in sync. `ruff check` alone is not the whole gate: the
`ruff-format` pre-commit hook and CI both enforce formatting, so a change that lints clean can
still be rejected.

[docs/architecture.md](docs/architecture.md) describes the layout, and the invariants a change
must keep.

### Test layout

Every test is offline: cases run against fakes on local sockets.

| Path | What it is |
| --- | --- |
| `tests/fakes.py`, `fake_openai.py`, `fake_anthropic.py`, `fleet_fakes.py` | real HTTP servers on local sockets, with flags for the defects the suites exist to catch |
| `tests/transport_fakes.py`, `tool_engine.py` | the fake fleet's transport defects, and a generic tool-calling model behind it |
| `tests/*_helpers.py`, `surface_headers.py` | shared helpers, and a pytest plugin (`-p tests.surface_headers`) that gives a surface a required header |
| `tests/conftest.py` | shared fixtures |
| `tests/test_*.py` | one file per area of the library or suite under test |
| `tests/fixtures/stream-with-usage.sse` | a recorded streamed Chat completion that ends with a usage frame |
| `tests/fixtures/stream-no-usage.sse` | the same stream without one |

### What CI runs

| Workflow (`.github/workflows/`) | What it does |
| --- | --- |
| `offline-tests.yaml` | on every PR and push to `main`: lint (`ruff check`, `ruff format --check`, with the pinned ruff), then the offline suite in parallel on Python 3.10 through 3.15. No secrets, cluster or gateway; live verification never runs here |
| `image.yaml` | builds the runner image (amd64, arm64) and publishes it to `ghcr.io/llm-d-incubation/llm-d-validate`: `:main` and `:sha-<sha>` on every push to `main`, `:vX.Y.Z` and `:latest` on a release tag, signed with cosign; PRs build without pushing |
| `ci-signed-commits.yaml` | checks every commit in a PR carries a DCO sign-off |
| `prow-github.yaml` | Prow commands in comments: `/lgtm`, `/approve`, `/assign`, `/kind`, `/priority`, `/close` and others |
| `prow-pr-automerge.yaml` | merges a PR once it has `lgtm` and `approved` |
| `prow-pr-remove-lgtm.yaml` | removes `lgtm` when new commits are pushed |
| `non-main-gatekeeper.yaml` | adds `do-not-merge/hold` to PRs that target a branch other than `main` or `release-*` |
| `stale.yaml`, `unstale.yaml` | mark inactive issues and PRs stale, and clear the mark on activity |

The shared ones call reusable workflows in [llm-d/llm-d-infra](https://github.com/llm-d/llm-d-infra).
Prow's settings are in [`.github/prow.yaml`](.github/prow.yaml).

`.github/scripts/live_summary.py` writes the GitHub job summary for a live run as Markdown: the
verdict, the totals, a table per suite, the largest failure groups and an optional comparison. It
reads only the redacted run directory, runs with no credentials in its environment, and refuses
to print anything shaped like a credential:

```sh
python .github/scripts/live_summary.py runs/<run> --profile <name> --gateway <name> \
  --channel <folder> --event "$GITHUB_EVENT_NAME" --code "$EXIT_CODE" >> "$GITHUB_STEP_SUMMARY"
```

## Adding a case

- **Prove it can fail.** Add the defect to a fake (usually `tests/fakes.py`) and assert that the case fails
  against it, including that a recorded check is marked not-ok. A case that cannot fail checks
  nothing.
- **Classify every skip.** Use one of the [six skip classes](docs/cli.md#skip-classes), never a
  bare skip.
- **Assert what the API defines.** A case checks the behaviour the API specifies, not what one
  deployment happens to do today.
- **Update [docs/test-suites.md](docs/test-suites.md)**: the suite's row says what it checks, what enables it, and its status.

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
  work that needs familiarity with a surface, the layers of a serving stack or the profile schema.

Comment `/assign` on an issue to take it, and `/unassign` to release it.

## Security

See [SECURITY.md](SECURITY.md) to report a vulnerability.
