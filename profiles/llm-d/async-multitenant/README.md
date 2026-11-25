# async-multitenant

Profiles for llm-d's [multitenant async-processing guide](https://github.com/llm-d/llm-d/tree/main/guides/batch-serving/asynchronous-processing/multitenant) (`guide.yaml` name `async-multitenant`) at llm-d `9ca8ff0`: llm-d-async dispatching queued work from Redis through an llm-d router with flow control and six priority bands, beside realtime traffic, with the router coordinator as an HTTP front door.

**These profiles cover only the router.** They test the guide's llm-d router (EPP), in standalone mode, through `../parts/topology/standalone.yaml`. The Service is the router release's `<release>-epp` on port 80, where `<release>` is the guide's `POOL_NAME` (the lanes' `gateway_host`).

## Lanes

Both lanes run `guides/batch-serving/asynchronous-processing/multitenant/scripts/nightly-deploy-gke.sh` through `reusable-nightly-e2e-gke`, render the guide's `modelserver/gpu/vllm/gke` (optimized-baseline's `gpu/vllm/gke` relabelled, one replica), and differ only in the router values file. A client sending one request at a time cannot tell the two apart, so they share one profile and pass `--identity flavor`. Both run flow control, which a client sees as the `x-llm-d-flow-queue-duration-ms` response header on every admitted request; `parts/common.yaml` declares it `promised`.

| CI lane (`nightly-e2e-<lane>`) | Router values | Profile | Identity flags |
| --- | --- | --- | --- |
| `async-multitenant-gke-acc-gpu-vllm-x` | `values/router/flow-control-evictable.yaml` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=evictable` |
| `async-multitenant-holdback-gke-acc-gpu-vllm-x` | `values/router/flow-control-holdback.yaml` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=holdback` |

```sh
export LLMDV_EPP_URL=<the EPP's URL>
llmdv verify --profile profiles/llm-d/async-multitenant/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke --identity flavor=evictable
```

## Not covered

- **Eviction, holdback, priority and fairness under saturation** (`requires_load`, #23). The lanes' validator runs an inference-perf experiment with realtime and async traffic at 20/80/90/100 % of the router's capacity; `llmdv` has no load mode. The objective and fairness headers are declared `consumed` in the topology part and skip as `requires_load`.
- **The coordinator** (`llm-d-coordinator`, port 8080): `x-llm-d-async-mode` (`passthrough`, `wait`, `enqueue`) and `x-llm-d-tenant`. Covering it needs a coordinator ingress in front of the router (gateway position, `fronts: [epp]`, #24), measured surfaces and error statuses, and, for `enqueue`, a submit-and-poll surface `llmdv` does not have.
- **llm-d-async and Redis**: queues, quota gates, tier priority and objective stamping are not HTTP surfaces.

`parts/common.yaml` maps each of the guide's claims.

## Upstream notes

- `guide.yaml` has no `support:` table, so no configuration is marked `validated`; these profiles follow the two lanes.
- The holdback lane passes `guide_name: async-multitenant-holdback` to the reusable workflow, a name no `guide.yaml` declares; the router's own release is the same as the other lane's.
- `guide.yaml`'s `GPUS=1` option (`modelserver/gpu/vllm/single-gpu`, Qwen/Qwen3-8B) and the Pub/Sub queue backend are `skip_in: [ci]`; no lane runs them, so no profile covers them.
