# multimodal-serving

Profiles for llm-d's [multimodal-serving](https://github.com/llm-d/llm-d/tree/main/guides/multimodal-serving) guide, written against llm-d `9ca8ff0`. Status: written, not run. See [../README.md](../README.md) for the layout, provenance tags and how to run a combination.

## Lanes

Every configuration the guide marks `validated` and a nightly lane deploys looks the same to a client: vLLM v0.31.0 serving `Qwen/Qwen3-VL-32B-Instruct` at its own window, with no reasoning or tool parser. One combination covers the three lanes, and `--identity flavor=<topology>` tells the aggregated GPU lane and the E/PD GPU lane apart (they share accelerator and provider). The TPU lane passes `flavor=aggregation` too, so the topology is recorded the same way on every run.

| CI lane (`nightly-e2e-multimodal-serving-<lane>`) | Overlay it renders | Profile | Identity flags |
| --- | --- | --- | --- |
| `aggregation-gke-acc-gpu-vllm-x` | `aggregation/modelserver/gpu/vllm/gke` | `vllm-qwen3-vl-32b-instruct-inline-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=aggregation` |
| `aggregation-gke-acc-tpu-vllm-x` | `aggregation/modelserver/tpu/v7/vllm/qwen3-vl` | `vllm-qwen3-vl-32b-instruct-inline-standalone` | `--identity accelerator=tpu/v7 --identity provider=gke --identity flavor=aggregation` |
| `e-disaggregation-gke-acc-gpu-vllm-x` | `e-disaggregation/modelserver/gpu/vllm/e-pd/gke` | `vllm-qwen3-vl-32b-instruct-inline-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=e-pd` |

The TPU lane's `infra_provider` input is `qwen3-vl`, a model folder rather than a provider; the guide's README names `tpu/v7/vllm/gke` for that accelerator, which wraps `qwen3-vl` and renders the same manifests, and the lane runs on GKE. Its runs are recorded as `provider=gke`.

The client calls the `<GUIDE_NAME>-epp` Service on port 80, in standalone mode.

## What the profile checks that is particular to this guide

- **Vision**: the model reads images, so the vision cases run on Chat, Messages and Responses and require the answer to describe a freshly drawn picture. The guide itself only sends an inline PNG through Chat `image_url`; the other two surfaces are declared because the APIs define image input.
- **No reasoning**: the Instruct model does not think. With no parser and no inline tag declared, the reasoning cases skip as `undeclared`. The reasoning efforts and Messages thinking modes are still declared, as the APIs define them; cases that expect an effort to change the answer are expected to fail until the team triages them.
- **Window**: 262144 tokens, derived from `text_config.max_position_embeddings` in the model's `config.json` on Hugging Face. No overlay sets a window; the first run's `GET /v1/models` confirms it.

## Not covered, and why

- **Multimodal prefix affinity** (the same prompt and image land on one pod, and `vllm:prefix_cache_hits_total` and `vllm:mm_cache_hits_total` grow): needs `routing.prefix_affinity` with per-pod counter deltas (#27) and cluster access.
- **Encode-only routing in E/PD** (encode pods count only media requests): per-pod counters, same reasons.
- **The EC transfer** between encode and decode pods: shown only by a DEBUG log line after a log-level change, which llmdv never makes. A decode pod that receives no encoder output encodes locally, so a client cannot tell.
- **EPP disaggregation and prefix-indexer metrics** (`MONITORING=true`): fleet metrics need cluster access.
- **Token estimation and video**: the router's per-image and per-video token estimate has no client-observable effect, and llmdv has no video input case (nor the `x-llm-d-video-*` headers).
- **Images by URL**: Messages `image_url` needs `image_url_fixture`, which no lane hosts, so it skips as `requires_instrumentation`.
- **The cache suite**: no role passes `--enable-prompt-tokens-details`, so `cache_reuse` is not declared (see `parts/common.yaml`).
- **Configurations no lane runs**: the E/P/D topology (not nightly), the `base` and `coreweave` providers, Intel XPU (community), the TPU v7 `gemma4` and `qwen3-5-397b` overlays, and SGLang's XPU-encode, GPU-PD profile.

## Upstream notes

- `nightly-e2e-multimodal-serving-aggregation-gke-acc-tpu-vllm-x.yaml` passes `infra_provider: qwen3-vl`, which is not one of `guide.yaml`'s `INFRA_PROVIDER` values (`base`, `gke`, `coreweave`); the README's overlay table names `tpu/v7/vllm/gke` for TPU v7. The two render identically today.
- The E/PD decode pods' routing sidecar runs `ghcr.io/llm-d/llm-d-router-disagg-sidecar:main` (`imagePullPolicy: Always`), a floating tag, so two runs of the same llm-d commit can run different sidecar builds. `e-disaggregation/modelserver/gpu/vllm/e-pd/base/kustomization.yaml` takes the `recipes/modelserver/components/images/routing-sidecar/nightly` component (tag `main`) rather than its `release` sibling (tag `v1.0.0`), while it takes the `release` vLLM image.
