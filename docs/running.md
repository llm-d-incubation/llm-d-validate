# Publishing and running unattended

After your [first run](getting-started.md), use this guide to keep results in an archive, retrieve
them, and run the same workflow in CI. [Selection rules](cli.md#test-sets-and-coverage),
[comparison semantics](compare.md) and [cluster runner setup](../deploy/README.md) have their own
references.

Set `$P` to the profile path and load its endpoint and credential variables before these commands.

## Publish a run

For AWS or an S3-compatible store using access keys, install the storage extra with
`./install.sh --s3`. Load credentials through the SDK's credential chain (including a session
token for temporary credentials), and set `$AWS_BUCKET` to your bucket. For a non-AWS endpoint,
set `AWS_ENDPOINT_URL_S3` (or `AWS_ENDPOINT_URL` when the first is unset).

For IBM IAM authentication, install `python -m pip install -e '.[ibm-cos]'`, set
`LLMDV_S3_AUTH=ibm-iam`, and export `IBM_COS_ENDPOINT`, `IBM_COS_API_KEY` and
`IBM_COS_SERVICE_INSTANCE_ID`. [IBM authentication details](../deploy/README.md#credentials)
cover token endpoints and alternatives.

To store a run where others can pull and list it, add `--remote-output` with the bucket
credentials loaded. `--evidence all` keeps a bundle for passing cases too, so a pass can be
audited:

```sh
llmdv verify --profile "$P" --analyze --evidence all \
  --remote-output "s3://$AWS_BUCKET/<prefix>" \
  --identity trigger=manual --label "actor=$USER"
```

Labels are free-form `key=value` pairs recorded in `outcome.json` and on the dashboard.
Use `--identity` for baseline matching; a label does not isolate a comparison lane.

A published run lives at `<prefix>/<profile name>/<run id>/` and is complete once its
`MANIFEST.json` is there. A local archive uses the same layout: pass
`--remote-output file:///absolute/path/to/archive` and compare with that URI. By contrast,
`--local-output ROOT` creates a fresh working directory under ROOT; it is not the archive root
that `compare --from` expects. See [output files](cli.md#output).

### Pull a run back

To pull a published run back:

```sh
PREFIX="s3://$AWS_BUCKET/<prefix>"
llmdv compare --from "$PREFIX" --profile <profile name> --list
RUN_ID='<run id from the list>'
aws s3 cp --recursive "$PREFIX/<profile name>/$RUN_ID/" "runs/$RUN_ID/"   # add --endpoint-url for an S3-compatible store
llmdv analyze "runs/$RUN_ID"
```

Open `runs/$RUN_ID/dashboard.html` in your browser. These `aws` commands require the AWS CLI
and access-key credentials; they do not use `LLMDV_S3_AUTH=ibm-iam`. With IAM-only credentials,
use an IBM-compatible download client.

The profile name is the profile's `name:` field, not its path. Run ids start with a UTC
timestamp, so they sort oldest first. `llmdv analyze` prints the counts and every failure group,
and rebuilds `dashboard.html`.

## Compare runs

`llmdv compare` reads two runs of the same profile back from the archive and reports which cases
changed their answer. Automatic baselines must have matching configuration identity, including trigger and flavor. See
[compare.md](compare.md).

## Run unattended

`llmdv job` runs `llmdv verify` as a child process, records its outcome, publishes results and
optionally notifies Slack, including when verification fails. That suits a Kubernetes CronJob or a CI step; the
[`Containerfile`](../Containerfile) builds a runner image.

```sh
llmdv job --profile /etc/llmdv/profile.yaml --remote-output "s3://$AWS_BUCKET/<prefix>" --full-suite -- -m contract
```

- **Settings** come from flags or the environment: `LLMDV_PROFILE`, `LLMDV_RESULTS_URI`,
  `LLMDV_NOTIFY_WEBHOOK`, and the rest in `llmdv job --help`. Set the webhook only when you
  want notification; keep its value in your credential store.
- **Flags the job does not know** go to `llmdv verify`, such as `--full-suite` or `--evidence all`.
  Arguments after `--` reach pytest unparsed, such as `-- -m contract`. `--analyze` and
  `--compare` are the job's own: a dashboard in the run, and a comparison against the archive.
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

| Symptom | Check |
| --- | --- |
| Missing credentials or access denied | Confirm credentials are exported or available through the selected SDK's credential chain, are unexpired, and grant access to this bucket/prefix. If sourcing a private environment file, export its variables. |
| `InvalidAccessKeyId` or `NoSuchBucket` | Confirm bucket, region, auth mode and endpoint; an S3-compatible store may need `AWS_ENDPOINT_URL_S3` and the AWS CLI's `--endpoint-url`. |
| `compare --list` finds no runs | Confirm archive prefix and profile `name:` (not YAML path), and that publication completed with `MANIFEST.json`. |
| Exit `3` | Read the reported environment failure. Preflight may stop before tests; a local connection can also fail mid-run. Check the summary for what executed. |
| Verification passed but results or notification are missing | Check delivery status in `outcome.json`, fallback location and logs. Exit `5` identifies delivery failure after a successful verification; it does not override a verification failure. |
| `trigger.sh` is refused | Check the `llmdv-trigger` Role and that a supplied `--profile-file` declares `vantage: in-cluster`. |
| Many local timeouts | Check machine sleep, local forwarding and competing live runs; retain the evidence before rerunning. |
