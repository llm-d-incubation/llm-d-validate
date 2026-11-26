# Getting started

Run a small check against a deployed endpoint, read its evidence, then widen coverage. This
walkthrough uses a local machine; [cluster jobs](../deploy/README.md) use the same profiles and
produce the same kind of run directory.

## Install

You need Python 3.10+ and Git. Clone the repository to obtain the installer and profiles:

```sh
git clone https://github.com/llm-d-incubation/llm-d-validate.git
cd llm-d-validate
./install.sh
source .venv/bin/activate
```

You also need a reachable model endpoint, and a credential if it authenticates. Cluster access
is optional for URL-only verification. `llmdv` does not create or supervise port-forwards.
If your profile declares setup, `llmdv profile show "$P" --setup` prints what is required;
resolving declared pods can read the cluster.

Choose one of the two paths below. Keep credentials in your environment or a private credential
store. Never put their values in committed profiles or shared output.

## Test a deployed llm-d guide

Find your deployment in the [guide catalog](../profiles/llm-d/README.md). Its README maps
configurations to profiles. The following example is for optimized-baseline's Qwen3-32B/vLLM
configuration with parsers; use the matching profile for your own deployment:

```sh
export P=profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml
export LLMDV_ROUTER_URL='https://your-router.example'
```

