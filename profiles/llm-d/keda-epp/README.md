# keda-epp

Profiles for llm-d's [keda-epp guide](https://github.com/llm-d/llm-d/tree/main/guides/workload-autoscaling/keda-epp) (`guides/workload-autoscaling/keda-epp`): optimized-baseline's model server, autoscaled by a KEDA `ScaledObject` on an EPP metric (flow-control queue size and running requests, or in-flight requests).

The profiles test the router (EPP) each lane exposes, in standalone mode, through `../parts/topology/standalone.yaml`. Each lane installs the router under its own helm release, so the Service differs per lane (each lane's `gateway_host`).

## Lanes

| CI lane (`nightly-e2e-workload-autoscaling-keda-epp-<lane>`) | Reusable workflow, deploy script (`guides/workload-autoscaling/scripts/`) | Overlay it renders | Router | Service | Profile | Identity flags |
| --- | --- | --- | --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `reusable-nightly-e2e-gke`, `nightly-deploy-gke-keda-epp.sh` | `optimized-baseline/modelserver/gpu/vllm/gke` | flow control on, queue `ScaledObject` | `<release>-gke-epp` | `vllm-qwen3-32b-parsers-queue-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=queue` |
| `ibm-acc-gpu-vllm-x` | `reusable-nightly-e2e-openshift`, `nightly-deploy-ocp-keda-epp.sh` | `optimized-baseline/modelserver/gpu/vllm/base` | optimized-baseline's, no flow control, inflight `ScaledObject` | `<release>-inflight-epp` | `vllm-qwen3-32b-parsers-inflight-standalone` | `--identity accelerator=gpu --identity provider=base --identity flavor=inflight` |
| `amd-acc-rocm-vllm-x` | `reusable-nightly-e2e-amd`, `nightly-deploy-amd-keda-epp.sh` | `optimized-baseline/modelserver/amd/vllm/base` | flow control on, queue `ScaledObject` | `<release>-rocm-epp` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=base --identity flavor=queue` |

Each lane's deploy script names its own router release; `<release>` stands for the prefix it sets, and the Service is in the lane's `gateway_host`. The provider value is the overlay's `INFRA_PROVIDER` folder, and the flavor is the guide's `SIGNAL` value, the `ScaledObject` overlay the lane applies. The `gke` and `ibm` lanes serve the same model configuration but do not share a profile: a router with flow control adds `x-llm-d-flow-queue-duration-ms` to every request it admits, even one at a time, and one without sends no such header. The `queue` combinations declare it `promised`, so `contract.header_disposition` checks that flow control is on.

```sh
export LLMDV_EPP_URL=<the EPP's URL>
llmdv verify --profile profiles/llm-d/keda-epp/vllm-qwen3-32b-parsers-queue-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke --identity flavor=queue
```

**Replicas change during a lane.** Each lane starts the model server at one replica, and its validator (`.github/scripts/e2e/e2e-validate-scale-event.sh`) drives the HPA to two and back. A run that overlaps a scale event may see a second replica join the pool, or stay Pending. No declaration here depends on the replica count.

## Not covered

- **Every scaling claim** (`requires_load`, #23, plus reads of cluster objects): that the EPP's metrics reach Prometheus or Thanos, that the `ScaledObject` is Ready and its HPA exists, that a burst raises `desiredReplicas` and a drain lowers it, and overshoot while pods start.
- **Flow control** on the `gke` and `amd` lanes, beyond its response header: queueing, priority and shedding need load, as in [flow-control](../flow-control/README.md). The objective and fairness headers skip as `requires_load`.
- **The `saturation` signal**: no lane runs it.

`parts/common.yaml` maps each of the guide's claims.

## Upstream notes

- The workflows pass `guide_name: workload-autoscaling`, not the `guide.yaml` name `keda-epp`.
- `guide.yaml` has no `support:` table, so no configuration is marked `validated`; these profiles follow the three lanes. The guide's README says the `inflight` and `queue` signals have nightly coverage.
- The `ibm` lane passes `image_override: docker.io/vllm/vllm-openai:nightly` to `reusable-nightly-e2e-openshift`, while the overlay it renders names `vllm-openai:v0.31.0`. If the override applies to the model server, that lane runs a different vLLM build from the `gke` lane, which serves the same model configuration; `llmdv compare` across their runs shows any difference.
- `guide.yaml`'s `EPP_SERVICE` and `INFERENCE_POOL` defaults assume optimized-baseline's router release; every lane uses its own release name instead, to avoid colliding with other InferencePools on a shared cluster.
