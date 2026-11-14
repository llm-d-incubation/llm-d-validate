# Running `llmdv` unattended

Deploy `llmdv job` to a cluster, give it credentials, start runs on demand, and read what they
left. Object storage and Slack are optional. Runs from your PC or CI: [docs/end-to-end.md](../docs/end-to-end.md).

| File | What it is |
| --- | --- |
| [`trigger.sh`](trigger.sh) | Starts a one-off run from the CronJob's own pod spec, optionally with a per-run profile |
| [`infra-smoke.sh`](infra-smoke.sh) | Checks the runner and S3 publication before a fleet is available |
| [`base/`](base/) | Kustomize base: the CronJob, and a Role for whoever triggers runs |
| [`overlays/example/`](overlays/example/) | An overlay with placeholders, showing what a real one supplies |
| [`secrets.env.example`](secrets.env.example) | Empty credential fields to fill in outside the repository |
| [`../Containerfile`](../Containerfile) | The image: llmdv with both publishing backends and pinned Claude Code, running `llmdv job` |

Real overlays carry cluster detail, so they live with the fleet's own manifests, not here.

## Deploying

1. Build and push the image, from the repository root:
   ```sh
   podman build -f Containerfile --build-arg LLMDV_GIT_SHA="$(git rev-parse HEAD)" \
       -t <registry>/<org>/llmdv:"$(git rev-parse --short HEAD)" .
   podman push <registry>/<org>/llmdv:"$(git rev-parse --short HEAD)"
   ```
2. Write an overlay from [`overlays/example/`](overlays/example/): namespace, image tag, an
   in-cluster profile (Service DNS, empty `setup`; check it with `llmdv profile validate`),
   `LLMDV_RESULTS_URI`, and the schedule.