Replace the example URL. These profiles target the router directly and declare no authentication.
To test more layers, follow [adding a gateway or engine](../profiles/README.md#include-a-gateway-or-engine).
Continue at [Start with a smoke check](#start-with-a-smoke-check).

## Run your own stack

Copy the minimal example to a local profile:

```sh
cp profiles/examples/example-minimal.yaml profiles/my-stack.local.yaml
export P=profiles/my-stack.local.yaml
```

Edit the copy before running it:

- Set `model.public_id` to the model ID your endpoint serves.
- Set the ingress `kind` to its position: `gateway`, `router` or `engine`.
- Set `surfaces` to the APIs it is intended to serve. The starter declares Chat Completions.
- Review expected error statuses. They state the intended contract, not values copied from a
  failed request. If the endpoint does not authenticate, use `auth: {type: none}`.

The starter reads `LLMDV_BASE_URL` and, for bearer authentication, `LLMDV_API_KEY`. Set the URL
and load the key from your credential store. In Bash, you can enter it without putting it in
shell history:

```bash
export LLMDV_BASE_URL='https://your-gateway.example'
read -r -s -p 'API key: ' LLMDV_API_KEY
export LLMDV_API_KEY
```

Replace the URL; omit the key step for unauthenticated endpoints. Local `*.local.yaml` files
are gitignored. [Writing a profile](writing-profiles.md) explains additional capabilities,
per-layer model aliases and reuse through `extends`.

## Start with a smoke check

Both paths now have a profile in `$P` and its endpoint variables set:

```sh
llmdv profile validate "$P"
llmdv plan --profile "$P" --test-set smoke
llmdv verify --profile "$P" --test-set smoke --analyze
```

`validate` and `plan` are offline. Review the plan before the live command: smoke selects model
discovery and basic non-streaming/streaming generation for the declared APIs. Cases that do not
apply are skipped. A smoke pass establishes only those checks, not tools, caching, long context
or every API behavior.

Verification runs one case at a time. Do not add pytest's `-n`, and avoid competing live runs
against the same endpoint. Keep a laptop awake until the run finishes.

## Read the results

The command prints a new directory under `runs/`. Each invocation creates its own child, so
reusing `--local-output ROOT` preserves earlier runs. Use the printed child path below:

```sh
export RUN='runs/<the directory printed by verify>'
cat "$RUN/summary.md"
```

Open `$RUN/dashboard.html` in your browser. If you did not pass `--analyze`, build it with
`llmdv analyze "$RUN"`. Start with the executed/ skipped counts, then the failures and their
recorded checks. A green result applies only to checks that actually executed.

| Result | Next action |
| --- | --- |
| Passed, with executed checks | Continue to the profile's default coverage below |
| Failed | Read the failing check and its evidence; confirm the profile expectation before attributing the failure |
| Skipped | Read the reason: an omitted declaration, missing instrument, policy or throttling is not a passing check |
| Usage/profile error (exit 2) | Correct the command or profile; no verification occurred |
| Environment error (exit 3) | Read the reported endpoint, credential, local connection or results-store problem |

The [command reference](cli.md#exit-codes) defines every exit code and
[skip class](cli.md#skip-classes). [Output files](cli.md#output) lists all artifacts.

## Widen coverage

Once smoke completes, run the profile's default selection:

```sh
llmdv plan --profile "$P"
llmdv verify --profile "$P" --analyze
```

For a newly written profile, check the declared surfaces and headers explicitly with
`llmdv verify --profile "$P" --full-suite -m contract` before relying on broader results.
This checks the declarations; it is a different task from the initial smoke check.

A guide's default tests its selected API and guide behavior. The minimal starter's default is
small and leaves optional capabilities undeclared. Add the intended declarations as described
in [Writing a profile](writing-profiles.md). When you want every applicable library case,
preview `llmdv plan --profile "$P" --full-suite` before running it.

For named subsets, individual suites or layer restrictions, use
[Selection and coverage](cli.md#test-sets-and-coverage). No `--ingress` means every declared
layer; an explicit selector restricts the run to the named IDs.

## Publish a run

To store a run where others can pull and list it, add `--remote-output` with the bucket
credentials loaded. `--evidence all` keeps a bundle for passing cases too, so a pass can be
audited:

```sh
llmdv verify --profile $P --analyze --evidence all \
  --remote-output "s3://$AWS_BUCKET/<prefix>" \
  --identity trigger=manual --label "actor=$USER"
```

Labels are free-form `key=value` pairs recorded in `outcome.json` and on the dashboard. For an
S3-compatible store, set `AWS_ENDPOINT_URL_S3` (or `AWS_ENDPOINT_URL`, read when the first is unset). IBM Cloud Object Storage with an IAM API key uses
`LLMDV_S3_AUTH=ibm-iam` and the `ibm-cos` extra. A run is published to
`<prefix>/<profile name>/<run id>/` and is complete once its `MANIFEST.json` is there.

To pull a published run back:

```sh
PREFIX="s3://$AWS_BUCKET/<prefix>"
llmdv compare --from "$PREFIX" --profile <profile name> --list
RUN=<run id from the list>
aws s3 cp --recursive "$PREFIX/<profile name>/$RUN/" "runs/$RUN/"   # add --endpoint-url for an S3-compatible store
llmdv analyze runs/$RUN && open runs/$RUN/dashboard.html
```

The profile name is the profile's `name:` field, not its path. Run ids start with a UTC
timestamp, so they sort oldest first. `llmdv analyze` prints the counts and every failure group,
and rebuilds `dashboard.html`.

## Compare runs

`llmdv compare` reads two runs of the same profile back from the archive and reports which cases
changed their answer. Automatic baselines must have matching configuration identity, including trigger and flavor. See
[compare.md](compare.md).

## Run unattended

`llmdv job` runs `llmdv verify` as a child process, then publishes, optionally notifies Slack, and
prints `outcome.json`, however verify ended. That suits a Kubernetes CronJob or a CI step; the
[`Containerfile`](../Containerfile) builds a runner image.

```sh
llmdv job --profile /etc/llmdv/profile.yaml --remote-output "s3://$AWS_BUCKET/<prefix>" --full-suite -- -m contract
```

- **Settings** come from flags or the environment: `LLMDV_PROFILE`, `LLMDV_RESULTS_URI`,
  `LLMDV_NOTIFY_WEBHOOK`, and the rest in `llmdv job --help`.
- **Flags the job does not know** go to `llmdv verify`, such as `--full-suite` or `--evidence all`.
  Arguments after `--` reach pytest unparsed, such as `-- -m contract`. `--analyze` and
  `--compare` are the job's own: a dashboard in the run, and a comparison after publishing.
- **If publishing fails** and a fallback is configured (`--fallback-output` or
  `LLMDV_FALLBACK_URI`), the run is kept there; `outcome.json` says where.

When the fleet is reachable only from inside its cluster, `llmdv job` runs there as a CronJob,
and `deploy/trigger.sh` starts a one-off run from it. The image, the overlay, the Secret, every
`trigger.sh` option and where results land are in [deploy/README.md](../deploy/README.md). A
cluster job publishes to the cluster's own `LLMDV_RESULTS_URI`, which may be a different bucket
or prefix from your other runs.

## Rules for every live run

- **One live run at a time per gateway.** Cache and session cases depend on what earlier requests
  left behind, and a key is usually rate-limited.
- **Never pass `-n`.** Live runs are always one case at a time; `llmdv verify` refuses it.
- **Keep comparison lanes distinct.** Set `--identity trigger=nightly` for scheduled runs;
  manual runs default to `trigger=manual`. Free-form labels do not isolate baselines.
- **Keep the machine awake.** A sleeping laptop stalls requests, and the cases in flight fail on
  the stream-idle timeout.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `aws` asks for credentials, or says access denied | the credentials file was sourced without `set -a`, so nothing was exported |
| `InvalidAccessKeyId` or `NoSuchBucket` | the store is S3-compatible and `--endpoint-url` (or `AWS_ENDPOINT_URL_S3`) is missing |
| `llmdv compare --list` finds no runs | wrong prefix, or the profile *path* was given instead of its name; or the run has not finished publishing |
| exit `3` | the gateway, a key or the bucket was unreachable at preflight, so nothing was tested; or a local port-forward died mid-run, and the summary says what ran before it |
| a run is not in the bucket after exit `0` | look for exit `5`: the upload failed, and the log says why |
| `trigger.sh` is refused | the caller lacks the `llmdv-trigger` Role, or `--profile-file` is not an in-cluster profile (`vantage: in-cluster`) |
| many timeouts in a run from your machine | the machine slept, or another live run hit the same gateway at the same time |
