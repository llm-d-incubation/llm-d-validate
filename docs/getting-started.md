# Getting started

Install `llmdv`, point it at a deployed stack, and read what it found. Every run writes the same
run directory, whether it ran from your machine, as an unattended job or as a cluster job.
[docs/cli.md](cli.md) is the command reference. Agents follow the
[`running-and-comparing-live-runs`](../.agents/skills/running-and-comparing-live-runs/SKILL.md)
skill.

`llmdv` never deploys, patches, scales or restarts anything. Cluster access, when given, is
read-only: every `kubectl` or `oc` call goes through a verb allowlist of `get`, `version`,
`config` and `logs`.

## Install

```sh
./install.sh && source .venv/bin/activate   # llmdv only
./install.sh --s3                           # adds boto3, for --remote-output s3://...
```

The script checks the Python floor, creates `.venv`, installs `llmdv` editable, and checks the
entry point and the pytest plugin. Re-run it to pick up a dependency change; `--recreate` starts
over. `--dev` adds the dev, `s3` and `ibm-cos` extras for working on `llmdv` itself: see
[CONTRIBUTING.md](../CONTRIBUTING.md). By hand, the equivalent of `--dev` is
`python3 -m venv .venv && ./.venv/bin/pip install -e '.[dev,s3,ibm-cos]'`.

What each path needs:

