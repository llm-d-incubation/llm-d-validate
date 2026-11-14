---
name: running-and-comparing-live-runs
description: Use when starting a live llmdv run against a real gateway (from a PC, or unattended with llmdv job), publishing a run or pulling a published run back from the results bucket, or listing or comparing runs with llmdv compare.
---

# Running and comparing live runs

Every path writes the same run directory, so reading and comparing work the same way afterwards. Human version: [docs/end-to-end.md](../../../docs/end-to-end.md). Comparison semantics: [docs/compare.md](../../../docs/compare.md).

## Before any live run

Before the first live step, check what the person has and ask only for what is missing.

1. **Check nobody else is running against this gateway.** One live run per gateway at a time: cache and session cases depend on what earlier requests left behind, and the key is rate-limited.
2. **Never pass `-n`.** `llmdv verify` refuses parallel workers; live cases run one at a time.
3. **Run the contract first.** `-m contract` takes about a minute and proves the gateway, the key and (when publishing) the bucket before an hour-long full run. A preflight failure exits `3`: nothing was tested.

## Credentials, without echoing them

The profile names its variables in `base_url_env` and `auth.key_env` (every ingress's, in a multi-ingress profile). Each profile reads its own names; a CI pipeline maps its secrets onto them the same way. Where the values live differs per person: ask, have them load the values, and map them onto the profile's names. Never type, paste or print a value. A router (EPP) ingress reached directly needs only its URL.

A `*.local.yaml` copy may write `base_url` inline instead of `base_url_env` (never both); it is gitignored. Testing a model you were handed endpoints for is the testing-a-model-deployment skill.

Publishing and pulling runs need the bucket variables exported (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `AWS_DEFAULT_REGION`, `AWS_BUCKET`); without them, work from local run directories. Check a variable with `[ -n "$VAR" ] && echo set`, never `echo $VAR`.

## Start a run

**From a PC** (at the repository root, with `.venv` active):

```sh
llmdv verify --profile <profile> -m contract
llmdv verify --profile <profile> --analyze        # the full run; lands in runs/<timestamp>/
```

To publish it, add:

```sh
--evidence all --remote-output "s3://$AWS_BUCKET/<prefix>" \
--label trigger=on-demand --label "actor=$USER"
```

Use a `*.local.yaml` copy for machine-specific changes, and keep the machine awake. A multi-ingress profile runs every ingress; `--ingress <id>` (repeatable) restricts it, and `llmdv profile show <profile> --setup` prints any port-forwards it needs.

**Unattended** (`llmdv job`, e.g. as a CronJob or a CI step):

```sh
llmdv job --profile <profile> --remote-output "s3://$AWS_BUCKET/<prefix>" -- -m contract
```

Arguments after `--` go to `llmdv verify`. Results go to `--remote-output`, or `LLMDV_RESULTS_URI` when it is not given; `outcome.json` in the job log says where.

## Prefixes and triggers

A scheduled run is labelled `trigger=nightly`; label every other run differently (e.g. `trigger=on-demand`). Only `llmdv job --compare` restricts its baselines to `trigger=nightly` runs. A standalone `llmdv compare` compares the latest run under the prefix with the one before it, whatever its trigger, so never publish a one-off under the scheduled runs' prefix: it would become the next comparison's baseline.

## Pull a run back

A published run sits at `s3://$AWS_BUCKET/<prefix>/<profile name>/<run id>/`. The profile name is its `name:` field, not its path. Run ids start with a UTC timestamp and sort oldest first.

```sh
PREFIX="s3://$AWS_BUCKET/<prefix>"   # the prefix it was published under, or a job's LLMDV_RESULTS_URI
llmdv compare --from "$PREFIX" --profile <profile name> --list
# if --list fails, list the folder directly:
aws s3 ls "$PREFIX/<profile name>/" --endpoint-url "$AWS_ENDPOINT_URL_S3"
RUN=<run id>
aws s3 cp --recursive "$PREFIX/<profile name>/$RUN/" "runs/$RUN/" --endpoint-url "$AWS_ENDPOINT_URL_S3"
```

`--endpoint-url` is required for an S3-compatible store that is not AWS: without it you get `InvalidAccessKeyId` or `NoSuchBucket`. A run is completely published only when its `MANIFEST.json` is there.

## Compare runs

```sh
llmdv compare --from "$PREFIX" --profile <profile name>                     # latest vs the run before
llmdv compare --from "$PREFIX" --profile <profile name> --baseline <run id> # latest vs a named run
llmdv compare --from "$PREFIX" --profile <profile name> --window 3          # three runs must agree
# (llmdv job spells these --compare-baseline and --compare-window)
```

| Exit | Meaning |
|---|---|
| `0` | no regression, or `--no-fail` was given |
| `1` | a case went from pass to fail or error |
| `2` | nothing to compare: no runs, one run, or an empty run |
| `4` | the store could not be read (missing credentials or `AWS_ENDPOINT_URL_S3`, wrong bucket); llmdv reports it as an internal failure. Check that the credentials file was sourced with `set -a` |

Runs published to a local directory (`--remote-output file:///<dir>`) compare the same way with `--from file:///<dir>`. A plain local run (`runs/<timestamp>/`) has neither that layout nor a `MANIFEST.json`, so compare two of those case by case from their `results.jsonl`, keyed on `nodeid`.

Read the declaration-drift warning first: a changed profile can explain the whole delta. Then `verdict → skip` is lost evidence, not a failure, and `skip → fail` is new evidence, not new breakage.

## Then

Build and annotate the dashboard with reporting-a-run. Turn a failure group into an issue with filing-a-fleet-finding. Exit codes for every command: [docs/manual.md](../../../docs/manual.md#exit-codes).
