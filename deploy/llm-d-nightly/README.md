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

## What it reads

The variables the nightly job already sets, and two of its own:

| Variable | Used for |
| --- | --- |
| `LLMDBENCH_CICD_SCENARIO` | the scenario (`optimized-baseline`, `multimodal-serving/aggregation`); its last path segment names the router Service, `<segment>-epp` |
| `LLMDBENCH_CICD_ACCELERATOR`, `_BACKEND`, `_CONNECTOR`, `_OFFLOADING_TARGET`, `_INFRA_PROVIDER` | the overlay the lane deployed: those values joined with `/`, empty ones dropped (`gpu/vllm/gke`, `tpu/v5p/vllm/base`) |
| `LLMDBENCH_CICD_NS` | the namespace the guide is deployed in |
| `RUNNER_TEMP` | where results are written: `$RUNNER_TEMP/llmdv-runs` |
| `LLMDV_VERSION` | the release: the PyPI package used on the runner and the image used in the pod |
| `LLMDV_IMAGE` (optional) | another image for the pod, for testing this script |
| `LLMDV_READY_TIMEOUT`, `LLMDV_TIMEOUT` (optional) | seconds to wait for the pod to start (600) and for `llmdv` to finish (900) |

The lane map, [`overlays.yaml`](overlays.yaml) beside the script, and the profiles come from the
checkout the script sits in, so the data, the script and the image are one version. The map lists
every lane the guide READMEs name: under `lanes`, by scenario and overlay, with the profile and
`--identity` flags of the lane's row in its README's maintainers' table; under `not_run`, the lanes
that deploy with their own steps rather than llm-d-infra's reusable workflow, which this step
cannot run on.

## What it does

1. Looks the scenario and overlay up in `overlays.yaml`. An overlay with no row is skipped.
2. Finds the `<segment>-epp` Service and its HTTP port, as llm-d-benchmark does: port 80, else a
   port named `http` or `https`, else the one targeting 8081.
3. Writes the profile as one file (`llmdv profile show --yaml`) and loads it as a ConfigMap.
4. Starts one pod from the release image, with the settings of
   [`../base/cronjob.yaml`](../base/cronjob.yaml) (non-root, read-only root filesystem, no
   capabilities, no Kubernetes API token). The pod runs
   `llmdv verify --test-set smoke --analyze`, with the row's `--identity` flags, against
   `http://<segment>-epp:<port>`, and waits to be
   collected.
5. Copies the run directory and the pod's console to `$RUNNER_TEMP/llmdv-runs`, deletes the pod
   and the ConfigMap, and adds what it did to the job summary.

`llmdv` itself changes nothing in the cluster. The pod and the ConfigMap are created and deleted
by the nightly's CI, as its own endpoint check and benchmark harness pods are.

## Exit status

| Exit | When |
| --- | --- |
| `0` | the smoke tests passed; or nothing was tested: no profile for the overlay, no `<segment>-epp` Service, a pod that did not start, a timeout, or `llmdv` exiting `2` (the profile) or `3` (the environment, such as an unreachable router), each reported as a warning |
| `1` | `llmdv` exited `1`: the deployment did not behave as its profile declares |

Only `1` is a failure of the guide, so a workflow can make the step blocking without charging
llm-d for a profile mistake or a CI problem.

## Testing it

[`.github/workflows/smoke-step-e2e.yaml`](../../.github/workflows/smoke-step-e2e.yaml) runs the
script on a kind cluster, in namespaces that enforce the `restricted` Pod Security Standard,
against a stand-in router (this repository's fake fleet behind an `optimized-baseline-epp`
Service): a passing router, a router with a defect, an overlay with no profile, a namespace with
no router Service, and a Service with nothing behind it. The harness is in
[`tests/e2e_llm_d_nightly/`](../../tests/e2e_llm_d_nightly/).