| You need | For |
| --- | --- |
| `llmdv`, installed as above (`--s3` adds the bucket client) | every path, and reading any run |
| the URL of the layer you test, and a key if it authenticates | every live run |
| read-only cluster access (`get` and `list` on pods, `get` on `pods/log`) | port-forwarded ingresses and `--pod-logs` |
| cluster access with the `llmdv-trigger` Role, and the CronJob deployed ([deploy/README.md](../deploy/README.md)) | cluster jobs |
| bucket credentials; the [AWS CLI](https://aws.amazon.com/cli/) to pull a run back | publishing and pulling runs |

A profile names its variables in `base_url_env` and `auth.key_env`. Export them from wherever you
keep the values. Never commit a credentials file, and never paste its values into a command, an
issue or a chat. If your credentials file sets variables without `export`, load it with
`set -a; source <file>; set +a`.

## Test a deployed llm-d guide

Every llm-d guide at [Tier 3](https://github.com/llm-d/llm-d/blob/main/guides/GUIDES-POLICY.md#5-production-readiness-tiers) (Validated: run end to end every
night) includes ready-to-use validation profiles under [`profiles/llm-d/`](../profiles/llm-d/),
one per combination of engine, model configuration and topology, built from the guide as of
llm-d commit `9ca8ff0`. After deploying a guide with an overlay a combination covers (its header
lists them), use that combination to verify that your deployment behaves as specified by the guide and its
APIs. By default it targets the guide's router, so give `llmdv` the EPP's URL. The EPP
authenticates nothing, so no key is needed:

```sh
P=profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml
export LLMDV_EPP_URL=<your EPP's URL>
llmdv profile validate $P                 # offline schema and consistency checks
llmdv plan --profile $P                   # every case verify would run or skip; sends nothing
llmdv verify --profile $P -m contract     # about a minute: the profile against reality
llmdv verify --profile $P --identity accelerator=gpu --identity provider=gke
```

To include a gateway or engine in your tests, add it as another ingress in a short local file
that extends the combination; `llmdv` then tests each layer and says which one a failure came
from. See
[include a gateway or engine](../profiles/README.md#include-a-gateway-or-engine).

Pass the lane's accelerator and provider with `--identity`: one combination covers several
overlays, and identity keeps their runs apart. Each guide folder's README gives the exact flags
per lane; [cli.md](cli.md#commands) lists every identity key.

A guide profile runs its `selection` by default: the router path, the overlay's parsers and
window, and what the guide changes. `--all` runs every case, including engine and model
conformance. One lane per configuration needs it; the table in
[profiles/llm-d/README.md](../profiles/llm-d/README.md#which-lane-runs-everything) names it.
[test-suites.md](test-suites.md) lists every suite, what it checks, and how to run one on its own
(`-m <suite>`).

A run opens with a banner and ends with a summary:

```
  llmdv 0.1.1  verify · example-two-ingress · 2 ingresses

── preflight · 2 ingresses ──────────────────────────────────────
   ok    vllm     http://localhost:18100                               81ms
   ok    router   http://localhost:18180                               87ms

── contract · 12 cases ──────────────────────────────────────────
   ok    vllm     declared_surfaces_exist                              0.54s
   skip  router   header_disposition [x-request-id]   requires_instrumentation

  PASSED · 21 of 22 cases passed, 1 skipped · 13.4s
```

Cases run contract first, then by ingress in the order the profile declares them. `-v` prints
the recorded checks under each case.

## Run your own stack

Any OpenAI- or Anthropic-compatible gateway, router or engine works, with or without llm-d.
Copy the closest example from [`profiles/examples/`](../profiles/examples/) to
`profiles/<system>/<model>.local.yaml` and fill it in; [writing-profiles.md](writing-profiles.md)
explains the choice and the minimum, and [profiles/REFERENCE.md](../profiles/REFERENCE.md) every
field. The `*.local.yaml` copy is gitignored, so cluster detail never enters the repository. A
profile holds no secrets or hostnames, only the names of the variables that carry them.

With only a gateway URL and a key, start from
[`example-gateway-only.yaml`](../profiles/examples/example-gateway-only.yaml):

```sh
P=profiles/<system>/<model>.local.yaml
export LLMDV_GATEWAY_KEY='<gateway key>'
llmdv profile validate $P
llmdv verify --profile $P -m contract
llmdv verify --profile $P --analyze       # the full run, with a dashboard
```

A profile can also declare the local setup its layers need, as
[`example-two-ingress.yaml`](../profiles/examples/example-two-ingress.yaml) does; `--setup` prints it:

```sh
llmdv profile show $P --matrix   # what the profile claims, and what will run
llmdv profile show $P --setup    # the setup it declares, pods resolved live
llmdv verify --profile $P
```

## Read the results

A run lands in `runs/<profile>-<utc-timestamp>-<unique-id>/`, named in the banner and the
summary. `--local-output ROOT` changes the parent directory; every invocation creates its own
child and preserves earlier runs. Start with:

- `summary.md`: the per-ingress table and the attribution reading.
- `failures/<ingress>/<case>/verdict.txt`: what was claimed and the observations behind it.
- `failures/<ingress>/<case>/repro.sh`: a runnable `curl` for the exchange. Export the key under
  the profile's variable name and run `bash` on it.
- `dashboard.html`, with `--analyze` or `llmdv analyze runs/<run>` afterwards.
- `outcome.json`: how the run ended, and the exit code's class.

Every other file, the exit codes and the skip classes are in [cli.md](cli.md#output). To turn
a failure group into an issue, use the
[`filing-a-fleet-finding`](../.agents/skills/filing-a-fleet-finding/SKILL.md) skill.

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
changed their answer. Scheduled runs (`--identity trigger=nightly`) are the baselines. See
[compare.md](compare.md).

## Run unattended

`llmdv job` runs `llmdv verify` as a child process, then publishes, optionally notifies Slack, and
prints `outcome.json`, however verify ended. That suits a Kubernetes CronJob or a CI step; the
[`Containerfile`](../Containerfile) builds a runner image.

```sh
llmdv job --profile /etc/llmdv/profile.yaml --remote-output "s3://$AWS_BUCKET/<prefix>" -- -m contract
```

- **Settings** come from flags or the environment: `LLMDV_PROFILE`, `LLMDV_RESULTS_URI`,
  `LLMDV_NOTIFY_WEBHOOK`, and the rest in `llmdv job --help`.
- **Flags the job does not know** go to `llmdv verify`, such as `--all` or `--evidence all`.
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
- **Keep one-off runs apart from scheduled ones.** Publish them under a different prefix or label,
  so a one-off never becomes one side of the next comparison.
- **Start with `-m contract`.** It proves the gateway, the key and the bucket work, in a minute,
  before you spend an hour on the full run.
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
