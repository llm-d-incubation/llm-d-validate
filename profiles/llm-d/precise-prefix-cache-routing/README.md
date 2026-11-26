# precise-prefix-cache-routing

Profiles for testing a deployment of llm-d's [precise-prefix-cache-routing](https://github.com/llm-d/llm-d/tree/main/guides/precise-prefix-cache-routing) guide, in which the router tokenizes every prompt by calling vLLM's render routes on the pool's own pods, looks the token blocks up in an index fed by each engine's KV-cache events, and routes to the pod that holds the prefix.

[What it tests](#what-it-tests) · [Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## What it tests

**Validation focus: rendering and token contracts used by precise routing.** Check declared render routes, token accounting and prompt pins because the router’s cache lookup depends on tokenized input.

**What a pass establishes:** the checks that actually executed held at the reachable ingress
during this run. Skipped checks remain coverage gaps. **Coverage boundary:** Router render success, KV-event indexing and per-pod affinity require additional instrumentation.

These focused defaults are a starting point for validating a production deployment. Add coverage
for the APIs, clients and operating conditions you rely on; see
[Growing coverage toward production](../../../docs/getting-started.md#widen-coverage).

<details>
<summary>Why these checks relate to the guide</summary>

The router's rendering, on top of the base tests every guide gets (each API, error handling, the parsers and the context window). Because the router tokenizes every prompt through the engine, the profiles check that Messages and Responses token counts agree with the engine's usage, that a fixed conversation keeps its size in input tokens (`evolution.prompt_pin`), and, on every overlay except TPU v5p, vLLM's four render routes. Every overlay passes `--block-size=64` and `--kv-events-config` and loads no KV connector. A green run does not show that precise routing is on, because a router whose renders all fail still answers 200: see [Not covered](#not-covered).

The declaration rationale and claim-to-case mapping live in
[`parts/common.yaml`](parts/common.yaml). [Not covered](#not-covered) explains the remaining
access, instrumentation and load requirements.

</details>

## Pick your profile

Pick the profile for the overlay you deployed (under `guides/precise-prefix-cache-routing/modelserver/`). The NVIDIA providers (`base`, `gke`, `ocp`) don't change what a client sees, so they share a profile.

| Model a client sees | Overlays | Profile |
| --- | --- | --- |
| Qwen3-32B on vLLM, with reasoning and tool-call parsers | `gpu/vllm/base`, `gpu/vllm/gke`, `gpu/vllm/ocp` | [vllm-qwen3-32b-parsers-standalone](vllm-qwen3-32b-parsers-standalone.yaml) |
| Qwen3-32B on vLLM, reasoning inline, no tool parser | `amd/vllm` | [vllm-qwen3-32b-inline-standalone](vllm-qwen3-32b-inline-standalone.yaml) |
| Qwen3-32B on vLLM, 4k window, reasoning inline, no tool parser | `tpu/v5p/vllm` | [vllm-qwen3-32b-ctx4k-inline-standalone](vllm-qwen3-32b-ctx4k-inline-standalone.yaml) |
| Qwen3-0.6B on vLLM, reasoning inline, no tool parser | `xpu/vllm` | [vllm-qwen3-0.6b-inline-standalone](vllm-qwen3-0.6b-inline-standalone.yaml) |

On `xpu/vllm`, the guide asks you to set the router's `token-producer` `modelName` to the served model by hand; left at `Qwen/Qwen3-32B`, every render the router makes is refused. On `tpu/v5p/vllm`, the engine runs without `--enable-scale-out`, so its render routes answer 404 and the router routes without cache information. In both cases requests can still answer 200.

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/precise-prefix-cache-routing/<profile>.yaml
```

To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional for verification. Set them when comparing deployments that share a profile, so their recorded configuration dimensions remain distinct; for example `--identity accelerator=gpu --identity provider=gke`, read off your overlay path.


## What `llmdv verify` selects

The default is defined once in [`parts/common.yaml`](parts/common.yaml) and used unchanged by every profile here.
See [selection rules](../../../docs/cli.md#test-sets-and-coverage) for smoke, full-library coverage and filters.

<details>
<summary>The default's case-ID patterns</summary>

`*` selects the whole library; `suite.*` selects a suite. Other patterns select matching cases.

**contract**

- `contract.*`

**discovery**

- `discovery.models_lists_declared_model`
- `discovery.unknown_model_no_fallback`
- `discovery.models_parses_with_openai_sdk`

**openai_chat**

- `openai_chat.basic_non_streaming`
- `openai_chat.streaming_sse`
- `openai_chat.stop_reason`
- `openai_chat.configured_reasoning`

**openai_completions**

- `openai_completions.basic_non_streaming`
- `openai_completions.streaming_matches_json`
- `openai_completions.stop_reason`

**anthropic_messages**

- `anthropic_messages.basic_non_streaming`
- `anthropic_messages.streaming_sse`
- `anthropic_messages.count_tokens`
- `anthropic_messages.token_count_matches_usage`
- `anthropic_messages.stream_matches_message`

**openai_responses**

- `openai_responses.basic_non_streaming`
- `openai_responses.streaming_sse`
- `openai_responses.input_tokens`

**streaming**

- `streaming.sse_framing`

**unhappy**

- `unhappy.unknown_path`
- `unhappy.malformed_json`
- `unhappy.missing_model`
- `unhappy.wrong_method`
- `unhappy.error_envelope_fields`
- `unhappy.context_overflow`
- `unhappy.token_limit_over_context`

**transport**

- `transport.connection`
- `transport.health_status`

**tools**

- `tools.auto_call`
- `tools.auto_stream`

**long_context**

- `long_context.*`

**render**

- `render.*`

**evolution**

- `evolution.prompt_pin`
- `evolution.prompt_pin_tools`


</details>

Counted offline by `llmdv plan`, one case per ingress and variant. Planned runs can still skip at runtime. Expand a profile for its scope counts and separate skip classes.

The default is also `--all-test-sets` for these profiles.

<details>
<summary>vllm-qwen3-0.6b-inline-standalone.yaml</summary>

[Open the profile](vllm-qwen3-0.6b-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 107 | 91 | 16 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 692 | 400 |

Planned skips by class:

- **Default:** 16 `undeclared`.
- **Full library:** 400 `undeclared`.

</details>

<details>
<summary>vllm-qwen3-32b-ctx4k-inline-standalone.yaml</summary>

[Open the profile](vllm-qwen3-32b-ctx4k-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 107 | 81 | 26 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 678 | 414 |

Planned skips by class:

- **Default:** 26 `undeclared`.
- **Full library:** 414 `undeclared`.

</details>

<details>
<summary>vllm-qwen3-32b-inline-standalone.yaml</summary>

[Open the profile](vllm-qwen3-32b-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 107 | 91 | 16 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 692 | 400 |

Planned skips by class:

- **Default:** 16 `undeclared`.
- **Full library:** 400 `undeclared`.

</details>

<details>
<summary>vllm-qwen3-32b-parsers-standalone.yaml</summary>

[Open the profile](vllm-qwen3-32b-parsers-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 107 | 100 | 7 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 938 | 154 |

Planned skips by class:

- **Default:** 7 `undeclared`.
- **Full library:** 154 `undeclared`.

</details>

Preview cases and reasons with `llmdv plan --profile <profile>`.

## Not covered

- **Configurations llm-d does not test nightly:** SGLang on NVIDIA GPU and Intel XPU (with their render pools), TPU v6e and v7, and CPU, all `community` in `guide.yaml`; gateway mode.
- **Render surfaces on TPU v5p.** The GPU, AMD and XPU combinations declare vLLM's four render surfaces (`chat_render`, `completions_render`, `messages_render`, `responses_render`) on the `router` ingress, with `prefix_stability` on `chat_render` and `messages_render`; their engines pass `--enable-scale-out`, which vLLM v0.30 and later require to register the routes (`vllm@v0.31.0:vllm/entrypoints/scale_out/factories.py`, `register_scale_out_api_routers`). The TPU v5p combination declares none, since its engine runs without the flag (see [Where llm-d's own sources disagree](#for-maintainers)). No combination declares `render_pins` yet. An ordinary run skips undeclared pins; [declare placeholders and run the seeding workflow](../../../docs/profile-provenance.md#seed-change-sentinels) to obtain them.
- **The guide's per-pod render check, router render metrics, per-pod prefix-cache counters and KV-event indexing**: they need per-pod URLs or cluster access a committed profile does not carry. Without the router render metrics, a router whose renders all fail still answers 200, so a green run does not show that precise routing is on.
- **Prefix affinity** (`routing.prefix_affinity`, #27) and `cache_reuse`: no overlay passes `--enable-prompt-tokens-details`, so no response carries `cached_tokens`.

## For maintainers

<details>
<summary>llm-d's nightly lanes, and how each maps to a profile</summary>

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

</details>

<details>
<summary>How these profiles were written</summary>

Written from the shipped configuration (each lane's overlay rendered with `kubectl kustomize`) and the API definitions. The layout and provenance tags are in the [profile provenance guide](../../../docs/profile-provenance.md#how-profilesllm-d-is-built), and the shared router claims in [../optimized-baseline/parts/common.yaml](../optimized-baseline/parts/common.yaml); this guide's claims and where each lands are in [parts/common.yaml](parts/common.yaml).

</details>

<details>
<summary>Where llm-d's own sources disagree</summary>

- **TPU v5p and `--enable-scale-out`.** `modelserver/tpu/v5p/vllm/base/patch-vllm.yaml` is the only lane overlay without `--enable-scale-out`, and it runs `vllm-tpu:v0.31.0` (from `recipes/modelserver/components/images/tpu-vllm/release`). vLLM v0.30 and later register `/v1/*/render` only with that flag (`vllm@v0.31.0:vllm/entrypoints/scale_out/factories.py`), so on this lane every render call the router makes answers 404 and it routes without cache information, while requests still answer 200. The README ("Render (Tokenizer) Endpoints", the support table) and `guide.yaml` say the TPU overlays use vLLM 0.29, which serves the routes without the flag, and the router values' comment on `messagesRenderMode` repeats it; the only vLLM 0.29 image in llm-d is `vllm-torchtpu:v0.29.0` (`recipes/modelserver/components/images/torch-tpu-vllm/release`), which this guide's `tpu/v6` and `tpu/v7` overlays use and no lane runs.
- **Intel XPU model name.** The router's `token-producer` `modelName` is `Qwen/Qwen3-32B` (`router/precise-prefix-cache-routing.values.yaml`), while the XPU overlay serves `Qwen/Qwen3-0.6B`. The README ("Model" note under the support table) asks the operator to edit it by hand; nothing in llm-d changes it for the lane. Unedited, every render call names a model the pods do not serve and is refused. The README ("Verify that the router renders") says requests still answer 200 when the router's renders fail, routed without cache information; if so, a client sees nothing wrong and `llmdv`'s cases pass while precise routing is off. If the router instead fails the request, every case through the EPP fails on this lane. The guide's own `router_render` check tells the two apart; `llmdv` cannot without cluster access.
- **`ocp` provider.** The `ibm-acc-gpu-vllm-x` lane deploys `gpu/vllm/ocp`, but `ocp` is not among `guide.yaml`'s `INFRA_PROVIDER` values (`base`, `gke`) or the `gpu` providers.
- **Providers of the no-provider lanes.** `guide.yaml` lists `gke` as the only provider for `tpu/v5p` and `base` for `amd` and `xpu`; those lanes pass an empty provider and apply `base`.
- **vLLM version in the docs.** The README's render table says vLLM 0.30 for NVIDIA, AMD and Intel; the GPU and AMD overlays render `v0.31.0`.

</details>
