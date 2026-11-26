# wide-ep

Profiles for testing a deployment of llm-d's [wide-ep](https://github.com/llm-d/llm-d/tree/main/guides/wide-ep) guide, in which models are served with wide expert parallelism and prefill/decode (P/D) disaggregation.

[What it tests](#what-it-tests) · [Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## What it tests

**Validation focus: api integrity through the expert-parallel serving path.** Check choices, streamed usage, errors and cancellation because the P/D sidecar can rewrite these exchanges. On XPU the same checks exercise the aggregated path.

**What a pass establishes:** the checks that actually executed held at the reachable ingress
during this run. Skipped checks remain coverage gaps. **Coverage boundary:** Expert placement and disaggregation internals need evidence beyond API responses.

These focused defaults are a starting point for validating a production deployment. Add coverage
for the APIs, clients and operating conditions you rely on; see
[Growing coverage toward production](../../../docs/getting-started.md#widen-coverage).

<details>
<summary>Why these checks relate to the guide</summary>

What the P/D routing sidecar changes, on top of the base tests every guide gets (each API, error handling, the parsers and the context window). On gpu and amd every request goes through the decode pod's sidecar, which runs the prefill leg remotely with some of the client's fields removed, rewrites streamed usage frames and writes some 400s of its own, so the profiles check that `n` still returns n choices, that streamed usage still parses, that its errors keep the API's error shape, and that a burst of cancelled streams leaves the path serving. The router differs by accelerator (always-disaggregate on gpu and amd, one aggregated profile on xpu; see [`parts/common.yaml`](parts/common.yaml)), but not in anything a client sees: the listener, APIs and header handling are the ones `../parts/topology/standalone.yaml` declares. On xpu, which has no sidecar, these cases run as plain conformance.

The declaration rationale and claim-to-case mapping live in
[`parts/common.yaml`](parts/common.yaml). [Not covered](#not-covered) explains the remaining
access, instrumentation and load requirements.

</details>

## Pick your profile

Pick the profile for the overlay you deployed (under `guides/wide-ep/modelserver/`). The three NVIDIA provider overlays render identical vLLM arguments, so they share a profile.

| Model a client sees | Overlays | Profile |
| --- | --- | --- |
| DeepSeek-R1-0528 on vLLM, with a tool-call parser, reasoning inline | `gpu/vllm/gke`, `gpu/vllm/coreweave`, `gpu/vllm/ocp` | [vllm-deepseek-r1-0528-tools-standalone](vllm-deepseek-r1-0528-tools-standalone.yaml) |
| DeepSeek-V3 on vLLM, 36k window, no parsers | `amd/vllm/amd-ci` | [vllm-deepseek-v3-ctx36k-inline-standalone](vllm-deepseek-v3-ctx36k-inline-standalone.yaml) |
| DeepSeek-V2-Lite-Chat on vLLM, 2k window, no parsers, aggregated (no P/D) | `xpu/vllm/xpu-ci` | [vllm-deepseek-v2-lite-chat-ctx2k-inline-standalone](vllm-deepseek-v2-lite-chat-ctx2k-inline-standalone.yaml) |

<details>
<summary>What each overlay serves</summary>

Read from the overlays rendered with `kubectl kustomize`:

- **gpu** (`vllm-openai` v0.31.0): `deepseek-ai/DeepSeek-R1-0528`, no `--max-model-len` (163840, derived from the model's config, until a run's `/v1/models` confirms it), `--tool-call-parser deepseek_v3 --enable-auto-tool-choice` with vLLM's `tool_chat_template_deepseekr1.jinja`, and no reasoning parser on purpose, so `<think>` reasoning arrives inline in `content`. The three provider overlays render identical vLLM arguments for both roles.
- **amd** (`vllm-openai-rocm` v0.32.0-rc.2): `deepseek-ai/DeepSeek-V3`, `--max-model-len 36864`, no parsers, prefix caching off. DeepSeek-V3 is not a reasoning model.
- **xpu** (`llm-d-xpu` v1.0.0): `deepseek-ai/DeepSeek-V2-Lite-Chat`, `MAX_MODEL_LEN` 2048, no parsers, aggregated (no P/D, no routing sidecar). The model part lists the cases whose budgets a 2048-token window cannot hold.

</details>

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL='https://your-router.example' # replace with your router's URL
llmdv verify --profile 'profiles/llm-d/wide-ep/<profile>.yaml'
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
- `openai_chat.streaming_include_usage`
- `openai_chat.continuous_usage`

**openai_completions**

- `openai_completions.basic_non_streaming`
- `openai_completions.streaming_matches_json`
- `openai_completions.stop_reason`
- `openai_completions.prompt_forms`
- `openai_completions.multiple_choices`

**anthropic_messages**

- `anthropic_messages.basic_non_streaming`
- `anthropic_messages.streaming_sse`

**openai_responses**

- `openai_responses.basic_non_streaming`
- `openai_responses.streaming_sse`

**streaming**

- `streaming.sse_framing`
- `streaming.*`

**unhappy**

- `unhappy.unknown_path`
- `unhappy.malformed_json`
- `unhappy.missing_model`
- `unhappy.wrong_method`
- `unhappy.error_envelope_fields`
- `unhappy.context_overflow`
- `unhappy.token_limit_over_context`
- `unhappy.invalid_token_limit`
- `unhappy.streaming_validation`
- `unhappy.wrong_field_type`

**transport**

- `transport.connection`
- `transport.health_status`

**tools**

- `tools.auto_call`
- `tools.auto_stream`

**long_context**

- `long_context.*`

**scenarios**

- `scenarios.cancel_burst`


</details>

Counted offline by `llmdv plan`, one case per ingress and variant. Planned runs can still skip at runtime. Expand a profile for its scope counts and separate skip classes.

The default is also `--all-test-sets` for these profiles.

<details>
<summary>vllm-deepseek-r1-0528-tools-standalone.yaml</summary>

[Open the profile](vllm-deepseek-r1-0528-tools-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 124 | 118 | 6 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 918 | 174 |

Planned skips by class:

- **Default:** 6 `undeclared`.
- **Full library:** 174 `undeclared`.

</details>

<details>
<summary>vllm-deepseek-v2-lite-chat-ctx2k-inline-standalone.yaml</summary>

[Open the profile](vllm-deepseek-v2-lite-chat-ctx2k-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 124 | 110 | 14 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 659 | 433 |

Planned skips by class:

- **Default:** 14 `undeclared`.
- **Full library:** 433 `undeclared`.

</details>

<details>
<summary>vllm-deepseek-v3-ctx36k-inline-standalone.yaml</summary>

[Open the profile](vllm-deepseek-v3-ctx36k-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 124 | 110 | 14 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 659 | 433 |

Planned skips by class:

- **Default:** 14 `undeclared`.
- **Full library:** 433 `undeclared`.

</details>

Preview cases and reasons with `llmdv plan --profile <profile>`.

## Not covered

- **Overlays llm-d does not test nightly:** `gpu/vllm/base`, `amd/vllm/base` (each the same to a client as the tested overlay) and `xpu/vllm/base` (Qwen/Qwen3-235B-A22B with DP/EP across pods), and gateway mode.
- **The guide's mechanism checks** (`guide.yaml` `verify.tests`): the concurrent `dp_requests` batch needs load mode (#23); per-DP-rank spread and connector prefix-cache hits (`rank_metrics`) and the router's P/D decision counter (`router_metrics`) need per-pod metric reads through cluster access a committed profile does not carry, and a routing case family (#27). Rollout gating needs a rollout during the run. Each is mapped in `parts/common.yaml`.
- **Prefix reuse:** no overlay passes `--enable-prompt-tokens-details`, so `cache_reuse` is not declared.

## For maintainers

<details>
<summary>llm-d's nightly lanes, and how each maps to a profile</summary>

The guide's five nightly lanes (`nightly-e2e-wide-ep-<lane>.yaml` in llm-d, each in standalone mode with `gateway_class: epponly`) deploy three configurations that differ to a client:

| CI lane | Overlay it deploys (`ACCELERATOR_TYPE/MODEL_SERVER/INFRA_PROVIDER`) | Profile (`wide-ep/`) | Identity flags |
| --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `gpu/vllm/gke` | `vllm-deepseek-r1-0528-tools-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `cks-acc-gpu-vllm-x` | `gpu/vllm/coreweave` | `vllm-deepseek-r1-0528-tools-standalone` | `--identity accelerator=gpu --identity provider=coreweave` |
| `ibm-acc-gpu-vllm-x` | `gpu/vllm/ocp` | `vllm-deepseek-r1-0528-tools-standalone` | `--identity accelerator=gpu --identity provider=ocp` |
| `amd-ci-acc-rocm-vllm-moriio` | `amd/vllm/amd-ci` | `vllm-deepseek-v3-ctx36k-inline-standalone` | `--identity accelerator=amd --identity provider=amd-ci` |
| `intel-acc-xpu-vllm-x` | `xpu/vllm/xpu-ci` | `vllm-deepseek-v2-lite-chat-ctx2k-inline-standalone` | `--identity accelerator=xpu --identity provider=xpu-ci` |

No two lanes share an identity, so none needs `--identity flavor`.

</details>

<details>
<summary>How these profiles are built</summary>

How the parts and combinations fit together, and the provenance tags, are in the [profile provenance guide](../../../docs/profile-provenance.md#how-profilesllm-d-is-built).

</details>

<details>
<summary>Where llm-d's own sources disagree</summary>

Facts in llm-d that do not agree with each other:

- `ibm-acc-gpu-vllm-x` deploys `INFRA_PROVIDER=ocp` (`guides/wide-ep/modelserver/gpu/vllm/ocp/`), but `guides/wide-ep/guide.yaml` lists only `base`, `gke` and `coreweave` as gpu providers, and its `INFRA_PROVIDER` values omit `ocp`.
- `intel-acc-xpu-vllm-x` deploys `xpu/vllm/xpu-ci`, which serves `deepseek-ai/DeepSeek-V2-Lite-Chat` (TP2 with expert parallelism within each pod, no data parallelism; its kustomization says CI has no RDMA network), while `guide.yaml` and the README's support table advertise `Qwen/Qwen3-235B-A22B` on 16 XPUs with DP2/EP across pods and list only `base` as an xpu provider. `xpu-ci` is in `INFRA_PROVIDER`'s values but not in the xpu provider list. The xpu-ci LeaderWorkerSet keeps the name `wide-ep-xpu-qwen3-235b`.
- The README's overview says the Intel XPU configuration uses "two plain `LeaderWorkerSet`s"; the support table and `modelserver/xpu/vllm/base/lws.yaml` have one aggregated `LeaderWorkerSet` of two pods.
- The xpu lane is aggregated, so `guide.yaml`'s mechanism text (every request disaggregated) and `router/wide-ep.values.yaml`'s P/D plugins do not apply to it; `router/xpu.values.yaml` replaces them.

</details>
