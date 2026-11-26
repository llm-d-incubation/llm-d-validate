# pd-disaggregation

Profiles for testing a deployment of llm-d's [pd-disaggregation](https://github.com/llm-d/llm-d/tree/main/guides/pd-disaggregation) guide, in which prefill and decode run on separate pods: the router (the EPP) schedules each request onto a prefill pod and a decode pod, and the decode pod's routing sidecar runs the prefill, then the decode, and returns one response.

[What it tests](#what-it-tests) · [Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## What it tests

**Validation focus: api correctness through the disaggregated path.** Exercise fields, errors and streaming so changes introduced by the routing sidecar or either engine become visible to the client.

**What a pass establishes:** the checks that actually executed held at the reachable ingress
during this run. Skipped checks remain coverage gaps. **Coverage boundary:** A successful response does not establish that prefill and decode were actually split.

These focused defaults are a starting point for validating a production deployment. Add coverage
for the APIs, clients and operating conditions you rely on; see
[Growing coverage toward production](../../../docs/getting-started.md#widen-coverage).

<details>
<summary>Why these checks relate to the guide</summary>

The base tests every guide gets (each API, error handling, streaming, the parsers and the context window), sent through the disaggregated path. Nothing about that path changes what a correct API returns, so a field or option the routing sidecar drops or mangles on its way to either engine fails its case. It checks that the stack serves correctly when disaggregated, not that requests are actually split: see [Not covered](#not-covered).

The declaration rationale and claim-to-case mapping live in
[`parts/common.yaml`](parts/common.yaml). [Not covered](#not-covered) explains the remaining
access, instrumentation and load requirements.

</details>

## Pick your profile

Pick the profile for the overlay you deployed (under `guides/pd-disaggregation/modelserver/`). Prefill and decode carry the same model id, window and parser flags, so a client sees one model.

| Model a client sees | Overlays | Profile |
| --- | --- | --- |
| gpt-oss-120b on vLLM v0.31.0 (also the ROCm build), with reasoning and tool-call parsers | `gpu/vllm/gke/base`, `gpu/vllm/coreweave`, `gpu/vllm/ocp`, `amd/vllm/nixl/amd-ci` | [vllm-gpt-oss-120b-parsers-standalone](vllm-gpt-oss-120b-parsers-standalone.yaml) |
| gpt-oss-120b on SGLang v0.5.22, no parsers: the reasoning text arrives in `content`, and no tool calls | `gpu/sglang/gke` | [sglang-gpt-oss-120b-inline-standalone](sglang-gpt-oss-120b-inline-standalone.yaml) |
| Qwen3-32B on vLLM v0.32.0-rc.2 (llm-d ROCm build), reasoning inline, no tool parser | `amd/vllm/moriio/amd-ci-1p1d-tp8` | [vllm-qwen3-32b-inline-standalone](vllm-qwen3-32b-inline-standalone.yaml) |
| Qwen3-32B on vllm-tpu v0.31.0, 9216-token window, with parsers | `tpu/v5p/vllm` | [vllm-qwen3-32b-ctx9k-parsers-standalone](vllm-qwen3-32b-ctx9k-parsers-standalone.yaml) |
| Qwen3-0.6B on llm-d-xpu v1.0.0, 32000-token window, no parsers | `xpu/vllm` | [vllm-qwen3-0.6b-ctx32000-inline-standalone](vllm-qwen3-0.6b-ctx32000-inline-standalone.yaml) |

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL='https://your-router.example' # replace with your router's URL
llmdv verify --profile 'profiles/llm-d/pd-disaggregation/<profile>.yaml'
```

To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional for verification. Set them when comparing deployments that share a profile, so their recorded configuration dimensions remain distinct; for example `--identity accelerator=gpu --identity provider=coreweave`, read off your overlay path.


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


</details>

Counted offline by `llmdv plan`, one case per ingress and variant. Planned runs can still skip at runtime. Expand a profile for its scope counts and separate skip classes.

The default is also `--all-test-sets` for these profiles.

<details>
<summary>sglang-gpt-oss-120b-inline-standalone.yaml</summary>

[Open the profile](sglang-gpt-oss-120b-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 121 | 107 | 14 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 659 | 433 |

Planned skips by class:

- **Default:** 14 `undeclared`.
- **Full library:** 433 `undeclared`.

</details>

<details>
<summary>vllm-gpt-oss-120b-parsers-standalone.yaml</summary>

[Open the profile](vllm-gpt-oss-120b-parsers-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 121 | 115 | 6 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 920 | 172 |

Planned skips by class:

- **Default:** 6 `undeclared`.
- **Full library:** 172 `undeclared`.

</details>

<details>
<summary>vllm-qwen3-0.6b-ctx32000-inline-standalone.yaml</summary>

[Open the profile](vllm-qwen3-0.6b-ctx32000-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 121 | 109 | 12 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 678 | 414 |

Planned skips by class:

- **Default:** 12 `undeclared`.
- **Full library:** 414 `undeclared`.

</details>

<details>
<summary>vllm-qwen3-32b-ctx9k-parsers-standalone.yaml</summary>

[Open the profile](vllm-qwen3-32b-ctx9k-parsers-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 121 | 115 | 6 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 920 | 172 |

Planned skips by class:

- **Default:** 6 `undeclared`.
- **Full library:** 172 `undeclared`.

</details>

<details>
<summary>vllm-qwen3-32b-inline-standalone.yaml</summary>

[Open the profile](vllm-qwen3-32b-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 121 | 109 | 12 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 678 | 414 |

Planned skips by class:

- **Default:** 12 `undeclared`.
- **Full library:** 414 `undeclared`.

</details>

Preview cases and reasons with `llmdv plan --profile <profile>`.

## Not covered

- **That requests are actually split** (prefill counters grow on prefill pods, `vllm:external_prefix_cache_hits_total` on decode pods, the router's `llm_d_epp_disagg_decision_total`): no response says which pod prefilled it, and a decode sidecar can serve a request on its own. These need per-role pod and EPP metrics read before and after, which need replica sets and cluster reads a committed profile does not carry (#24).
- **Prefill prefix affinity and decode active-request balancing**: needs `routing.prefix_affinity` against the prefill role and per-pod counter deltas (#27).
- **Prompt cache reuse** (`cache_reuse`): none of the overlays covered by these profiles has its engine report `cached_tokens`; only the community `tpu/v7` overlays pass `--enable-prompt-tokens-details` or `--enable-cache-report`.
- **Latency, throughput, KV-transfer stalls**: a benchmark or load mode (#23), not `llmdv`.
- **Configurations without a profile**: every `community` cell in guide.yaml, and the validated cells' other providers (`gpu` base, `gke/a4x`, `gke/a4xmax`, `aws`, `cks-mooncake`; `amd` `nixl/base`, `nixl/oci`, `nixl/tensorwave`, `moriio/base`, `moriio/amd-ci`; `xpu` `rdma`). Gateway mode also has no profile.

## For maintainers

<details>
<summary>llm-d's nightly workflows, and how each maps to a profile</summary>

Every workflow deploys the guide in standalone mode, so every profile has one ingress, the router (`<GUIDE_NAME>-epp`, port 80). In every workflow, prefill and decode carry the same model id, window and parser flags, so a client sees one model. Each combination's header names its workflows and overlays; `parts/common.yaml` maps the guide's claims to cases or to what is not covered.

One profile per configuration a client can tell apart. Each workflow is one of llm-d's `nightly-e2e-pd-disaggregation-<workflow-suffix>` workflows; the overlay is `modelserver/<ACCELERATOR_TYPE>/<MODEL_SERVER>/<INFRA_PROVIDER>/`, rendered locally with `kubectl kustomize`.

| Workflow | Overlay | Engine, model | Profile | Identity flags |
| --- | --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `gpu/vllm/gke/base` | vLLM v0.31.0, gpt-oss-120b, parsers | `vllm-gpt-oss-120b-parsers-standalone.yaml` | `--identity accelerator=gpu --identity provider=gke/base` |
| `cks-acc-gpu-vllm-x` | `gpu/vllm/coreweave` | vLLM v0.31.0, gpt-oss-120b, parsers | `vllm-gpt-oss-120b-parsers-standalone.yaml` | `--identity accelerator=gpu --identity provider=coreweave` |
| `ibm-acc-gpu-vllm-x` | `gpu/vllm/ocp` | vLLM v0.31.0, gpt-oss-120b, parsers | `vllm-gpt-oss-120b-parsers-standalone.yaml` | `--identity accelerator=gpu --identity provider=ocp` |
| `amd-ci-acc-rocm-vllm-nixl` | `amd/vllm/nixl/amd-ci` | vLLM v0.31.0 (ROCm), gpt-oss-120b, parsers | `vllm-gpt-oss-120b-parsers-standalone.yaml` | `--identity accelerator=amd --identity provider=nixl/amd-ci` |
| `gke-acc-gpu-sglang-x` | `gpu/sglang/gke` | SGLang v0.5.22, gpt-oss-120b, no parsers | `sglang-gpt-oss-120b-inline-standalone.yaml` | `--identity accelerator=gpu --identity provider=gke` |
| `amd-ci-acc-rocm-vllm-moriio` | `amd/vllm/moriio/amd-ci-1p1d-tp8` | vLLM v0.32.0-rc.2 (llm-d ROCm build), Qwen3-32B, no parsers | `vllm-qwen3-32b-inline-standalone.yaml` | `--identity accelerator=amd --identity provider=moriio/amd-ci-1p1d-tp8` |
| `gke-acc-tpu-vllm-x` | `tpu/v5p/vllm` (no provider: the folder's own kustomization, which includes `base`) | vllm-tpu v0.31.0, Qwen3-32B at 9216 tokens, parsers | `vllm-qwen3-32b-ctx9k-parsers-standalone.yaml` | `--identity accelerator=tpu/v5p --identity provider=base` |
| `intel-acc-xpu-vllm-x` | `xpu/vllm` (no provider: the folder's own kustomization, which includes `base`) | llm-d-xpu v1.0.0, Qwen3-0.6B at 32000 tokens, no parsers | `vllm-qwen3-0.6b-ctx32000-inline-standalone.yaml` | `--identity accelerator=xpu --identity provider=base` |

The workflows pass `infra_provider` and `connector` separately to llm-d-infra's reusable workflow (`amd-ci` with `nixl`, `amd-ci-1p1d-tp8` with `moriio`); the overlay paths above are the `nixl/amd-ci` and `moriio/amd-ci-1p1d-tp8` values guide.yaml lists for `INFRA_PROVIDER`, and the provider identity uses them so the AMD workflows never share an identity. Four workflows share the gpt-oss-120b vLLM profile: accelerator and provider tell their runs apart, and no `flavor` is needed.

</details>

<details>
<summary>Where llm-d's own sources disagree</summary>

Facts found while writing these profiles, for llm-d to decide on:

- `ibm-acc-gpu-vllm-x` deploys `gpu/vllm/ocp`, but `ocp` is not among the gpu `providers` in `guides/pd-disaggregation/guide.yaml`, nor among the `INFRA_PROVIDER` values there.
- `amd-ci-acc-rocm-vllm-moriio` deploys `amd/vllm/moriio/amd-ci-1p1d-tp8`, which serves `Qwen/Qwen3-32B` with no reasoning or tool parser, while guide.yaml's `amd` cell advertises `openai/gpt-oss-120b` (only the `MODEL` comment mentions the MoRI-IO model). The NIXL workflow on the same accelerator serves gpt-oss-120b with both parsers, so the two AMD workflows present different models and API shapes under one validated cell.
- The MoRI-IO overlay's routing sidecar takes `--secure-proxy=false` where every other workflow's takes `--secure-serving=false`; it also passes `--kv-connector=nixlv2` beside its MoRI-IO flags (`guides/pd-disaggregation/modelserver/amd/vllm/moriio/`).
- `gke-acc-gpu-sglang-x` serves gpt-oss-120b on SGLang with no `--reasoning-parser` and no `--tool-call-parser`, while the vLLM workflows of the same cell pass `openai_gptoss` and `openai`: a client of the SGLang workflow gets harmony analysis text in `content` and no tool calls.
- `gke-acc-gpu-vllm-x` passes `infra_provider` `gke/base`, which is not among guide.yaml's `INFRA_PROVIDER` values (`gke` is; `gpu/vllm/gke/kustomization.yaml` includes `./base`, and the two render the same engine arguments). Likewise the AMD workflows' `nixl/amd-ci` and `moriio/amd-ci-1p1d-tp8` are `INFRA_PROVIDER` values but not among the `amd` cell's `providers`.
- `gke-acc-tpu-vllm-x` and `intel-acc-xpu-vllm-x` set no `infra_provider`, so they apply the accelerator folder's own kustomization; guide.yaml lists only `gke` for `tpu/v5p` (the folder includes `base`, which renders the same engine arguments).

</details>
