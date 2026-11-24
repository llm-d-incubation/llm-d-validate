# precise-prefix-cache-routing

Profiles for llm-d's [precise-prefix-cache-routing](https://github.com/llm-d/llm-d/tree/main/guides/precise-prefix-cache-routing) guide at llm-d `9ca8ff0`. The router tokenizes every prompt by calling vLLM's render routes on the pool's own pods, looks the token blocks up in an index fed by each engine's KV-cache events, and routes to the pod that holds the prefix. Written from the shipped configuration (each lane's overlay rendered with `kubectl kustomize`) and the API definitions; none has been run yet. Layout, provenance tags and the shared router claims are in [../README.md](../README.md) and [../optimized-baseline/parts/common.yaml](../optimized-baseline/parts/common.yaml); this guide's claims and where each lands are in [parts/common.yaml](parts/common.yaml).

## Lanes

Six nightly lanes (`nightly-e2e-precise-prefix-cache-routing-<lane>.yaml`, each `gateway_class: epponly`, so standalone) deploy four configurations that differ to a client. Every overlay passes `--block-size=64` and `--kv-events-config` and loads no KV connector.

| CI lane | Overlay (`ACCELERATOR_TYPE/MODEL_SERVER/INFRA_PROVIDER`) | Profile | Identity flags |
| --- | --- | --- | --- |
| `cks-acc-gpu-vllm-x` | `gpu/vllm/base` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=base` |
| `gke-acc-gpu-vllm-x` | `gpu/vllm/gke` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `ibm-acc-gpu-vllm-x` | `gpu/vllm/ocp` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=ocp` |
| `amd-ci-acc-rocm-vllm-x` | `amd/vllm` (no provider; includes `base`) | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=base` |
| `gke-acc-tpu-vllm-x` | `tpu/v5p/vllm` (no provider; includes `base`) | `vllm-qwen3-32b-ctx4k-inline-standalone` | `--identity accelerator=tpu/v5p --identity provider=base` |
| `intel-acc-xpu-vllm-x` | `xpu/vllm` (no provider; includes `base`) | `vllm-qwen3-0.6b-inline-standalone` | `--identity accelerator=xpu --identity provider=base` |

A lane that sets no provider applies the accelerator and engine folder's own kustomization, which includes only `base`, so it passes `provider=base`. No two lanes share both a profile and an accelerator and provider, so none passes `flavor`.

```sh
export LLMDV_EPP_URL=http://<GUIDE_NAME>-epp.<namespace>.svc.cluster.local   # GUIDE_NAME=precise-prefix-cache-routing
llmdv verify --profile profiles/llm-d/precise-prefix-cache-routing/<profile>.yaml <identity flags>
```

## Not covered

- **Configurations no lane runs:** SGLang on NVIDIA GPU and Intel XPU (with their render pools), TPU v6e and v7, and CPU, all `community` in `guide.yaml`; gateway mode.
- **Render surfaces** are not declared. Existence is measured by the first run: these engines are expected to serve vLLM's render routes (the router depends on them), and the EPP forwards every path, so `contract.undeclared_surfaces_absent` will name each one that answers. Declare them then, with their `render_pins`.
- **The guide's per-pod render check, router render metrics, per-pod prefix-cache counters and KV-event indexing**: they need per-pod URLs or cluster access a committed profile does not carry. Without the router render metrics, a router whose renders all fail still answers 200, so a green run does not show that precise routing is on.
- **Prefix affinity** (`routing.prefix_affinity`, #27) and `cache_reuse`: no overlay passes `--enable-prompt-tokens-details`, so no response carries `cached_tokens`.

## Upstream notes

- **TPU v5p and `--enable-scale-out`.** `modelserver/tpu/v5p/vllm/base/patch-vllm.yaml` is the only lane overlay without `--enable-scale-out`. The README ("Render (Tokenizer) Endpoints") says the TPU overlays use vLLM 0.29, which serves the render routes without the flag, and `guide.yaml` notes `vLLM 0.29` for `tpu/v5p`. The rendered overlay runs `vllm-tpu:v0.31.0` (from `recipes/modelserver/components/images/tpu-vllm/release`). If v0.31.0 behaves like 0.30 and needs the flag, every render call answers 404: requests still answer 200, but routed without cache information. The router values' comment on `messagesRenderMode` repeats the 0.29 claim.
- **Intel XPU model name.** The router's `token-producer` `modelName` is `Qwen/Qwen3-32B` (`router/precise-prefix-cache-routing.values.yaml`), while the XPU overlay serves `Qwen/Qwen3-0.6B`. The README ("Model" note under the support table) asks the operator to edit it by hand; nothing in llm-d `9ca8ff0` changes it for the lane. Unedited, every render call names a model the pods do not serve and is refused. The README ("Verify that the router renders") says requests still answer 200 when the router's renders fail, routed without cache information; if so, a client sees nothing wrong and llmdv's cases pass while precise routing is off. If the router instead fails the request, every case through the EPP fails on this lane. The guide's own `router_render` check tells the two apart; llmdv cannot without cluster access.
- **`ocp` provider.** The `ibm-acc-gpu-vllm-x` lane deploys `gpu/vllm/ocp`, but `ocp` is not among `guide.yaml`'s `INFRA_PROVIDER` values (`base`, `gke`) or the `gpu` providers.
- **Providers of the no-provider lanes.** `guide.yaml` lists `gke` as the only provider for `tpu/v5p` and `base` for `amd` and `xpu`; those lanes pass an empty provider and apply `base`.
- **vLLM version in the docs.** The README's render table says vLLM 0.30 for NVIDIA, AMD and Intel; the GPU and AMD overlays render `v0.31.0`.
