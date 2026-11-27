# Smoke tests in llm-d's nightly CI

[`smoke-step.sh`](smoke-step.sh) runs the built-in smoke test set against an llm-d guide's
router after benchmarking and before teardown. CI orchestrates `llmdv verify` through
separate `prepare`, `verify`, and `finalize` phases; this wrapper does not use `llmdv job`.

## Scope and enablement

The proposed llm-d-infra integration is opt-in per caller and runs only with
`standup_method: kustomize`. An empty `llmdv_version` disables it. Setting a released
version enables router smoke validation; `llmdv_blocking: 'false'` keeps it advisory.

These are basic API checks through the router, not the full suite or a guide's default
test selection. They do not establish routing, caching, autoscaling, or performance
correctness, and do not select gateway or engine ingresses.

Automatic selection requires an exact scenario/overlay entry in [`overlays.yaml`](overlays.yaml)
from the selected profile checkout. An unmapped configuration skips. The map's `not_run`
section lists configurations that use separate workflows: agentic API, async multitenant,
flow control, pod snapshot, and KEDA/EPP autoscaling. Their profiles exist, but adding this
wrapper to the reusable benchmark workflow does not wire it into those other workflows.
A map entry alone also does not override the caller's kustomize requirement.

## Invoke the phases

The CI job supplies `LLMDV_VERSION`, `RUNNER_TEMP`, and the deployment variables listed
below. Clone the release once before preparation:

```sh
git clone -q --depth 1 --branch "v$LLMDV_VERSION" https://github.com/llm-d-incubation/llm-d-validate.git "$RUNNER_TEMP/llmdv"
```

Each command below belongs in its **own Actions step**, using the same `RUNNER_TEMP`.
They are not a script to paste into a single step: `exec` replaces that step's shell so
signals reach the wrapper.

Preparation validates the profile, creates the pod and ConfigMap, and waits for readiness.
**Verification requests begin when the pod starts.**

```sh
exec bash "$RUNNER_TEMP/llmdv/deploy/llm-d-nightly/smoke-step.sh" prepare
```

The verify step waits for that run's result. Run it only when preparation reports
`status=prepared` and the job has not been cancelled:

```sh
exec bash "$RUNNER_TEMP/llmdv/deploy/llm-d-nightly/smoke-step.sh" verify
```

The finalize step uses `always()` and runs when recovery state exists, including after
cancellation. It stops unfinished verification, collects evidence, and deletes resources:

```sh
if [ -f "$RUNNER_TEMP/llmdv-state.json" ]; then
  exec bash "$RUNNER_TEMP/llmdv/deploy/llm-d-nightly/smoke-step.sh" finalize
fi
```

