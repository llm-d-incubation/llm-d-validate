# Working in this repo

What an agent or a new contributor needs before changing anything here.

`llmdv` is a functional test library that points at **already-deployed** model serving stacks.
Start with [docs/architecture.md](docs/architecture.md) for the layout and [docs/manual.md](docs/manual.md) for
every command. Before any live step, check what the person you are working for has: tools,
access, and where they keep credentials. The skills assume nothing about the machine.

## Skills

Each lives at `.agents/skills/<name>/SKILL.md`. Load the one that matches before you start.

| Skill | Use it when |
|---|---|
| [writing-an-infra-profile](.agents/skills/writing-an-infra-profile/SKILL.md) | writing, updating or reviewing a profile; choosing any declaration; naming a component behind an ingress |
| [probing-a-fleet-before-asserting](.agents/skills/probing-a-fleet-before-asserting/SKILL.md) | writing or changing an assertion about a live response, or a live case fails |
| [extending-llmdv](.agents/skills/extending-llmdv/SKILL.md) | adding a surface, ingress kind, case, client, assertion or capability |
| [testing-a-model-deployment](.agents/skills/testing-a-model-deployment/SKILL.md) | testing a model you were handed endpoints for (a LiteLLM gateway, a router or EPP route, or both): picking and pointing a profile, first checks, the run, the comparison |
| [running-and-comparing-live-runs](.agents/skills/running-and-comparing-live-runs/SKILL.md) | starting a live run, pulling one back, comparing runs |
| [reporting-a-run](.agents/skills/reporting-a-run/SKILL.md) | sharing a run directory; building the annotated HTML dashboard and its notes |
| [triaging-a-run](.agents/skills/triaging-a-run/SKILL.md) | finding out what broke in a run and why: grouping failures into causes, existing issues, transient or real, the layer responsible, reproducing from bundles and logs |
| [filing-a-fleet-finding](.agents/skills/filing-a-fleet-finding/SKILL.md) | turning a failure group into a GitHub issue or issue draft |

`.agents/` is the shared place for agent instructions and skills. `.claude/` is gitignored:
keep personal Claude Code settings there. To have Claude Code load these skills automatically,
link them in your own checkout: `ln -s ../.agents/skills .claude/skills` (from the repo root,
after `mkdir -p .claude`).

## Hard rules

**Never mutate a cluster.** `llmdv` tests deployed infrastructure; it does not deploy. Cluster
access goes through `kube.py`, whose verb allowlist leaves no path to `apply`, `delete`,
`scale` or `exec`. If you need a new verb, justify it against that guarantee first.

**Never commit cluster-identifiable detail.** No namespaces, cluster names, kubeconfig
contexts, API server hostnames, pod or service names, or tenant keys — not in code, comments,
profiles, docs, issues, PR bodies or commit messages. The committed `profiles/examples/*.yaml`
carry placeholders; real values live in `profiles/<system>/*.local.yaml`, which is gitignored.

## Conventions that are enforced

**No `print`.** Ruff rule `T20`. Everything goes through the logger tree: diagnostics to
stderr under `--log-level`, primary output to stdout through a logger the level never
suppresses. See `log/setup.py`.

**argparse, not Typer.** Click writes help and errors straight to stdout, which fights the
single-logger contract, and `parse_known_args` is what forwards unknown arguments to pytest.
`argparse.REMAINDER` is specifically avoided — it swallows later flags.

**Six skip classes, never collapsed** (`SkipClass` in `profile/schema.py`). `undeclared` is a
fleet gap, `not_implemented` a tool gap, `requires_instrumentation` and `requires_load` name a
missing instrument, `excluded_by_policy` is a choice not to run on a shared fleet, and
`throttled` is our own quota. A matrix that merges them reads as coverage when it is not.

**Exit codes separate blame.** `0` ok, `1` test failures (the fleet), `2` usage or invalid
profile, `3` preflight unsatisfied (the operator's environment), `4` a llmdv defect, `5`
verification passed but results or the notification were not delivered, `6` interrupted. Never
widen `1` to cover an environment problem, and never let `5` mask a verification failure: the
verification code takes precedence.

**Test requests are never retried; deliveries are, within bounds.** A retried inference
request hides the failure under test. A retried upload or notification preserves evidence.

**A verdict carries its evidence.** Assertions record what they observed through
`journal.record`, before deciding, so a failure shows the value that caused it. A pass with no
recorded checks is indistinguishable from a test that checked nothing.

**Redaction is a logging filter**, not just an evidence-writer concern. httpx logs request
headers at DEBUG and the run directory is published.

## Before you say it works

```sh
source .venv/bin/activate                     # ./install.sh --dev creates it
python -m pytest tests/ -q -n auto            # no cluster needed; -n needs ./install.sh --dev
ruff check src/ tests/
ruff format --check src/ tests/               # the pre-commit hook gates on this too
```

The repo pins `ruff` exactly, in `pyproject.toml` and in `.pre-commit-config.yaml`, which must
stay in sync. `ruff check` alone is not the whole gate: formatting is enforced by a
`ruff-format` pre-commit hook, so a change that lints clean can still be rejected on commit.
`./install.sh --dev` installs both tools, and `pytest-xdist` for `-n auto`. Parallel is for
llmdv's own offline tests only: `llmdv verify` and `llmdv job` refuse `-n` and always run live
cases one at a time, so never try to parallelize a run against a fleet.

A live run needs a profile and, for in-cluster ingresses, port-forwards:

```sh
llmdv profile show <profile> --setup           # prints the forwards, pods resolved live
llmdv verify --profile <profile>
```

If you add a test case, add the corresponding defect to `tests/fakes.py` and prove the case
fails against it. A case that has never failed has not been tested.

## Repo workflow notes

These have each cost time at least once:

- GitHub renders a single newline in an **issue or PR body** as a line break. Do not hard-wrap
  prose there. Files like `README.md` do reflow, so wrapping is fine in those.
- A closing keyword binds only to the first reference in a comma-separated list. `Closes #1,
  #2` closes one issue.
- Closing keywords in a commit only fire when the commit reaches the default branch. On a
  feature branch, close the issue explicitly with a pointer to the commit.
- If you replay history, generate file lists with `git show --name-only --no-renames`. Rename
  detection collapses a rename to the new path and silently drops the deletion.
- Every PR starts from an issue in this repo and links it (`Closes #N` or `Part of #N`); keep
  each PR small. Sign your commits and run the pre-commit hooks before pushing. See
  [CONTRIBUTING.md](CONTRIBUTING.md).
- Prefer independent branches off `main` to stacked PRs. If you do stack, a squash merge of the
  parent rewrites its commits, so rebase each child onto main past the old parent:
  `git rebase --onto origin/main <old parent tip>`.
