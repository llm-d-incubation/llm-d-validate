# multimodal-serving

Profiles for llm-d's [multimodal-serving](https://github.com/llm-d/llm-d/tree/main/guides/multimodal-serving) guide, written against llm-d `9ca8ff0`. Status: written, not run. See [../README.md](../README.md) for how to run a combination, and the [profile skill](../../../.agents/skills/writing-an-infra-profile/SKILL.md#how-profilesllm-d-is-built) for the layout and provenance tags.

## Lanes

Every configuration a nightly lane deploys looks the same to a client: vLLM v0.31.0 serving `Qwen/Qwen3-VL-32B-Instruct` at its own window, with no reasoning or tool parser. One combination covers the three lanes. `accelerator` is the lane's `ACCELERATOR_TYPE`, `provider` the `INFRA_PROVIDER` folder it renders, and `flavor` the guide's topology as the overlay path spells it: the topology folder, then `TOPOLOGY` where it adds a folder of its own (`aggregation`, `e-disaggregation/e-pd`). Every lane passes it; it is what tells the aggregated GPU lane and the E/PD GPU lane apart (they share accelerator and provider).

| CI lane (`nightly-e2e-multimodal-serving-<lane>`) | Overlay it renders | Profile | Identity flags |
| --- | --- | --- | --- |
| `aggregation-gke-acc-gpu-vllm-x` | `aggregation/modelserver/gpu/vllm/gke` | `vllm-qwen3-vl-32b-instruct-inline-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=aggregation` |
| `aggregation-gke-acc-tpu-vllm-x` | `aggregation/modelserver/tpu/v7/vllm/qwen3-vl` | `vllm-qwen3-vl-32b-instruct-inline-standalone` | `--identity accelerator=tpu/v7 --identity provider=qwen3-vl --identity flavor=aggregation` |
| `e-disaggregation-gke-acc-gpu-vllm-x` | `e-disaggregation/modelserver/gpu/vllm/e-pd/gke` | `vllm-qwen3-vl-32b-instruct-inline-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=e-disaggregation/e-pd` |

The TPU lane's `infra_provider` input is `qwen3-vl`, and the folder it renders is `tpu/v7/vllm/qwen3-vl`, so its runs are recorded as `provider=qwen3-vl`: the folder the lane renders, not the one `guide.yaml` lists (see Upstream notes). The E/PD lane's `infra_provider` input is `e-pd/gke`, the workflow's join of `TOPOLOGY` and `INFRA_PROVIDER`; `guide.yaml` keeps them apart, so `e-pd` goes in the flavor and the provider is `gke`.

The client calls the `<GUIDE_NAME>-epp` Service on port 80, in standalone mode.

## What the profile checks that is particular to this guide

- **Vision**: the model reads images, so the vision cases run on Chat, Messages and Responses and require the answer to describe a freshly drawn picture. The guide itself only sends an inline PNG through Chat `image_url`; the other two surfaces are declared because the APIs define image input.
- **No reasoning**: the Instruct model does not think. With no parser and no inline tag declared, the reasoning cases skip as `undeclared`. The reasoning efforts and Messages thinking modes are still declared, as the APIs define them; cases that expect an effort to change the answer are expected to fail until the team triages them.
- **Window**: 262144 tokens, derived from `text_config.max_position_embeddings` in the model's `config.json` on Hugging Face. No overlay sets a window; the first run's `GET /v1/models` confirms it.

## Not covered, and why

- **Multimodal prefix affinity** (the same prompt and image land on one pod, and `vllm:prefix_cache_hits_total` and `vllm:mm_cache_hits_total` grow): needs `routing.prefix_affinity` with per-pod counter deltas (#27) and cluster access.
- **Encode-only routing in E/PD** (encode pods count only media requests): per-pod counters, same reasons.
- **The EC transfer** between encode and decode pods: shown only by a DEBUG log line after a log-level change, which `llmdv` never makes. A decode pod that receives no encoder output encodes locally, so a client cannot tell.
- **EPP disaggregation and prefix-indexer metrics** (`MONITORING=true`): fleet metrics need cluster access.
- **Token estimation and video**: the router's per-image and per-video token estimate has no client-observable effect, and `llmdv` has no video input case (nor the `x-llm-d-video-*` headers).
- **Images by URL**: Messages `image_url` needs `image_url_fixture`, which no lane hosts, so it skips as `requires_instrumentation`.
- **The cache suite**: no role passes `--enable-prompt-tokens-details`, so `cache_reuse` is not declared (see `parts/common.yaml`).
- **Configurations no lane runs**: the E/P/D topology (not nightly), the `base` and `coreweave` providers, Intel XPU (community), the TPU v7 `gemma4` and `qwen3-5-397b` overlays, and SGLang's XPU-encode, GPU-PD profile.

## Upstream notes

- `nightly-e2e-multimodal-serving-aggregation-gke-acc-tpu-vllm-x.yaml` passes `infra_provider: qwen3-vl`, which is not one of `guide.yaml`'s `INFRA_PROVIDER` values (`base`, `gke`, `coreweave`), and `guide.yaml`'s support table lists only `gke` as the provider for `tpu/v7`; the README's overlay table names `tpu/v7/vllm/gke` for TPU v7, which wraps `qwen3-vl`. The two render identically today.
- The E/PD decode pods' routing sidecar runs `ghcr.io/llm-d/llm-d-router-disagg-sidecar:main` (`imagePullPolicy: Always`), a floating tag, so two runs of the same llm-d commit can run different sidecar builds. `e-disaggregation/modelserver/gpu/vllm/e-pd/base/kustomization.yaml` takes the `recipes/modelserver/components/images/routing-sidecar/nightly` component (tag `main`) rather than its `release` sibling (tag `v1.0.0`), while it takes the `release` vLLM image.
- `unhappy.invalid_image` is expected to fail on the E/PD lane, and that is a finding for the team, not an excuse; the selection is unchanged. The router's `always-disagg-multimodal-decider` (`e-disaggregation/router/vllm/e-pd-disaggregation.values.yaml:21-25`) sends media requests through the decode pod's routing sidecar, which turns an encoder's non-2xx answer, such as vLLM's 400 for a corrupt image, into its own 502 (llm-d-router main@a490c31e, the `:main` image above: `connector_ec_common.go:298`, `connector_ec_nixl.go:136-144`). Text requests carry no encoder header and pass through unchanged (`dispatch.go:264-285`).
