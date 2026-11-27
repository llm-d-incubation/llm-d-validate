---
name: running-and-comparing-live-runs
description: Use when starting a live `llmdv` run against a real gateway (from a PC, unattended with `llmdv` job, or as a cluster job with trigger.sh), publishing a run or pulling a published run back from the results bucket, or listing or comparing runs with `llmdv` compare.
---

# Running and comparing live runs

Use [Getting started](../../../docs/getting-started.md) for a first check,
[Publishing and running unattended](../../../docs/running.md) for operations, and
[Comparing runs](../../../docs/compare.md) for baseline semantics.

## Before any live run

1. Read [ENVIRONMENT.md](../../ENVIRONMENT.md) and establish tools, access and credential sources.
   Ask only for missing information. Never print credentials or cluster-identifying values.
2. Check for competing live runs. Use the default serial mode or the requested `--concurrency N`. Never pass `-n` or retry test requests.
3. Validate and preview the requested selection. For a newly authored profile, check its
   declarations with `--full-suite -m contract` before broader verification. A first-user smoke
   check has the narrower purpose described in Getting started.
4. Respect requested scope and keep the machine awake. Do not silently widen to the full suite.
   A guide's broader-validation requirements remain in its profile documentation.

## Credentials, without echoing them

Map the available secrets onto each ingress's declared variables. Check presence without
printing values. Publishing uses the selected storage credential chain; use
[storage prerequisites](../../../docs/running.md#publish-a-run). Direct router access still
follows the profile's authentication declaration.

## Start a run

Follow the [local walkthrough](../../../docs/getting-started.md#start-with-a-smoke-check) or
[job workflow](../../../docs/running.md#run-unattended). Use the printed child directory for
analysis and reproduction. Keep endpoint variables in the environment and local details in
ignored `*.local.yaml` files. Cluster runner operation is in [deploy/README.md](../../../deploy/README.md).

## Prefixes and triggers

Set configuration identity explicitly when required by the profile's comparison-identity guidance.
Scheduled runs use `trigger=nightly`; manual runs default to `trigger=manual`. Labels do not
separate baselines. Standalone compare judges the latest profile run; a job judges its own run.

## Pull a run back

Use [archive retrieval](../../../docs/running.md#pull-a-run-back). Confirm profile name,
archive prefix, authentication mode and `MANIFEST.json`; a working directory is not an archive.

## Compare runs

Follow [comparison commands and interpretation](../../../docs/compare.md). Read declaration and
stack drift first. A skip is missing evidence; newly exercised failures are not regressions.
Do not report comparison exit 2 or 3 as a passing result. Inspect delivery status separately
from the verification verdict.

## Then

Use [reporting-a-run](../reporting-a-run/SKILL.md) for an annotated dashboard,
[triaging-a-run](../triaging-a-run/SKILL.md) for investigation.
