# multimodal-serving

Profiles for testing a deployment of llm-d's [multimodal-serving](https://github.com/llm-d/llm-d/tree/main/guides/multimodal-serving) guide, in which vLLM serves a vision model, `Qwen/Qwen3-VL-32B-Instruct`, either aggregated or with image encoding on its own encode pods (E/PD).

[Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What it tests](#what-it-tests) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## Pick your profile

There is one profile: every topology and overlay below looks the same to a client, vLLM v0.31.0 serving `Qwen/Qwen3-VL-32B-Instruct` at its own window.

| Profile | Model a client sees | Overlays |
| --- | --- | --- |
| `vllm-qwen3-vl-32b-instruct-inline-standalone` | Qwen3-VL-32B-Instruct on vLLM, reads images, no reasoning or tool parser | `aggregation/modelserver/gpu/vllm/gke`, `aggregation/modelserver/tpu/v7/vllm/qwen3-vl`, `e-disaggregation/modelserver/gpu/vllm/e-pd/gke` |

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/multimodal-serving/vllm-qwen3-vl-32b-instruct-inline-standalone.yaml
```

To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional: they label a run so `llmdv compare` never compares two configurations, for example `--identity accelerator=gpu --identity provider=gke --identity flavor=e-disaggregation/e-pd`, where `flavor` is the topology as your overlay path spells it.

## What it tests

Vision on top of the base tests every guide gets: the vision cases run on Chat, Messages and Responses and require the answer to describe a freshly drawn picture (the guide itself only sends an inline PNG through Chat `image_url`; the other two surfaces are declared because the APIs define image input). The Instruct model does not think, so the reasoning cases skip as `undeclared`; the reasoning efforts and Messages thinking modes are still declared, as the APIs define them, and cases that expect an effort to change the answer are expected to fail until the team triages them. The window is 262144 tokens, from `text_config.max_position_embeddings` in the model's `config.json` on Hugging Face (no overlay sets one; the first run's `GET /v1/models` confirms it). On the E/PD topology `unhappy.invalid_image` is expected to fail, and that is a finding for the team, not an excuse: the decode pod's routing sidecar turns the encoder's 400 for a corrupt image into its own 502 (details under [For maintainers](#for-maintainers)).

## What `llmdv verify` selects

The default is defined once in [`parts/common.yaml`](parts/common.yaml) and used unchanged by every profile here.
See [selection rules](../../../docs/cli.md#test-sets-and-coverage) for smoke, full-library coverage and filters.

<details>
<summary>The default's case-ID patterns</summary>

`*` selects the whole library; `suite.*` selects a suite. Other patterns select matching cases.

| Suite | Patterns |
| --- | --- |
| `contract` | `contract.*` |
| `discovery` | `discovery.models_lists_declared_model`, `discovery.unknown_model_no_fallback`, `discovery.models_parses_with_openai_sdk` |
| `openai_chat` | `openai_chat.basic_non_streaming`, `openai_chat.streaming_sse`, `openai_chat.stop_reason`, `openai_chat.configured_reasoning` |
| `openai_completions` | `openai_completions.basic_non_streaming`, `openai_completions.streaming_matches_json`, `openai_completions.stop_reason` |
| `anthropic_messages` | `anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse` |
| `openai_responses` | `openai_responses.basic_non_streaming`, `openai_responses.streaming_sse` |
| `streaming` | `streaming.sse_framing`, `streaming.*` |
| `unhappy` | `unhappy.unknown_path`, `unhappy.malformed_json`, `unhappy.missing_model`, `unhappy.wrong_method`, `unhappy.error_envelope_fields`, `unhappy.context_overflow`, `unhappy.token_limit_over_context`, `unhappy.image_input`, `unhappy.invalid_image` |
| `transport` | `transport.connection`, `transport.health_status` |
| `tools` | `tools.auto_call`, `tools.auto_stream` |
| `long_context` | `long_context.*` |
| `images` | `images.*` |

</details>

<details>
<summary>How many cases each scope selects, per profile</summary>

Counted offline by `llmdv plan`, one per ingress and variant. *Run* cases can still skip during a run (for example `throttled`); *skip* cases are ruled out by the profile's declarations, with their skip class.

| Profile | Default (= `--all-test-sets`) | `smoke` | `--full-suite` |
| --- | --- | --- | --- |
| `vllm-qwen3-vl-32b-instruct-inline-standalone.yaml` | 132 selected, 118 run, 14 skip: 14 undeclared | 9 selected, 9 run | 1092 selected, 659 run, 433 skip: 433 undeclared |

</details>

Preview individual cases and skip reasons with `llmdv plan --profile <profile>`.

## Not covered

- **Multimodal prefix affinity** (the same prompt and image land on one pod, and `vllm:prefix_cache_hits_total` and `vllm:mm_cache_hits_total` grow): needs `routing.prefix_affinity` with per-pod counter deltas (#27) and cluster access.
- **Encode-only routing in E/PD** (encode pods count only media requests): per-pod counters, same reasons.
- **The EC transfer** between encode and decode pods: shown only by a DEBUG log line after a log-level change, which `llmdv` never makes. A decode pod that receives no encoder output encodes locally, so a client cannot tell.
- **EPP disaggregation and prefix-indexer metrics** (`MONITORING=true`): fleet metrics need cluster access.
- **Token estimation and video**: the router's per-image and per-video token estimate has no client-observable effect, and `llmdv` has no video input case (nor the `x-llm-d-video-*` headers).
- **Images by URL**: Messages `image_url` needs `image_url_fixture`, which no llm-d test deployment hosts, so it skips as `requires_instrumentation`.
- **The cache suite**: no role passes `--enable-prompt-tokens-details`, so `cache_reuse` is not declared (see `parts/common.yaml`).
- **Configurations llm-d does not test nightly**: the E/P/D topology, the `base` and `coreweave` providers, Intel XPU (community), the TPU v7 `gemma4` and `qwen3-5-397b` overlays, and SGLang's XPU-encode, GPU-PD profile.

## For maintainers

<details>
<summary>llm-d's nightly lanes, and how each maps to a profile</summary>

Every configuration a nightly lane deploys looks the same to a client: vLLM v0.31.0 serving `Qwen/Qwen3-VL-32B-Instruct` at its own window, with no reasoning or tool parser. One combination covers the three lanes. `accelerator` is the lane's `ACCELERATOR_TYPE`, `provider` the `INFRA_PROVIDER` folder it renders, and `flavor` the guide's topology as the overlay path spells it: the topology folder, then `TOPOLOGY` where it adds a folder of its own (`aggregation`, `e-disaggregation/e-pd`). Every lane passes it; it is what tells the aggregated GPU lane and the E/PD GPU lane apart (they share accelerator and provider).

| CI lane (`nightly-e2e-multimodal-serving-<lane>`) | Overlay it renders | Profile | Identity flags |
| --- | --- | --- | --- |
| `aggregation-gke-acc-gpu-vllm-x` | `aggregation/modelserver/gpu/vllm/gke` | `vllm-qwen3-vl-32b-instruct-inline-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=aggregation` |
| `aggregation-gke-acc-tpu-vllm-x` | `aggregation/modelserver/tpu/v7/vllm/qwen3-vl` | `vllm-qwen3-vl-32b-instruct-inline-standalone` | `--identity accelerator=tpu/v7 --identity provider=qwen3-vl --identity flavor=aggregation` |
| `e-disaggregation-gke-acc-gpu-vllm-x` | `e-disaggregation/modelserver/gpu/vllm/e-pd/gke` | `vllm-qwen3-vl-32b-instruct-inline-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=e-disaggregation/e-pd` |

The TPU lane's `infra_provider` input is `qwen3-vl`, and the folder it renders is `tpu/v7/vllm/qwen3-vl`, so its runs are recorded as `provider=qwen3-vl`: the folder the lane renders, not the one `guide.yaml` lists (see "Where llm-d's own sources disagree" below). The E/PD lane's `infra_provider` input is `e-pd/gke`, the workflow's join of `TOPOLOGY` and `INFRA_PROVIDER`; `guide.yaml` keeps them apart, so `e-pd` goes in the flavor and the provider is `gke`.

The client calls the `<GUIDE_NAME>-epp` Service on port 80, in standalone mode.

</details>

<details>
<summary>Where llm-d's own sources disagree</summary>

- `nightly-e2e-multimodal-serving-aggregation-gke-acc-tpu-vllm-x.yaml` passes `infra_provider: qwen3-vl`, which is not one of `guide.yaml`'s `INFRA_PROVIDER` values (`base`, `gke`, `coreweave`), and `guide.yaml`'s support table lists only `gke` as the provider for `tpu/v7`; the README's overlay table names `tpu/v7/vllm/gke` for TPU v7, which wraps `qwen3-vl`. The two render identically today.
- The E/PD decode pods' routing sidecar runs `ghcr.io/llm-d/llm-d-router-disagg-sidecar:main` (`imagePullPolicy: Always`), a floating tag, so two runs of the same llm-d commit can run different sidecar builds. `e-disaggregation/modelserver/gpu/vllm/e-pd/base/kustomization.yaml` takes the `recipes/modelserver/components/images/routing-sidecar/nightly` component (tag `main`) rather than its `release` sibling (tag `v1.0.0`), while it takes the `release` vLLM image.
- `unhappy.invalid_image` is expected to fail on the E/PD lane, and that is a finding for the team, not an excuse; the default test set is unchanged. The router's `always-disagg-multimodal-decider` (`e-disaggregation/router/vllm/e-pd-disaggregation.values.yaml:21-25`) sends media requests through the decode pod's routing sidecar, which turns an encoder's non-2xx answer, such as vLLM's 400 for a corrupt image, into its own 502 (llm-d-router main, the `:main` image above: `connector_ec_common.go:298`, `connector_ec_nixl.go:136-144`). Text requests carry no encoder header and pass through unchanged (`dispatch.go:264-285`).

</details>
