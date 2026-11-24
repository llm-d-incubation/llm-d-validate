# flow-control

Profiles for llm-d's [flow-control guide](https://github.com/llm-d/llm-d/tree/main/guides/flow-control) at llm-d `9ca8ff0`: the llm-d router with its `flowControl` feature gate on, priority bands 100, 0 and -10, round-robin fairness and FCFS ordering, in front of optimized-baseline's vLLM model servers. Every profile here is WRITTEN, NOT RUN.

The profiles test the router (EPP) each lane exposes, in standalone mode, through `../parts/topology/standalone.yaml`. The helm release is `flow-control`, so the Service is `<GUIDE_NAME>-epp` on port 80 (the lanes' `gateway_host`).

## Lanes

Both lanes run `guides/flow-control/scripts/nightly-deploy-gke.sh` as a `custom_deploy_script`, which renders optimized-baseline's model-server overlay, relabels it for this guide and cuts it to one replica. They then run `.github/scripts/e2e/e2e-validate-flow-control.sh`.

| CI lane (`nightly-e2e-flow-control-<lane>`) | Reusable workflow | Overlay it renders | Profile | Identity flags |
| --- | --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `reusable-nightly-e2e-gke` | `optimized-baseline/modelserver/gpu/vllm/gke` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `amd-ci-acc-rocm-vllm-x` | `reusable-nightly-e2e-amd` | `optimized-baseline/modelserver/amd/vllm/base` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=base` |

The provider value is the `INFRA_PROVIDER` the lane passes to the deploy script.

```sh
export LLMDV_EPP_URL=<the EPP's URL>
llmdv verify --profile profiles/llm-d/flow-control/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

## What llmdv sees of flow control

Flow control is work-conserving: a request that finds a free slot dispatches at once. llmdv sends one request at a time, so on a correct stack a client sees this router as it would see optimized-baseline's, but for one response header: flow control adds `x-llm-d-flow-queue-duration-ms` (the time the request waited) to every request it admits, before the first byte, and the router sends no such header without it (llm-d-router `pkg/epp/handlers/response.go`, at the `main` build `guides/env.sh` pins). `parts/common.yaml` declares it `promised`, so `contract.header_disposition` fails a lane whose router runs without flow control. The guide's other claims are about contention, and none of them is covered yet:

- **NOT COVERED: queueing, backpressure, priority, fairness and shedding** (`requires_load`, #23). Holding requests past `maxConcurrency` × ready replicas, best-effort waiting longer than premium, round-robin between tenants in a band, and rejection with 429, 503 or 500 carrying `x-llm-d-request-dropped-reason`. The CI lane forces these with `maxConcurrency: 4` and one replica; llmdv has no load mode to produce them.
- **NOT COVERED: classification.** The objective and fairness headers (`x-llm-d-inference-objective`, `x-llm-d-inference-fairness-id`, and their deprecated `x-gateway-inference-*` aliases) are declared `consumed` in the topology part, which skips as `requires_load`. That declaration still holds with flow control on: the EPP reads them and their effect shows only under contention.
- **NOT COVERED: the feature gate's log line, `maxConcurrency` and the EPP's `llm_d_epp_flow_control_*` series.** They need EPP logs, config reads and the metrics port, which a committed profile does not carry. The header above is what a client sees of the gate.

One case sends concurrent requests: `scenarios.cancel_burst` opens eight streams at once. Under the CI setting, four dispatch and four wait in the EPP until a cancelled stream frees a slot, well inside `defaultRequestTTL` (60s) and the standard band's 200-request limit. A rejection there is a finding.

`parts/common.yaml` maps each of the guide's claims to a case or a gap.

## Upstream notes

- The guide's `guide.yaml` has no `support:` table, so no configuration is marked `validated` there; these profiles follow the two lanes. Its `INFRA_PROVIDER` values are `base` and `gke`, and its `deploy.modelserver` step names only the `gpu` overlay; the AMD path is documented in the README and run by the `amd-ci` lane.
- The README's "Default Configuration" table says 2 replicas; both lanes run one (`nightly-deploy-gke.sh`, CI-only deviation 3 of 3).
- The AMD lane runs the script named `nightly-deploy-gke.sh` through `reusable-nightly-e2e-amd`.
