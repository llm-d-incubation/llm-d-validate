# optimized-baseline

Profiles for testing a deployment of llm-d's [optimized-baseline](https://github.com/llm-d/llm-d/tree/main/guides/optimized-baseline) guide, in which a model server runs behind llm-d's router (the EPP), which places each request by prefix-cache affinity and token load.

[Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What it tests](#what-it-tests) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## Pick your profile

Pick the profile for the overlay you deployed (under `guides/optimized-baseline/modelserver/`, as `ACCELERATOR_TYPE/MODEL_SERVER/INFRA_PROVIDER`). A provider overlay changes only node, cache and NCCL settings, so overlays that differ only in provider share a profile.

| Profile | Model a client sees | Overlays |
| --- | --- | --- |
| `vllm-qwen3-32b-parsers-standalone` | Qwen3-32B on vLLM, with reasoning and tool-call parsers | `gpu/vllm/base`, `gpu/vllm/gke`, `gpu/vllm/ocp` |
| `vllm-qwen3-32b-inline-standalone` | Qwen3-32B on vLLM, reasoning inline, no tool parser | `amd/vllm/amd-ci`, `tpu/v5p/vllm` |
| `vllm-qwen3-0.6b-inline-standalone` | Qwen3-0.6B on vLLM (an Intel XPU build), reasoning inline, no tool parser | `xpu/vllm` |
| `sglang-qwen3-32b-parsers-standalone` | Qwen3-32B on SGLang, with parsers | `gpu/sglang/gke` |
| `sglang-qwen3-32b-ctx16k-parsers-standalone` | Qwen3-32B on SGLang, 16k window, with parsers | `tpu/v5p/sglang` |
| `sglang-qwen3-32b-inline-standalone` | Qwen3-32B on SGLang, reasoning inline, no tool parser | `amd/sglang/amd-ci` |
| `atom-qwen3-32b-ctxunknown-inline-standalone` | Qwen3-32B on Atom, reasoning inline, no tool parser, window not yet known | `amd/atom/amd-ci` |
| `sim-qwen3-32b-standalone` | the llm-d inference simulator answering as Qwen3-32B | none: simulator replicas in place of the model server |

`sim-qwen3-32b-standalone` is for a cluster without accelerators, such as kind: it runs the guide's router in front of llm-d-inference-sim, so it tests the router, not an engine.

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/optimized-baseline/<profile>.yaml
```

To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional: they label a run so `llmdv compare` never compares two configurations, for example `--identity accelerator=gpu --identity provider=gke`, read off your overlay path (`provider=base` if you applied the accelerator folder's own kustomization).

## What it tests

The base tests every guide gets: each API through the router, error handling, streaming pass-through, the parsers and the context window, and that the deployment serves exactly one model. The guide's own check, `/v1/completions` answering, also ten times with a shared prefix, is covered by the completions cases. For the Atom profile no source states the window, so its context-overflow and long-context cases skip as `undeclared` until a run supplies it.

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
| `openai_completions` | `openai_completions.basic_non_streaming`, `openai_completions.streaming_matches_json`, `openai_completions.stop_reason`, `openai_completions.prompt_forms` |
| `anthropic_messages` | `anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse` |
| `openai_responses` | `openai_responses.basic_non_streaming`, `openai_responses.streaming_sse` |
| `streaming` | `streaming.sse_framing`, `streaming.*` |
| `unhappy` | `unhappy.unknown_path`, `unhappy.malformed_json`, `unhappy.missing_model`, `unhappy.wrong_method`, `unhappy.error_envelope_fields`, `unhappy.context_overflow`, `unhappy.token_limit_over_context` |
| `transport` | `transport.connection`, `transport.health_status` |
| `tools` | `tools.auto_call`, `tools.auto_stream` |
| `long_context` | `long_context.*` |

</details>

<details>
<summary>How many cases each scope selects, per profile</summary>

Counted offline by `llmdv plan`, one per ingress and variant. *Run* cases can still skip during a run (for example `throttled`); *skip* cases are ruled out by the profile's declarations, with their skip class.

| Profile | Default (= `--all-test-sets`) | `smoke` | `--full-suite` |
| --- | --- | --- | --- |
| `atom-qwen3-32b-ctxunknown-inline-standalone.yaml` | 106 selected, 94 run, 12 skip: 12 undeclared | 9 selected, 9 run | 1092 selected, 678 run, 414 skip: 414 undeclared |
| `sglang-qwen3-32b-ctx16k-parsers-standalone.yaml` | 106 selected, 100 run, 6 skip: 6 undeclared | 9 selected, 9 run | 1092 selected, 920 run, 172 skip: 172 undeclared |
| `sglang-qwen3-32b-inline-standalone.yaml` | 106 selected, 94 run, 12 skip: 12 undeclared | 9 selected, 9 run | 1092 selected, 678 run, 414 skip: 414 undeclared |
| `sglang-qwen3-32b-parsers-standalone.yaml` | 106 selected, 100 run, 6 skip: 6 undeclared | 9 selected, 9 run | 1092 selected, 920 run, 172 skip: 172 undeclared |
| `sim-qwen3-32b-standalone.yaml` | 106 selected, 92 run, 14 skip: 14 undeclared | 9 selected, 9 run | 1092 selected, 648 run, 444 skip: 444 undeclared |
| `vllm-qwen3-0.6b-inline-standalone.yaml` | 106 selected, 94 run, 12 skip: 12 undeclared | 9 selected, 9 run | 1092 selected, 678 run, 414 skip: 414 undeclared |
| `vllm-qwen3-32b-inline-standalone.yaml` | 106 selected, 94 run, 12 skip: 12 undeclared | 9 selected, 9 run | 1092 selected, 678 run, 414 skip: 414 undeclared |
| `vllm-qwen3-32b-parsers-standalone.yaml` | 106 selected, 100 run, 6 skip: 6 undeclared | 9 selected, 9 run | 1092 selected, 920 run, 172 skip: 172 undeclared |

</details>

Preview individual cases and skip reasons with `llmdv plan --profile <profile>`.

## Not covered

See [optimized-baseline details](../README.md#for-maintainers): the community overlays, gateway mode and agentgateway as the standalone sidecar proxy are configurations llm-d does not test nightly, so no profile here covers them.

## For maintainers

<details>
<summary>llm-d's nightly lanes, and how each maps to a profile</summary>

Why the lanes group as they do: [optimized-baseline details](../README.md#for-maintainers).

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
