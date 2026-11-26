# predicted-latency-routing

Profiles for testing a deployment of llm-d's [predicted-latency-routing](https://github.com/llm-d/llm-d/tree/main/guides/predicted-latency-routing) guide, in which the router scores pods on latency predicted by models it trains on its own completed requests; the predictor runs as sidecars in the EPP pod and is not on the client's path.

[What it tests](#what-it-tests) · [Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## What it tests

**Validation focus: serving correctness with predictor routing enabled.** Exercise streaming and non-streaming behavior and the configured context window to detect regressions on the predictor’s serving path.

**What a pass establishes:** the checks that actually executed held at the reachable ingress
during this run. Skipped checks remain coverage gaps. **Coverage boundary:** Predictor accuracy and latency improvements need timing experiments and routing evidence.

These focused defaults are a starting point for validating a production deployment. Add coverage
for the APIs, clients and operating conditions you rely on; see
[Growing coverage toward production](../../../docs/getting-started.md#widen-coverage).

<details>
<summary>Why these checks relate to the guide</summary>

Every streaming case, on top of the base tests every guide gets (each API, error handling, the parsers and the context window): the guide serves streaming and non-streaming clients alike, and on NVIDIA and AMD at a RoPE-scaled 128k window. It checks that the stack serves correctly with the predictor routing, not that the predictor works or lowers latency: see [Not covered](#not-covered).

The declaration rationale and claim-to-case mapping live in
[`parts/common.yaml`](parts/common.yaml). [Not covered](#not-covered) explains the remaining
access, instrumentation and load requirements.

</details>

## Pick your profile

Pick the profile for the overlay you deployed (under `guides/predicted-latency-routing/modelserver/`). The profiles assume the guide's routing-only router values (`SLO_AWARE=false`); the SLO-aware values are [not covered](#not-covered).

| Model a client sees | Overlays | Profile |
| --- | --- | --- |
| Qwen3-32B on vLLM, 128k window, with reasoning and tool-call parsers | `gpu/vllm/base`, `gpu/vllm/gke`, `gpu/vllm/ocp`, `amd/vllm` | [vllm-qwen3-32b-ctx128k-parsers-standalone](vllm-qwen3-32b-ctx128k-parsers-standalone.yaml) |
| Qwen3-0.6B on vLLM, reasoning inline, no tool parser | `xpu/vllm` | [vllm-qwen3-0.6b-inline-standalone](vllm-qwen3-0.6b-inline-standalone.yaml) |

The overlays reuse optimized-baseline's model servers, so the pods carry `llm-d.ai/guide=optimized-baseline`. The profiles depend on no label, but if you add a fleet block, select the pods by that label, not by this guide's name.

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL='https://your-router.example' # replace with your router's URL
llmdv verify --profile 'profiles/llm-d/predicted-latency-routing/<profile>.yaml'
```

To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional for verification. Set them when comparing deployments that share a profile, so their recorded configuration dimensions remain distinct; for example `--identity accelerator=amd --identity provider=base`, read off your overlay path. The NVIDIA and AMD overlays serve the same model, window and parsers, so their runs are worth comparing.


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
<summary>vllm-qwen3-0.6b-inline-standalone.yaml</summary>

[Open the profile](vllm-qwen3-0.6b-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 103 | 91 | 12 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 678 | 414 |

Planned skips by class:

- **Default:** 12 `undeclared`.
- **Full library:** 414 `undeclared`.

</details>

<details>
<summary>vllm-qwen3-32b-ctx128k-parsers-standalone.yaml</summary>

[Open the profile](vllm-qwen3-32b-ctx128k-parsers-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 103 | 97 | 6 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 920 | 172 |

Planned skips by class:

- **Default:** 6 `undeclared`.
- **Full library:** 172 `undeclared`.

</details>

Preview cases and reasons with `llmdv plan --profile <profile>`.

## Not covered

- **Configurations without a profile:** SGLang on NVIDIA GPU, TPU v6e and v7 (all `community`); the SLO-aware router values (`router/predicted-latency-slo.values.yaml`, `skip_in: [ci]` in `guide.yaml`), the P/D and multimodal values; gateway mode.
- **The predictor itself**: training sample counts and `/status` on the EPP pod's sidecar ports, and the router's predicted-versus-actual TTFT and TPOT histograms (`MONITORING=true`, which the covered configurations do not enable). They need cluster access a committed profile does not carry.
- **Latency claims and the traffic burst**: a benchmark or load mode (#23), not functional cases.
- **SLO headers with an effect**: none of the tested configurations sets an admission plugin, so the topology part's `ignored` declarations stand.
- **`cache_reuse`**: no overlay passes `--enable-prompt-tokens-details`.

## For maintainers

<details>
<summary>llm-d's nightly workflows, and how each maps to a profile</summary>

Five nightly workflows (`nightly-e2e-predicted-latency-routing-<workflow-suffix>.yaml`, each `gateway_class: epponly`, so standalone, and each with the routing-only router values, `SLO_AWARE=false`) deploy two configurations that differ to a client.

| upstream workflow | Overlay (`ACCELERATOR_TYPE/MODEL_SERVER/INFRA_PROVIDER`) | Profile | Identity flags |
| --- | --- | --- | --- |
| `cks-acc-gpu-vllm-x` | `gpu/vllm/base` | `vllm-qwen3-32b-ctx128k-parsers-standalone` | `--identity accelerator=gpu --identity provider=base` |
| `gke-acc-gpu-vllm-x` | `gpu/vllm/gke` | `vllm-qwen3-32b-ctx128k-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `ibm-acc-gpu-vllm-x` | `gpu/vllm/ocp` | `vllm-qwen3-32b-ctx128k-parsers-standalone` | `--identity accelerator=gpu --identity provider=ocp` |
| `amd-ci-acc-rocm-vllm-x` | `amd/vllm` (no provider; includes `base`) | `vllm-qwen3-32b-ctx128k-parsers-standalone` | `--identity accelerator=amd --identity provider=base` |
| `intel-acc-xpu-vllm-x` | `xpu/vllm` (no provider; includes `base`, which is optimized-baseline's `xpu/vllm`) | `vllm-qwen3-0.6b-inline-standalone` | `--identity accelerator=xpu --identity provider=base` |

A workflow that sets no provider applies the folder's own kustomization, which includes only `base`, so it passes `provider=base`. No two workflows share both a profile and an accelerator and provider, so none passes `flavor`. The NVIDIA and AMD workflows run the same model, window and parsers, so their runs are worth comparing with `llmdv compare`.

</details>

<details>
<summary>How these profiles were written</summary>

Written from the shipped configuration (each workflow's overlay rendered with `kubectl kustomize`) and the API definitions. The layout and provenance tags are in the [profile provenance guide](../../../docs/profile-provenance.md#how-profilesllm-d-is-built), and the shared router claims in [../optimized-baseline/parts/common.yaml](../optimized-baseline/parts/common.yaml); this guide's claims and where each lands are in [parts/common.yaml](parts/common.yaml).

</details>

<details>
<summary>Where llm-d's own sources disagree</summary>

- **The model servers are optimized-baseline's.** Every overlay builds on `guides/optimized-baseline/modelserver/...` (the GPU and AMD bases patch it; the XPU base includes optimized-baseline's `xpu/vllm` unchanged), so the rendered Deployments are named `optimized-baseline-*` and the pods carry `llm-d.ai/guide=optimized-baseline`. The router values select them with `modelServers.matchLabels: llm-d.ai/guide: optimized-baseline`. These profiles depend on no label; a workflow that adds a fleet block must select this guide's pods by that label, not by the guide's own name.
- **AMD adds parsers.** `modelserver/amd/vllm/base/patch-vllm.yaml` adds `--reasoning-parser=qwen3`, `--tool-call-parser=hermes` and `--enable-auto-tool-choice`, which optimized-baseline's AMD overlay does not pass, so AMD here looks like NVIDIA to a client.
- **`ocp` provider.** The `ibm-acc-gpu-vllm-x` workflow deploys `gpu/vllm/ocp`, but `ocp` is not among `guide.yaml`'s `INFRA_PROVIDER` values (`base`, `gke`) or the `gpu` providers.
- **TPU provider notes.** The `tpu/v6` and `tpu/v7` notes in `guide.yaml` say "`INFRA_PROVIDER`: `base`, `gke`" while their `providers` list only `gke`. No workflow runs them.

</details>
