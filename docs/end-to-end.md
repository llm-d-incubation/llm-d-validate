# End to end: run `llmdv` against a live stack, then read the results

Run the suite against a real gateway from your machine, unattended as a job, or as a cluster job.
Every run writes
the same run directory, so reading the results works the same way afterwards.

**Contents:** [Setup once](#setup-once) · [From your machine](#from-your-machine) ·
[Publish a run](#publish-a-run) · [Unattended](#unattended) ·
[From a cluster job](#from-a-cluster-job) ·
[Read the results](#read-the-results) · [Rules](#rules-for-every-live-run) ·
[Reference](#reference)

## Setup once

```sh
./install.sh --s3 && source .venv/bin/activate
```

You need the gateway URL and a key for it. To publish or pull runs, you also need credentials for
an S3 or S3-compatible bucket.

<details>
<summary><b>More:</b> what each path needs, and keeping credentials safe</summary>

| You need | For |
| --- | --- |
| `llmdv`, installed as above (`--s3` adds the bucket client) | every path, and reading any run |
| the gateway URL and key | every live run |
| read-only cluster access (`get` on pods and `pods/log`) | port-forwarded ingresses and `--pod-logs` |
| cluster access with the `llmdv-trigger` Role, and the CronJob deployed ([deploy/README.md](../deploy/README.md)) | cluster jobs |
| the [AWS CLI](https://aws.amazon.com/cli/) and bucket credentials | pulling a published run (publishing needs only the credentials) |

A profile names its variables in `base_url_env` and `auth.key_env`; export those from wherever you
keep the values. Never commit a credentials file, and never paste its values into a command, an
issue or a chat. If your credentials file sets variables without `export`, load it with
`set -a; source <file>; set +a`.

</details>

## From your machine

Copy [`example-gateway-only.yaml`](../profiles/examples/example-gateway-only.yaml) to
`profiles/<system>/<model>.local.yaml` and fill it in ([profiles/README.md](../profiles/README.md)).
Then:

```sh
export LLMDV_GATEWAY_KEY='<gateway key>'
llmdv verify --profile profiles/<system>/<model>.local.yaml -m contract   # about a minute
llmdv verify --profile profiles/<system>/<model>.local.yaml --analyze     # the full run
```

The run lands in `runs/<timestamp>/`; open its `dashboard.html`.

- **Keep the machine awake.** A sleeping laptop stalls requests, and the cases in flight fail
  on the stream-idle timeout.
- **Keep machine-specific changes in a `*.local.yaml` copy.** It is gitignored, so cluster detail
  never enters the repository.

## Publish a run

To store a run where others can pull and list it, add `--remote-output` with the bucket
credentials loaded:

```sh
llmdv verify --profile P --analyze --evidence all \
  --remote-output "s3://$AWS_BUCKET/<prefix>" \
  --label trigger=on-demand --label "actor=$USER"
```

Labels are free-form `key=value` pairs recorded in `outcome.json` and on the dashboard. For an
S3-compatible store, set `AWS_ENDPOINT_URL_S3`. IBM Cloud Object Storage with an IAM API key
uses `LLMDV_S3_AUTH=ibm-iam` and the `ibm-cos` extra.

A run is published to `<prefix>/<profile name>/<run id>/`, and is complete once its
`MANIFEST.json` is there.

## Unattended

`llmdv job` runs `llmdv verify` as a child process, publishes, optionally notifies a webhook, and
prints `outcome.json`, however verify ended. That makes it suitable for a Kubernetes CronJob or a
CI step. The [`Containerfile`](../Containerfile) builds a runner image.

```sh
llmdv job --profile /etc/llmdv/profile.yaml --remote-output "s3://$AWS_BUCKET/<prefix>" -- -m contract
```

<details>
<summary><b>More:</b> settings, and where results land</summary>

- **Settings** come from flags or the environment: `LLMDV_PROFILE`, `LLMDV_RESULTS_URI`,
  `LLMDV_NOTIFY_WEBHOOK`. Run `llmdv job --help` for the full list.
- **Arguments after `--`** go to `llmdv verify`, such as `-- -m contract`. `--analyze` and
  `--compare` are the job's own flags: a dashboard in the run, and a comparison after publishing.
- **Scheduled runs** labelled `trigger=nightly` are the baselines for `--compare`
  ([docs/compare.md](compare.md)). Label one-off runs differently so they never become baselines.
- **If publishing fails** and a fallback location is configured (`--fallback-output` or
  `LLMDV_FALLBACK_URI`), the run is kept there; `outcome.json` says where.

</details>

## From a cluster job

When the fleet is only reachable from inside its cluster, `llmdv job` runs there as a CronJob,
on its own schedule. To start a one-off run:

```sh
job="$(deploy/trigger.sh -n <namespace> --trigger manual --id contract-check -- -m contract)"
kubectl -n <namespace> logs -f "job/$job"
```

<details>
<summary><b>More:</b> what a job does, options, and where results land</summary>

`llmdv job` runs `llmdv verify` as a child process, publishes, optionally notifies Slack, and prints
`outcome.json` to the job log. Deploying it (image, overlay, settings and Secret) and every `trigger.sh` option are in
[deploy/README.md](../deploy/README.md).

- **Scheduled runs** are labelled `trigger=nightly` and are the baselines for `--compare`
  ([compare.md](compare.md)).
- **One-off runs:** the arguments after `--` replace the CronJob's `args:`; for example
  `-- --analyze --compare` for a full run with a dashboard and a comparison. These runs are
  never baselines.
- **Options:** `--cli oc` for OpenShift, and `--profile-file <in-cluster profile>` to run a
  different profile once.
- **Results** land at `<LLMDV_RESULTS_URI>/<profile name>/<run id>/`. `LLMDV_RESULTS_URI` is the
  cluster's own setting, which may be a different bucket or prefix from your other runs. If
  publishing fails and a fallback volume is configured, the run is kept there; `outcome.json` in
  the job log says where.

</details>

## Read the results

A run you did not publish is already in `runs/`. To pull a published one:

```sh
PREFIX="s3://$AWS_BUCKET/<prefix>"
llmdv compare --from "$PREFIX" --profile <profile name> --list
RUN=<run id from the list>
aws s3 cp --recursive "$PREFIX/<profile name>/$RUN/" "runs/$RUN/"   # add --endpoint-url for an S3-compatible store
llmdv analyze runs/$RUN && open runs/$RUN/dashboard.html
```

<details>
<summary><b>More:</b> run layout, and replaying a failure</summary>

- **The profile name** is the profile's `name:` field, not its path.
- **Run ids** start with a UTC timestamp, so they sort oldest first.

`llmdv analyze` prints the counts and every failure group, and rebuilds `dashboard.html`.

To replay one failure by hand, export the gateway key under the profile's variable name and run
its script:

```sh
bash runs/$RUN/failures/<ingress>/<case>/repro.sh
```

</details>

## Rules for every live run

- **One live run at a time per gateway.** Cache and session cases depend on what earlier requests
  left behind, and a key is usually rate-limited.
- **Never pass `-n`.** Live runs are always one case at a time; `llmdv verify` refuses it.
- **Keep one-off runs apart from scheduled ones.** Publish them under a different prefix or label,
  so a one-off never becomes one side of the next comparison.
- **Start with `-m contract`.** It proves the gateway, the key and the bucket work, in a minute,
  before you spend an hour on the full run.

## Reference

**Exit codes** are the same for every path: see [docs/manual.md](manual.md#exit-codes).

<details>
<summary><b>Troubleshooting</b></summary>

| Symptom | Cause |
| --- | --- |
| `aws` asks for credentials, or says access denied | the credentials file was sourced without `set -a`, so nothing was exported |
| `InvalidAccessKeyId` or `NoSuchBucket` | the store is S3-compatible and `--endpoint-url` (or `AWS_ENDPOINT_URL_S3`) is missing |
| `llmdv compare --list` finds no runs | wrong prefix, or the profile *path* was given instead of its name; or the run has not finished publishing |
| exit `3` | the gateway, a key or the bucket was unreachable; nothing was tested |
| a run is not in the bucket after exit `0` | look for exit `5`: the upload failed, and the log says why |
| `trigger.sh` is refused | the caller lacks the `llmdv-trigger` Role, or `--profile-file` is not an in-cluster profile (`vantage: in-cluster`) |
| many timeouts in a run from your machine | the machine slept, or another live run hit the same gateway at the same time |

</details>
