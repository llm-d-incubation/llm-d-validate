# Smoke tests in llm-d's nightly CI

[`smoke-step.sh`](smoke-step.sh) runs `llmdv`'s smoke test set against an llm-d guide's router
from inside llm-d's nightly CI, after the nightly has stood the guide up. It is meant for one
step in llm-d-infra's reusable nightly workflow, after the benchmark and before teardown, run
from a clone of the llm-d-validate release it should use:

```sh
git clone -q --depth 1 --branch "v$V" https://github.com/llm-d-incubation/llm-d-validate.git "$RUNNER_TEMP/llmdv"
LLMDV_VERSION="$V" bash "$RUNNER_TEMP/llmdv/deploy/llm-d-nightly/smoke-step.sh"
```

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
| `LLMDV_IMAGE`, `LLMDV_PACKAGE` (optional) | test image and local wheel; the installed wheel must match `LLMDV_VERSION` |
| `LLMDV_READY_TIMEOUT`, `LLMDV_TIMEOUT`, `LLMDV_FINALIZE_TIMEOUT` (optional) | seconds allowed for pod startup (600), verification (900), and graceful finalization (60) |

The lane map, [`overlays.yaml`](overlays.yaml) beside the script, and the profiles come from the
checkout the script sits in, so the data, the script and the image are one version. The map lists
every lane the guide READMEs name: under `lanes`, by scenario and overlay, with the profile and
`--identity` flags of the lane's row in its README's maintainers' table; under `not_run`, the lanes
that deploy with their own steps rather than llm-d-infra's reusable workflow, which this step
cannot run on.

## What it does

1. Validates `overlays.yaml` and looks up the scenario and overlay. A valid map with no row
   skips; an unreadable/malformed map or failing `yq` reports a setup error.
2. Finds the `<segment>-epp` Service and its HTTP port, as llm-d-benchmark does: port 80, else a
   port named `http` or `https`, else the one targeting 8081.
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
   Keeps available diagnostics and partial files if collection fails, then cleans up the pod
   and ConfigMap. An abrupt runner kill can still prevent collection.
6. Writes `integration.json`, GitHub step outputs, and a job summary that distinguish the
   verification verdict from setup and collection failures. The private virtual environment
   is removed before upload. Collection success means files reached the runner; the workflow
   must separately report whether artifact upload succeeded.

`llmdv` itself changes nothing in the cluster. The pod and the ConfigMap are created and deleted
by the nightly's CI, as its own endpoint check and benchmark harness pods are.

## Exit status

| Exit | When |
| --- | --- |
| `0` | no recorded verification exit `1`: pass, skip, setup error, environment/tool error, or interruption; read the separate statuses |
| `1` | verification recorded exit `1`, regardless of whether evidence delivery succeeded |

The shell entrypoint delegates bounded subprocess handling to `smoke_step.py`. The pod helper
keeps the verification exit and files available until collection. All cluster writes remain
in this CI wrapper, outside llmdv's read-only core.

`integration.json` and `$GITHUB_OUTPUT` expose `status` (`completed`, `skipped`, `setup_error`,
`timed_out`, or `interrupted`), `verification_exit` (empty/null if no exit was collected), and
`collection_status` (`not_attempted`, `complete`, or `failed`). A green non-blocking job does
not mean verification passed: consumers must use these fields.

The upstream workflow should always tolerate setup/collection/upload step failures, then use
a separate gate conditioned on `llmdv_blocking == 'true'` **and** the recorded
`verification_exit == '1'`. Failure categorization should read that gate, not the setup step.
Artifact upload needs its own `continue-on-error: true`; report its failure in the summary.

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
