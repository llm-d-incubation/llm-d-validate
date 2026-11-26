# optimized-baseline

Profiles for testing a deployment of llm-d's [optimized-baseline](https://github.com/llm-d/llm-d/tree/main/guides/optimized-baseline) guide, in which a model server runs behind llm-d's router (the EPP), which places each request by prefix-cache affinity and token load.

[What it tests](#what-it-tests) · [Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## What it tests

**Validation focus: the baseline router-to-model api contract.** Check advertised models, generation, streaming, parsers and errors because later guide configurations depend on this serving contract.

**What a pass establishes:** the checks that actually executed held at the reachable ingress
during this run. Skipped checks remain coverage gaps. **Coverage boundary:** API correctness does not establish prefix affinity, throughput or backend placement.

These focused defaults are a starting point for validating a production deployment. Add coverage
for the APIs, clients and operating conditions you rely on; see
[Growing coverage toward production](../../../docs/getting-started.md#widen-coverage).

<details>
<summary>Why these checks relate to the guide</summary>

The base tests every guide gets: each API through the router, error handling, streaming pass-through, the parsers and the context window, and that the deployment serves exactly one model. The guide's own check, `/v1/completions` answering, also ten times with a shared prefix, is covered by the completions cases. For the Atom profile no source states the window, so its context-overflow and long-context cases skip as `undeclared` until a run supplies it.

The declaration rationale and claim-to-case mapping live in
[`parts/common.yaml`](parts/common.yaml). [Not covered](#not-covered) explains the remaining
access, instrumentation and load requirements.

</details>

## Pick your profile

Pick the profile for the overlay you deployed (under `guides/optimized-baseline/modelserver/`, as `ACCELERATOR_TYPE/MODEL_SERVER/INFRA_PROVIDER`). A provider overlay changes only node, cache and NCCL settings, so overlays that differ only in provider share a profile.

| Model a client sees | Overlays | Profile |
| --- | --- | --- |
| Qwen3-32B on vLLM, with reasoning and tool-call parsers | `gpu/vllm/base`, `gpu/vllm/gke`, `gpu/vllm/ocp` | [vllm-qwen3-32b-parsers-standalone](vllm-qwen3-32b-parsers-standalone.yaml) |
| Qwen3-32B on vLLM, reasoning inline, no tool parser | `amd/vllm/amd-ci`, `tpu/v5p/vllm` | [vllm-qwen3-32b-inline-standalone](vllm-qwen3-32b-inline-standalone.yaml) |
| Qwen3-0.6B on vLLM (an Intel XPU build), reasoning inline, no tool parser | `xpu/vllm` | [vllm-qwen3-0.6b-inline-standalone](vllm-qwen3-0.6b-inline-standalone.yaml) |
| Qwen3-32B on SGLang, with parsers | `gpu/sglang/gke` | [sglang-qwen3-32b-parsers-standalone](sglang-qwen3-32b-parsers-standalone.yaml) |
| Qwen3-32B on SGLang, 16k window, with parsers | `tpu/v5p/sglang` | [sglang-qwen3-32b-ctx16k-parsers-standalone](sglang-qwen3-32b-ctx16k-parsers-standalone.yaml) |
| Qwen3-32B on SGLang, reasoning inline, no tool parser | `amd/sglang/amd-ci` | [sglang-qwen3-32b-inline-standalone](sglang-qwen3-32b-inline-standalone.yaml) |
| Qwen3-32B on Atom, reasoning inline, no tool parser, window not yet known | `amd/atom/amd-ci` | [atom-qwen3-32b-ctxunknown-inline-standalone](atom-qwen3-32b-ctxunknown-inline-standalone.yaml) |
| the llm-d inference simulator answering as Qwen3-32B | none: simulator replicas in place of the model server | [sim-qwen3-32b-standalone](sim-qwen3-32b-standalone.yaml) |

`sim-qwen3-32b-standalone` is for a cluster without accelerators, such as kind: it runs the guide's router in front of llm-d-inference-sim, so it tests the router, not an engine.

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/optimized-baseline/<profile>.yaml
```

To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional for verification. Set them when comparing deployments that share a profile, so their recorded configuration dimensions remain distinct; for example `--identity accelerator=gpu --identity provider=gke`, read off your overlay path (`provider=base` if you applied the accelerator folder's own kustomization).


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
- `openai_completions.prompt_forms`

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
<summary>atom-qwen3-32b-ctxunknown-inline-standalone.yaml</summary>

[Open the profile](atom-qwen3-32b-ctxunknown-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 106 | 94 | 12 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 678 | 414 |

Planned skips by class:

- **Default:** 12 `undeclared`.
- **Full library:** 414 `undeclared`.

</details>

<details>
<summary>sglang-qwen3-32b-ctx16k-parsers-standalone.yaml</summary>

[Open the profile](sglang-qwen3-32b-ctx16k-parsers-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 106 | 100 | 6 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 920 | 172 |

Planned skips by class:

- **Default:** 6 `undeclared`.
- **Full library:** 172 `undeclared`.

</details>

<details>
<summary>sglang-qwen3-32b-inline-standalone.yaml</summary>

[Open the profile](sglang-qwen3-32b-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 106 | 94 | 12 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 678 | 414 |

Planned skips by class:

- **Default:** 12 `undeclared`.
- **Full library:** 414 `undeclared`.

</details>

<details>
<summary>sglang-qwen3-32b-parsers-standalone.yaml</summary>

[Open the profile](sglang-qwen3-32b-parsers-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 106 | 100 | 6 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 920 | 172 |

Planned skips by class:

- **Default:** 6 `undeclared`.
- **Full library:** 172 `undeclared`.

</details>

<details>
<summary>sim-qwen3-32b-standalone.yaml</summary>

[Open the profile](sim-qwen3-32b-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 106 | 92 | 14 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 648 | 444 |

Planned skips by class:

- **Default:** 14 `undeclared`.
- **Full library:** 444 `undeclared`.

</details>

<details>
<summary>vllm-qwen3-0.6b-inline-standalone.yaml</summary>

[Open the profile](vllm-qwen3-0.6b-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 106 | 94 | 12 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 678 | 414 |

Planned skips by class:

- **Default:** 12 `undeclared`.
- **Full library:** 414 `undeclared`.

</details>

<details>
<summary>vllm-qwen3-32b-inline-standalone.yaml</summary>

[Open the profile](vllm-qwen3-32b-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 106 | 94 | 12 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 678 | 414 |

Planned skips by class:

- **Default:** 12 `undeclared`.
- **Full library:** 414 `undeclared`.

</details>

<details>
<summary>vllm-qwen3-32b-parsers-standalone.yaml</summary>

[Open the profile](vllm-qwen3-32b-parsers-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 106 | 100 | 6 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 920 | 172 |

Planned skips by class:

- **Default:** 6 `undeclared`.
- **Full library:** 172 `undeclared`.

</details>

Preview cases and reasons with `llmdv plan --profile <profile>`.

## Not covered

No nightly lane covers these configurations, so no profile here covers them: the community
overlays (TPU v6e and v7, TPU v7 dynamic slicing, Rebellions NPU, CPU, Iluvatar, MetaX, Biren,
Enflame, Moore Threads, TensorRT-LLM, and SGLang on Intel XPU), gateway mode, and agentgateway
as the standalone sidecar proxy.

For library-wide limitations, see the [shared guide gaps](../README.md#not-covered).

## For maintainers

<details>
<summary>llm-d's nightly lanes, and how each maps to a profile</summary>

Lanes share a profile when their model, context window, parsers and engine match. Provider-only node, cache and NCCL settings do not require another profile. These lanes use no `flavor` dimension.

Every lane runs standalone: the client calls the `<GUIDE_NAME>-epp` Service on port 80 (the lanes' `gateway_host`). Lanes that share a profile are told apart by `--identity`; the provider value is the overlay's `INFRA_PROVIDER` folder, and `base` for a lane that sets none, which applies the `base` overlay.

| CI lane (`nightly-e2e-optimized-baseline-<lane>`) | Overlay it deploys (`ACCELERATOR_TYPE/MODEL_SERVER/INFRA_PROVIDER`) | Profile | Identity flags |
| --- | --- | --- | --- |
| `cks-acc-gpu-vllm-x` | `gpu/vllm/base` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=base` |
| `gke-acc-gpu-vllm-x` | `gpu/vllm/gke` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `ibm-acc-gpu-vllm-x` | `gpu/vllm/ocp` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=ocp` |
| `amd-acc-rocm-vllm-x` | `amd/vllm/amd-ci` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=amd-ci` |
| `gke-acc-tpu-vllm-x` | `tpu/v5p/vllm` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=tpu/v5p --identity provider=base` |
| `intel-acc-xpu-vllm-x` | `xpu/vllm` | `vllm-qwen3-0.6b-inline-standalone` | `--identity accelerator=xpu --identity provider=base` |
| `gke-acc-gpu-sglang-x` | `gpu/sglang/gke` | `sglang-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `gke-acc-tpu-sglang-x` | `tpu/v5p/sglang` | `sglang-qwen3-32b-ctx16k-parsers-standalone` | `--identity accelerator=tpu/v5p --identity provider=base` |
| `amd-acc-rocm-sglang-x` | `amd/sglang/amd-ci` | `sglang-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=amd-ci` |
| `amd-acc-rocm-atom-x` | `amd/atom/amd-ci` | `atom-qwen3-32b-ctxunknown-inline-standalone` | `--identity accelerator=amd --identity provider=amd-ci` |

`sim-qwen3-32b-standalone` is not a lane: it runs the guide's router in front of llm-d-inference-sim, for kind or CI without accelerators.

These lanes run the guide's `test_sets.default` (`parts/common.yaml`). The lanes that run every case with `--full-suite`, one per configuration, are listed in [Which lane runs the full suite](../README.md#for-maintainers).

</details>
