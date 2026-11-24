# keda-epp

Profiles for llm-d's [keda-epp guide](https://github.com/llm-d/llm-d/tree/main/guides/workload-autoscaling/keda-epp) (`guides/workload-autoscaling/keda-epp`) at llm-d `9ca8ff0`: optimized-baseline's model server, autoscaled by a KEDA `ScaledObject` on an EPP metric (flow-control queue size and running requests, or in-flight requests). Every profile here is WRITTEN, NOT RUN.

The profiles test the router (EPP) each lane exposes, in standalone mode, through `../parts/topology/standalone.yaml`. Each lane installs the router under its own helm release, so the Service differs per lane (each lane's `gateway_host`).

## Lanes

| CI lane (`nightly-e2e-workload-autoscaling-keda-epp-<lane>`) | Reusable workflow, deploy script (`guides/workload-autoscaling/scripts/`) | Overlay it renders | Router | Service | Profile | Identity flags |
| --- | --- | --- | --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `reusable-nightly-e2e-gke`, `nightly-deploy-gke-keda-epp.sh` | `optimized-baseline/modelserver/gpu/vllm/gke` | flow control on, queue `ScaledObject` | `keda-epp-gke-epp` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `ibm-acc-gpu-vllm-x` | `reusable-nightly-e2e-openshift`, `nightly-deploy-ocp-keda-epp.sh` | `optimized-baseline/modelserver/gpu/vllm/base` | optimized-baseline's, no flow control, inflight `ScaledObject` | `keda-epp-inflight-epp` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=base` |
| `amd-acc-rocm-vllm-x` | `reusable-nightly-e2e-amd`, `nightly-deploy-amd-keda-epp.sh` | `optimized-baseline/modelserver/amd/vllm/base` | flow control on, queue `ScaledObject` | `keda-epp-rocm-epp` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=base` |

The provider value is the overlay's `INFRA_PROVIDER` folder. The `gke` and `ibm` lanes share a profile: their routers differ only under contention, which a client sending one request at a time does not see, and their providers already tell their runs apart, so no flavor is needed.

```sh
export LLMDV_EPP_URL=http://keda-epp-gke-epp.<namespace>.svc.cluster.local
llmdv verify --profile profiles/llm-d/keda-epp/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

**Replicas change during a lane.** Each lane starts the model server at one replica, and its validator (`.github/scripts/e2e/e2e-validate-scale-event.sh`) drives the HPA to two and back. A run that overlaps a scale event may see a second replica join the pool, or stay Pending. No declaration here depends on the replica count.

## Not covered

- **Every scaling claim** (`requires_load`, #23, plus reads of cluster objects): that the EPP's metrics reach Prometheus or Thanos, that the `ScaledObject` is Ready and its HPA exists, that a burst raises `desiredReplicas` and a drain lowers it, and overshoot while pods start.
- **Flow control** on the `gke` and `amd` lanes: queueing, priority and shedding need load, as in [flow-control](../flow-control/README.md). The objective and fairness headers skip as `requires_load`.
- **The `saturation` signal**: no lane runs it.

`parts/common.yaml` maps each of the guide's claims.

## Upstream notes

- The workflows pass `guide_name: workload-autoscaling`, not the `guide.yaml` name `keda-epp`.
- `guide.yaml` has no `support:` table, so no configuration is marked `validated`; these profiles follow the three lanes. The guide's README says the `inflight` and `queue` signals have nightly coverage.
- The `ibm` lane passes `image_override: docker.io/vllm/vllm-openai:nightly` to `reusable-nightly-e2e-openshift`, while the overlay it renders names `vllm-openai:v0.31.0`. If the override applies to the model server, that lane runs a different vLLM build from the `gke` lane under the same profile; `llmdv compare` across their runs shows any difference.
- `guide.yaml`'s defaults (`EPP_SERVICE: optimized-baseline-epp`, `INFERENCE_POOL: optimized-baseline`) assume the router release `optimized-baseline`; every lane uses its own release name instead, to avoid colliding with other InferencePools on a shared cluster.