3. Optionally create `llmdv-secrets` ([below](#creating-and-updating-the-credentials-secret)).
   Without it, runs keep local artifacts and send no Slack message.
4. `kubectl apply -k <overlay>`.
5. Run it once by hand before trusting the schedule, and check the bucket and Slack:
   ```sh
   job="$(deploy/trigger.sh -n <namespace> --trigger manual --id first-run)" &&
   kubectl -n <namespace> logs -f "job/$job"
   ```

### Infrastructure smoke test

Before a model is deployed, check image startup, non-root execution, writable `/tmp` and
`LLMDV_WORK_DIR`, and S3 authentication and upload. Apply your overlay with the CronJob suspended;
the profile ConfigMap must exist if the pod mounts it, but the probe never loads it. From
the repository root, with Bash and `jq`:

```sh
smoke_job="$(bash deploy/infra-smoke.sh -n <namespace> --cronjob llmdv)" &&
kubectl -n <namespace> logs -f "job/$smoke_job" --pod-running-timeout=180s &&
kubectl -n <namespace> wait --for=condition=complete "job/$smoke_job" --timeout=300s
```

On OpenShift pass `--cli oc`. The Job runs [`infra-smoke.py`](infra-smoke.py) from the CronJob's
pod spec with a five-minute deadline, and must publish `infrastructure.json` and `MANIFEST.json`
under `<LLMDV_RESULTS_URI>/infra-smoke/<run id>/`. It exits 0, or 2 (invalid destination), 3 (local
problem) or 5 (publication failed). It does not test inference, `llmdv job` or Slack.

## Credentials

### Creating and updating the credentials Secret

The CronJob reads a Secret named `llmdv-secrets` in its own namespace through `envFrom`. The
reference is optional, so the pod starts without it. Nothing creates it for you.

1. Copy [`secrets.env.example`](secrets.env.example) to a private path outside Git and
   `chmod 600` it. Fill only what you use, as `KEY=value` without quotes or `export`. Never put
   credentials in a profile, ConfigMap, image or committed manifest.
2. Create or update the Secret:
   ```sh
   kubectl --context <context> -n <namespace> create secret generic llmdv-secrets \
       --from-env-file=/secure/path/llmdv-secrets.env --dry-run=client -o yaml |
       kubectl --context <context> -n <namespace> apply -f -
   ```
   Repeat after editing to rotate. New pods get the new values; running pods keep theirs.
3. Keep nonsecret settings (`LLMDV_RESULTS_URI`, region, `AWS_ENDPOINT_URL`) in the overlay's
   `llmdv-settings`. The S3 identity needs object-write access under the destination, including
   `<prefix>/.llmdv-write-check`, which preflight writes.

For an authenticated inference endpoint, add the key the profile names to the same Secret, e.g.
`INFERENCE_API_KEY`, referenced as `auth: {type: bearer, key_env: INFERENCE_API_KEY}`. Unlike S3
and Slack credentials, it is required: a missing key fails preflight.

### Optional credentials and delivery

`llmdv job` runs with neither S3 credentials nor a Slack webhook:

| Configuration | Behaviour of `llmdv job` |
| --- | --- |
| No `LLMDV_RESULTS_URI` / `--remote-output` | logs that publication is skipped; keeps local artifacts |
| S3 destination, credentials for the auth mode missing | logs why, skips the S3 preflight write and uploads, runs verification normally; `outcome.json` records `publication.status: skipped` and the reason |
| S3 credentials available | checks the destination and publishes |
| `LLMDV_NOTIFY_WEBHOOK` missing, empty or whitespace | logs that notification is skipped |
| Webhook available | sends the run summary, including any skipped-publication reason |
| A configured destination or webhook rejects delivery | an error: exit `5` if verification passed |

The `aws` mode uses [boto3's credential chain](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html),
so missing access-key variables alone do not disable publication; `ibm-iam` checks only its two
variables. This leniency is `llmdv job`'s alone: `llmdv verify --remote-output`, `llmdv publish` and
`llmdv notify` treat missing credentials as errors.

<details>
<summary><b>IBM Cloud Object Storage with IAM</b></summary>

With IBM COS [HMAC credentials](https://cloud.ibm.com/docs/cloud-object-storage?topic=cloud-object-storage-uhc-hmac-credentials-main),
use the default `LLMDV_S3_AUTH=aws` with `AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`.

A storage Secret holding `api-key` and `service-instance-id` uses IAM instead, through
[`ibm-cos-sdk`](https://cloud.ibm.com/docs/cloud-object-storage?topic=cloud-object-storage-python)
(the `ibm-cos` extra; the image includes it). The SDK exchanges the key for a token and refreshes
it; llmdv registers the key and tokens for redaction. In `llmdv-settings`:

```yaml
literals:
  - LLMDV_RESULTS_URI=s3://example-bucket/llmdv
  - LLMDV_S3_AUTH=ibm-iam
  - IBM_COS_ENDPOINT=https://s3.<region>.cloud-object-storage.appdomain.cloud
```

Supply `IBM_COS_API_KEY` and `IBM_COS_SERVICE_INSTANCE_ID` in `llmdv-secrets`, or reference an
existing Secret from the `llmdv` container's `env` in your overlay:

```yaml
- name: IBM_COS_API_KEY
  valueFrom:
    secretKeyRef:
      name: cos-credentials
      key: api-key
      optional: true
- name: IBM_COS_SERVICE_INSTANCE_ID
  valueFrom:
    secretKeyRef:
      name: cos-credentials
      key: service-instance-id
      optional: true
```

`optional: true` lets `llmdv job` skip publication if a key is absent; a rejected key is a delivery
failure. Outside the cluster, install `llmdv[ibm-cos]`, supply the two variables decoded (not
base64), and:

```sh
export LLMDV_S3_AUTH=ibm-iam
export IBM_COS_ENDPOINT=https://s3.<region>.cloud-object-storage.appdomain.cloud
llmdv verify --profile <profile> --remote-output s3://example-bucket/llmdv
```

`IBM_COS_AUTH_ENDPOINT`, if overridden, is the full token URL ending in `/identity/token`. No
bucket is created. Before switching the CronJob, prove a real upload from the intended
environment with `llmdv publish <unpublished run> --remote-output s3://example-bucket/llmdv/staging`
and check for `MANIFEST.json`.

</details>

## Environment

| Variable | Source | Meaning |
| --- | --- | --- |
| `LLMDV_RESULTS_URI` | `llmdv-settings` ConfigMap | `s3://bucket/prefix`; unset keeps results in the pod only |
| `LLMDV_S3_AUTH` | `llmdv-settings` ConfigMap | `aws` (default) or `ibm-iam` |
| `AWS_ENDPOINT_URL` | `llmdv-settings` ConfigMap | only for an S3-compatible store |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | `llmdv-secrets` Secret | or any other source in boto3's credential chain |
| `AWS_SESSION_TOKEN` | `llmdv-secrets` Secret | also needed with temporary credentials |
| `AWS_DEFAULT_REGION` | `llmdv-settings` ConfigMap | the region your object store requires |
| `IBM_COS_ENDPOINT` | `llmdv-settings` ConfigMap | required S3 service URL in `ibm-iam` mode |
| `IBM_COS_API_KEY`, `IBM_COS_SERVICE_INSTANCE_ID` | Secret | required in `ibm-iam` mode; decoded API key and instance ID/CRN |
| `IBM_COS_AUTH_ENDPOINT` | `llmdv-settings` ConfigMap | optional full IAM token URL; defaults to `https://iam.cloud.ibm.com/identity/token` |
| `LLMDV_NOTIFY_WEBHOOK` | `llmdv-secrets` Secret | Slack incoming-webhook URL; unset sends nothing. Never logged |
| `LLMDV_FALLBACK_URI` | overlay patch | `file:///results` on an existing PVC; used only when `LLMDV_RESULTS_URI` is unavailable (`--fallback-output`) |
| `LLMDV_RUN_BUDGET` | CronJob | seconds before tests are interrupted (`--budget`, default 3000); keep it below `activeDeadlineSeconds` |
| `LLMDV_PROFILE` | default | `/etc/llmdv/profile.yaml`, from the `llmdv-profile` ConfigMap (`--profile`) |
| `LLMDV_WORK_DIR` | image | `/work/runs`, where run directories go (`--work-dir`) |

To use a fallback PVC you already have, uncomment the patch in
[`overlays/example/kustomization.yaml`](overlays/example/kustomization.yaml) and set its
`claimName`. Nothing creates the PVC.

## Running on demand

`trigger.sh` creates a Job from the CronJob's template with only its arguments replaced, prints
the Job's name, and returns:

```sh
deploy/trigger.sh -n <namespace> --trigger manual --id "$RUN_ID" -- --compare
deploy/trigger.sh -n <namespace> --id "$ROLLOUT_ID" -- --client forge --label deployment="$IMAGE_DIGEST"
deploy/trigger.sh -n <namespace> --trigger manual --profile-file /secure/path/profile.yaml -- -m contract
```

| Option | Default | Meaning |
| --- | --- | --- |
| `-n` | required | the CronJob's namespace |
| `--trigger` | `rollout` | becomes `--label trigger=<value>`, replacing the job's default `trigger=nightly` |
| `--id` | UTC timestamp | becomes `--label <trigger>=<id>` and names the Job |
| `--cronjob` | `llmdv` | the CronJob to copy |
| `--cli` | `kubectl` | or `oc` |
| `--profile-file` | none | a local in-cluster profile (top-level `vantage: in-cluster`) for this run only |

Arguments after `--` replace the CronJob's `args:` entirely, so pass selection and comparison
options explicitly. The caller waits on the Job with its own tooling and needs the `llmdv-trigger`
Role: get CronJobs, create Jobs and ConfigMaps (the last only for `--profile-file`, whose
ConfigMap is removed with the Job). Label a rollout run with an immutable deployment reference.

On-demand runs are never `--compare` baselines; see [docs/compare.md](../docs/compare.md). A triggered run
can overlap a nightly one, but run ids never collide. For on-demand only, suspend the CronJob.

## What one run does

```
llmdv job [llmdv verify arguments...]
  1. resolve optional S3 credentials; if unavailable, log and keep results local
  2. llmdv verify, as a child process, within $LLMDV_RUN_BUDGET seconds
  3. compare     with --compare
  4. publish     when enabled; a no-op if verify published, otherwise uploads what it left
  5. notify      if LLMDV_NOTIFY_WEBHOOK is nonempty
  6. print outcome.json to the job log, then alerts.json as its last line
  7. exit: the verification code if non-zero (a 5 is superseded by step 4), else 1 on a regression,
     else 2 if --compare had no archive, else 5 if an enabled delivery failed, else 0
```

Selection goes in the Job's `args:`. `--profile`, `--work-dir` and `--remote-output` are
`llmdv job`'s own; `--artifact-sink` passes through to verify. `--analyze` adds `dashboard.html`,
built after the comparison and published with
the run. The pod never calls the Kubernetes API, so it needs no token or Role. Verify runs as a
separate process so that `llmdv job` outlives it:

| The run... | What happens |
| --- | --- |
| passes or fails | verify publishes it |
| hits an invalid profile or a failed preflight | verify publishes the bundle, with `outcome.json` saying which |
| runs out of budget, or the pod is stopped | verify gets SIGTERM, stops testing, publishes and notifies within the grace period; exit `6` |
| is killed outright (OOM) | `llmdv job` uploads what it left, recorded as crashed with its exit code (137) |

Exit codes are those of [`llmdv verify`](../docs/manual.md#exit-codes); 137 and other signal codes mean
verify was killed and `llmdv job` uploaded what it left.

### Publishing and the manifest

`--remote-output` publishes to `s3://bucket/prefix` (S3 or any S3-compatible store; the `s3`
extra) or `file:///path`, at `<prefix>/<profile name>/<run id>/`. The destination is checked
writable at preflight, so a misconfigured bucket fails before a long run.

The bundle is frozen before it is hashed (`llmdv.log` is closed first), and `MANIFEST.json` is
published **last**. A run is published if and only if its manifest exists; the manifest means
"publication complete", not "tests passed": the verdict is in `outcome.json`. If one file fails
to upload the rest still go, but the manifest is withheld. Retention is a bucket lifecycle rule.

When S3 is unavailable and a fallback is configured, the bundle goes there with the same layout;
`outcome.json` and the Slack message name the destination that failed and why.

The two delivery steps are also ordinary commands:

```sh
llmdv publish runs/<run> --remote-output s3://bucket/llmdv   # records a killed run as crashed; no-op if already published
llmdv notify runs/<run>                                    # posts outcome.json to $LLMDV_NOTIFY_WEBHOOK
```

`llmdv notify` retries a failing webhook within bounds and never logs its URL. Everything published
is redacted at capture, in the logging layer and in every run-file writer.

## Claude Code in the runner

The image includes the native Claude Code binary at `/usr/local/bin/claude` (amd64 and arm64),
the version in the [Containerfile](../Containerfile)'s `CLAUDE_CODE_VERSION`, currently
**2.1.289**, checksum-verified, with background updates disabled. It works with a read-only root
filesystem and a non-root UID. No Claude credentials are baked in.

Declare it in the profile mounted at `/etc/llmdv`, on an ingress that also declares `messages`:

```yaml
clients:
  - id: claude-code
    surfaces: [messages]
    binaries:
      - version: "2.1.289"
        executable: /usr/local/bin/claude
```

Start with `--client claude-code -k test_claude_code_smoke`. Tool-using sessions execute
model-generated code: run them only in a disposable pod, and size the memory limit for them; the
base's 1 GiB is for the ordinary runner.

To check the executable under the same restrictions, without calling a model:

```sh
podman run --rm --network none --read-only --user 10001:0 \
    --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /work:rw,mode=1777 --tmpfs /tmp:rw,mode=1777 \
    --entrypoint /usr/local/bin/claude <image> --version
```

## Refreshing the image's pins

The base image is pinned by digest, every Python package by hash, and Claude Code by version and
checksum. To update the base or Python packages, from the repository root:

```sh
uv pip compile pyproject.toml --extra s3 --extra ibm-cos --universal --generate-hashes \
    --python-version 3.11 --no-header -o requirements.lock    # then restore its header comment
curl -sI -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
    https://registry.access.redhat.com/v2/ubi9/python-311/manifests/latest | grep -i digest
```

Put the new digest in the Containerfile's `FROM`, rebuild, and run the tests.
`tests/test_requirements_lock.py` fails if `pyproject.toml` gains a dependency the lock lacks,
and checks both publishing extras: dropping `--extra ibm-cos` would silently remove
`ibm-cos-sdk` from the image.

To update Claude Code, take the `linux-x64` and `linux-arm64` checksums from
`https://downloads.claude.ai/claude-code-releases/<version>/manifest.json`
([verification guidance](https://code.claude.com/docs/en/setup#binary-integrity-and-code-signing)),
and update `CLAUDE_CODE_VERSION`, both `CLAUDE_CODE_SHA256_*` defaults, the manifest comment and
the version in profiles and docs together. Rebuild and repeat the check above per architecture.

## Limits

- A lost node, an image-pull failure or a Job that never starts leaves nothing to publish. Use
  platform alerts for failed Jobs and missed schedules.
- `/work` is an `emptyDir`: without S3, copy artifacts out before the pod goes, or mount
  persistent storage. `outcome.json` always survives in the job log.
- The image carries neither `kubectl` nor `oc`, so pod logs are not collected; the run says
  `no pod logs: neither kubectl nor oc is on PATH`. To collect them, add a CLI and a Role
  granting `get`/`list` on pods and `get` on `pods/log`.
- `fleet.config` rules skip as `requires_instrumentation` without a CLI. They need `get`/`list`
  on each kind a rule names, plus `pods`, and `endpointslices` or `endpoints` where a rule checks
  selectors or ready endpoints. Log-signature cases need pod-log access. Grant nothing on
  Secrets; the loader refuses rules that read them.
