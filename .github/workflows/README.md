# Workflows

| Workflow | What it does |
| --- | --- |
| [offline-tests.yaml](offline-tests.yaml) | lint and the offline suite on every pull request and push to `main` |
| [ci-signed-commits.yaml](ci-signed-commits.yaml) | checks every commit in a pull request carries a DCO sign-off |
| [prow-github.yaml](prow-github.yaml) | Prow commands in comments: `/lgtm`, `/approve`, `/assign`, `/kind`, `/priority`, `/close` and others |
| [prow-pr-automerge.yaml](prow-pr-automerge.yaml) | merges a pull request once it has `lgtm` and `approved` |
| [prow-pr-remove-lgtm.yaml](prow-pr-remove-lgtm.yaml) | removes `lgtm` when new commits are pushed |
| [non-main-gatekeeper.yaml](non-main-gatekeeper.yaml) | adds a `do-not-merge/hold` label to pull requests that target a branch other than `main` or `release-*` |
| [stale.yaml](stale.yaml), [unstale.yaml](unstale.yaml) | mark inactive issues and pull requests stale, and clear the mark on activity |

The shared ones call reusable workflows in [llm-d/llm-d-infra](https://github.com/llm-d/llm-d-infra).
Prow's settings are in [`../prow.yaml`](../prow.yaml).