The separate phases and recovery fixes require a release containing
[#235](https://github.com/llm-d-incubation/llm-d-validate/pull/235) and
[#236](https://github.com/llm-d-incubation/llm-d-validate/pull/236);
**0.1.4 is the first release with both changes.** Use its matching package, image,
and checkout.
Version 0.1.3 supports only the single-process entrypoint and cannot be used with these
steps. No-argument mode remains available for callers that want one process.

Resource identity must be saved atomically to `$RUNNER_TEMP/llmdv-state.json` before
cluster writes. Later checkpoints preserve verdicts when storage is writable; reporting
failures do not prevent cleanup. It is runner-local recovery state, outside the uploaded `llmdv-runs`
directory. Preparation and verification exit promptly on cancellation; the independent
finalizer owns stopping, collection, and deletion. It preserves partial copy attempts and
records verdicts before cleanup. Recovery is bounded to roughly four minutes, with cleanup
reserved separately, rather than depending on the runner's ten-second signal window.
Actions passes `LLMDV_CANCELLED=true` to shorten recovery to roughly 80 seconds during
workflow cancellation, leaving time for guide teardown before artifact upload.
A dead runner or forced cancellation can still prevent recovery; the pod deadline stops
abandoned work but cannot delete its Kubernetes objects.

The design and the reasons for it are in
[#230](https://github.com/llm-d-incubation/llm-d-validate/issues/230).

The initial integration attaches each run to GitHub Actions as an artifact. It does not
publish to S3, fetch a baseline, or invoke `llmdv compare`. A failure means this deployment
did not meet its profile's smoke expectations; it is not automatically classified as a new
regression. Cross-run regression tracking is a separate follow-up requiring a retained
results store and baseline-selection policy.

## What it reads

The variables the nightly job already sets, plus the release and optional test settings:

| Variable | Used for |
| --- | --- |
| `LLMDBENCH_CICD_SCENARIO` | the scenario (`optimized-baseline`, `multimodal-serving/aggregation`); its last path segment names the router Service, `<segment>-epp` |
| `LLMDBENCH_CICD_ACCELERATOR`, `_BACKEND`, `_CONNECTOR`, `_OFFLOADING_TARGET`, `_INFRA_PROVIDER` | the overlay the lane deployed: those values joined with `/`, empty ones dropped (`gpu/vllm/gke`, `tpu/v5p/vllm/base`) |
| `LLMDBENCH_CICD_NS` | the namespace the guide is deployed in |
| `RUNNER_TEMP` | where results are written: `$RUNNER_TEMP/llmdv-runs` |
| `LLMDV_VERSION` | the release: the PyPI package used on the runner and the image used in the pod |
| `LLMDV_PROFILE_ROOT` (optional) | a separate profile checkout; defaults to the runtime checkout |
| `LLMDV_PROFILE` (optional) | explicit YAML path relative to that checkout; bypasses the map and its identity flags |
| `LLMDV_PROFILE_REPOSITORY`, `LLMDV_PROFILE_REF` (optional) | source labels for provenance; commit IDs are resolved from the actual checkouts |
| `LLMDV_IMAGE`, `LLMDV_PACKAGE` (optional) | test image and local wheel; the installed wheel must match `LLMDV_VERSION` |
| `LLMDV_CANCELLED` (optional) | set to `true` in the finalizer when the Actions job was cancelled; shortens its recovery budget |
| `LLMDV_READY_TIMEOUT`, `LLMDV_TIMEOUT`, `LLMDV_FINALIZE_TIMEOUT` (optional) | seconds allowed for pod startup (600), verification (900), and graceful finalization (60) |

The CI workflow must fetch an independent profile checkout and pass its path as
`LLMDV_PROFILE_ROOT`. `LLMDV_PROFILE_REPOSITORY` and `LLMDV_PROFILE_REF` only label
provenance; setting them alone does not fetch or switch profiles.
An independent profile checkout supplies YAML only, never executable integration code.
The whole inheritance chain must stay in that checkout, including symlink targets. The
profile must declare `id: router`, `kind: router`, and
`base_url_env: LLMDV_ROUTER_URL`; an
inline URL or another variable is refused to avoid testing an unrelated deployment. The
pinned runtime exports and plans the selected `router` ingress and `smoke` set offline
before creating a pod; incompatible capabilities or selection errors report setup failure.
`profile-source.json` records the selected path, requested ref, resolved source commit,
and runtime version/commit. An explicit profile retains profile-derived identity; the
map's accelerator, provider, and flavor flags are not added. Without an explicit path, the map comes from the profile checkout.

The lane map, [`overlays.yaml`](overlays.yaml) beside the script, and the profiles come from the
profile checkout. By default it is the runtime checkout, so all parts use one release. The map lists
every lane the guide READMEs name: under `lanes`, by scenario and overlay, with the profile and
`--identity` flags of the lane's row in its README's maintainers' table; under `not_run`, the lanes
that deploy with their own steps rather than llm-d-infra's reusable workflow, which this step
cannot run on.

## What it does

1. Uses an explicit `LLMDV_PROFILE` path if supplied; otherwise validates `overlays.yaml`
   and looks up the scenario and overlay. A valid map with no row
   skips; an unreadable/malformed map or failing `yq` reports a setup error.
2. Finds the `<segment>-epp` Service and its HTTP port, as llm-d-benchmark does: port 80, else a
   port named `http` or `https`, else the one targeting 8081, else the first declared port.
3. Installs the requested release in a private virtual environment, verifies its version,
   exports the profile (`llmdv profile show --yaml`), and loads it as a ConfigMap. An existing
   `llmdv` on the runner cannot change which exporter is used.
4. Starts one pod from the release image, with the settings of
   [`../base/cronjob.yaml`](../base/cronjob.yaml) (non-root, read-only root filesystem, no
   capabilities, no Kubernetes API token). The pod runs
   `llmdv verify --test-set smoke --analyze`, with the row's `--identity` flags, against
   `http://<segment>-epp:<port>`, and waits to be
   collected.
5. On timeout or cancellation, asks verification to stop and gives it a bounded finalization
   period before collecting. Copies the run directory with up to three attempts and checks
   finalized outcome, JSONL completeness, counts, and required artifacts before accepting it.
   Keeps available diagnostics and partial files if collection fails, then the finalizer cleans up the pod
   and ConfigMap, reserving deletion time for each. Abnormal verification exits and
   terminated containers are detected without waiting for the verification timeout;
   diagnostics retain the process exit or container termination reason. Unhandled
   SIGINT/SIGTERM exits are interruptions; other abnormal exits are tool failures,
   never a passing run or a guide failure. An abrupt runner kill can still prevent collection.
6. Writes `integration.json`, GitHub step outputs, and a job summary that distinguish the
   verification verdict from setup and collection failures. The private virtual environment
   is removed before upload. Collection success means files reached the runner; the workflow
   must separately report whether artifact upload succeeded. Report and log write failures
   do not prevent evidence collection or cleanup. Resource identity must be saved before
   creation; later recovery-state writes are best effort so storage errors cannot block cleanup.

`llmdv` itself changes nothing in the cluster. The pod and the ConfigMap are created and deleted
by the nightly's CI, as its own endpoint check and benchmark harness pods are.

## Find the evidence

The wrapper collects into `$RUNNER_TEMP/llmdv-runs`; the CI workflow uploads that directory.
`integration.json` at its root describes orchestration status, and `profile-source.json`
records the selected source when preparation reached that step. A successful collection
is under `runs/<run-directory>/`, containing `dashboard.html`, results, exchanges, logs,
the profile, and available failure evidence. Unsuccessful copies remain under
`runs.partial-*/` and may be incomplete. Setup failure may leave no verification run.

## Exit status

| Exit | When |
| --- | --- |
| `0` | no recorded verification exit `1`: pass, skip, setup error, environment/tool error, or interruption; read the separate statuses |
| `1` | verification recorded exit `1`, regardless of whether evidence delivery succeeded |

The shell entrypoint delegates bounded subprocess handling to `smoke_step.py`. The pod helper
keeps the verification exit and files available until collection. All cluster writes remain
in this CI wrapper, outside llmdv's read-only core.

`integration.json` and `$GITHUB_OUTPUT` expose `status` (`prepared`, `completed`, `skipped`, `setup_error`,
`timed_out`, or `interrupted`), `verification_exit` (empty/null if no exit was collected), and
`collection_status` (`not_attempted`, `complete`, or `failed`). A green non-blocking job does
not mean verification passed: consumers must use these fields. `completed` means the
verification process exited, not that it passed. `verification_exit` is `null` in JSON
and empty in step outputs when no verdict was obtained. `collection_status: complete`
means evidence reached the runner, not that the tests passed or artifact upload succeeded.
See [CLI exit codes](../../docs/cli.md#exit-codes) for verification verdict meanings.

The upstream workflow should always tolerate setup/collection/upload step failures, then use
a separate gate conditioned on `llmdv_blocking == 'true'` **and** the recorded
`verification_exit == '1'`. Failure categorization should read that gate, not the setup step.
Artifact upload needs its own `continue-on-error: true`; report its failure in the summary.
The optional gate requires test failures to block; it does not require validation to have
run. Existing benchmark and infrastructure failures still determine the nightly result.

## Testing it

[`.github/workflows/smoke-step-e2e.yaml`](../../.github/workflows/smoke-step-e2e.yaml) runs the
script on a kind cluster, in namespaces that enforce the `restricted` Pod Security Standard,
against a stand-in router (this repository's fake fleet behind an `optimized-baseline-epp`
Service): a passing router, a router with a defect, an overlay with no profile, a namespace with
no router Service, a Service with nothing behind it, and graceful timeout of a slow router. The harness is in
[`tests/e2e_llm_d_nightly/`](../../tests/e2e_llm_d_nightly/).

`tests/test_nightly_smoke_step.py` covers lookup failures, version isolation, collection retries,
partial files, timeout/cancellation ordering, and verdict precedence without a cluster. The
kind job supplies a wheel built from the same checkout as its image; the isolated runner still
checks its version. Production uses the pinned PyPI release.
