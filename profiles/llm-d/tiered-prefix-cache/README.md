# tiered-prefix-cache

Profiles for testing a deployment of llm-d's [tiered-prefix-cache](https://github.com/llm-d/llm-d/tree/main/guides/tiered-prefix-cache) guide, in which engines offload evicted KV-cache blocks to CPU RAM (or, with the `fs` variant, a shared filesystem) and the router scores pods on both tiers.

[Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What it tests](#what-it-tests) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## Pick your profile

Pick the profile for the overlay you deployed (under `guides/tiered-prefix-cache/modelserver/`). The connector (`lmcache-connector` or `native`) and the tier (`cpu` or `fs`) don't change what a client sees, so they share a profile.

| Profile | Model a client sees | Overlays |
| --- | --- | --- |
| `vllm-qwen3-32b-parsers-standalone` | Qwen3-32B on vLLM, with reasoning and tool-call parsers | `gpu/vllm/<connector>/cpu/<provider>` |
| `vllm-qwen3-32b-inline-standalone` | Qwen3-32B on vLLM, reasoning inline, no tool parser | `amd/vllm/<connector>/<tier>/amd-ci`, `tpu/v5p/vllm/native/cpu`, `xpu/vllm/native/cpu/base` |
| `vllm-qwen3-8b-ctx20k-inline-standalone` | Qwen3-8B on vLLM, 20k window, reasoning inline | `xpu/vllm/lmcache-connector/cpu/base` |
| `sglang-qwen3-32b-parsers-standalone` | Qwen3-32B on SGLang, with parsers | `gpu/sglang/native/cpu/gke` |
| `sglang-qwen3-32b-ctx16k-parsers-standalone` | Qwen3-32B on SGLang, 16k window, with parsers | `tpu/v5p/sglang/native/cpu` |

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/tiered-prefix-cache/<profile>.yaml
```

To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional: they label a run so `llmdv compare` never compares two configurations, for example `--identity accelerator=gpu --identity provider=gke --identity flavor=native/cpu`, read off your overlay path.

## What it tests

Completions that share a long prefix, the request the guide's own check sends, on top of the base tests every guide gets (each API, error handling, the parsers and the context window). It checks that the stack serves correctly with offloading on, not that offloading itself works: see [Not covered](#not-covered).

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
| `streaming` | `streaming.sse_framing` |
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
| `sglang-qwen3-32b-ctx16k-parsers-standalone.yaml` | 88 selected, 82 run, 6 skip: 6 undeclared | 9 selected, 9 run | 1092 selected, 920 run, 172 skip: 172 undeclared |
| `sglang-qwen3-32b-parsers-standalone.yaml` | 88 selected, 82 run, 6 skip: 6 undeclared | 9 selected, 9 run | 1092 selected, 920 run, 172 skip: 172 undeclared |
| `vllm-qwen3-32b-inline-standalone.yaml` | 88 selected, 76 run, 12 skip: 12 undeclared | 9 selected, 9 run | 1092 selected, 678 run, 414 skip: 414 undeclared |
| `vllm-qwen3-32b-parsers-standalone.yaml` | 88 selected, 82 run, 6 skip: 6 undeclared | 9 selected, 9 run | 1092 selected, 920 run, 172 skip: 172 undeclared |
| `vllm-qwen3-8b-ctx20k-inline-standalone.yaml` | 88 selected, 76 run, 12 skip: 12 undeclared | 9 selected, 9 run | 1092 selected, 678 run, 414 skip: 414 undeclared |

</details>

Preview individual cases and skip reasons with `llmdv plan --profile <profile>`.

## Not covered

- **Configurations no lane runs:** MooncakeStore, the NVIDIA and XPU `fs` variants, TPU v6e and v7 (including multi-host and the TPU v7 `fs` tier), SGLang `fs`; gateway mode.
- **Everything the offload does**: offload and load-back counters, the native connector's offload gate, the shared filesystem tier (a prefix primed on one pod hits on another) and the fs lanes' PVC. They need per-pod URLs, engine metrics or cluster objects a committed profile does not carry, and no response field shows a tier hit. A run of these profiles checks that the stack serves correctly with offloading on, not that offloading works.
- **`cache_reuse`**: no overlay passes `--enable-prompt-tokens-details` or `--enable-cache-report`.

## For maintainers

<details>
<summary>llm-d's nightly lanes, and how each maps to a profile</summary>


The overlay is `modelserver/<ACCELERATOR_TYPE>/<MODEL_SERVER>/<CONNECTOR>/<VARIANT>/<INFRA_PROVIDER>/`. Each workflow passes `connector` (`lmcache-connector` or `native`) and `offloading_target` (`cpu` or `fs`) to llm-d-infra's reusable workflow; `guide.yaml` says the nightlies override `CONNECTOR` and `VARIANT`, so `offloading_target` is read as `VARIANT` (inferred: the reusable workflow is not in the llm-d repository). The connector and the tier never change the model, window or parsers, so fifteen lanes (each `gateway_class: epponly`, so standalone) deploy five configurations that differ to a client.

| CI lane | Overlay | Profile | Identity flags |
| --- | --- | --- | --- |
| `cks-cpu-gpu-vllm-lmcache` | `gpu/vllm/lmcache-connector/cpu/base` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=base --identity flavor=lmcache-connector/cpu` |
| `cks-cpu-gpu-vllm-native` | `gpu/vllm/native/cpu/base` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=base --identity flavor=native/cpu` |
| `gke-cpu-gpu-vllm-lmcache` | `gpu/vllm/lmcache-connector/cpu/gke` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=lmcache-connector/cpu` |
| `gke-cpu-gpu-vllm-native` | `gpu/vllm/native/cpu/gke` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=native/cpu` |
| `ibm-cpu-gpu-vllm-lmcache` | `gpu/vllm/lmcache-connector/cpu/ocp` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=ocp --identity flavor=lmcache-connector/cpu` |
| `ibm-cpu-gpu-vllm-native` | `gpu/vllm/native/cpu/ocp` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=ocp --identity flavor=native/cpu` |
| `amd-cpu-rocm-vllm-lmcache` | `amd/vllm/lmcache-connector/cpu/amd-ci` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=amd-ci --identity flavor=lmcache-connector/cpu` |
| `amd-cpu-rocm-vllm-native` | `amd/vllm/native/cpu/amd-ci` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=amd-ci --identity flavor=native/cpu` |
| `amd-fs-rocm-vllm-lmcache` | `amd/vllm/lmcache-connector/fs/amd-ci` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=amd-ci --identity flavor=lmcache-connector/fs` |
| `amd-fs-rocm-vllm-native` | `amd/vllm/native/fs/amd-ci` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=amd-ci --identity flavor=native/fs` |
| `gke-cpu-tpu-vllm-native` | `tpu/v5p/vllm/native/cpu` (no provider; includes `base`) | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=tpu/v5p --identity provider=base --identity flavor=native/cpu` |
| `intel-acc-xpu-vllm-native` | `xpu/vllm/native/cpu/base` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=xpu --identity provider=base --identity flavor=native/cpu` |
| `intel-acc-xpu-vllm-lmcache` | `xpu/vllm/lmcache-connector/cpu/base` | `vllm-qwen3-8b-ctx20k-inline-standalone` | `--identity accelerator=xpu --identity provider=base --identity flavor=lmcache-connector/cpu` |
| `gke-cpu-gpu-sglang-native` | `gpu/sglang/native/cpu/gke` | `sglang-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=native/cpu` |
| `gke-cpu-tpu-sglang-native` | `tpu/v5p/sglang/native/cpu` (no provider; includes `base`) | `sglang-qwen3-32b-ctx16k-parsers-standalone` | `--identity accelerator=tpu/v5p --identity provider=base --identity flavor=native/cpu` |

`accelerator` is the lane's `ACCELERATOR_TYPE` and `provider` the `INFRA_PROVIDER` folder it renders (`base` for the two TPU lanes, which set none). `flavor` is `<CONNECTOR>/<VARIANT>`, the workflow's `connector` and `offloading_target` in overlay-path order, and every lane passes it, as [../README.md](../README.md) sets out. Lanes that would otherwise share a profile, accelerator and provider need it to stay apart: the six NVIDIA vLLM lanes (two connectors per provider) and the four AMD lanes (one provider, two connectors by two tiers).

</details>

<details>
<summary>Where llm-d's own sources disagree</summary>

- **Unlisted providers.** The four AMD lanes deploy `amd-ci`, which is among `guide.yaml`'s `INFRA_PROVIDER` values but not in the `amd` providers (`base` only). The two `ibm-*` lanes deploy `ocp`, which is in neither the `INFRA_PROVIDER` values (`base`, `gke`, `amd-ci`) nor the `gpu` providers.
- **Providers of the no-provider lanes.** The two TPU lanes pass an empty provider and apply `base`; `guide.yaml` lists only `gke` for `tpu/v5p`.
- **The XPU support row names one model for two.** `guide.yaml` gives `Qwen/Qwen3-32B` for `xpu`, and its notes and `MODEL` comment say the `lmcache-connector` overlay serves `Qwen/Qwen3-8B` (rendered: `--max-model-len 20480`, one XPU). The guide's own `model` check fails a run that leaves `MODEL` at the default.
- **Router CPU-tier capacity is sized for one overlay.** `router/single-host/tiered-prefix-cache-cpu.values.yaml` sets `lruCapacityPerServer: 19200`, sized for the NVIDIA native overlay's 300 GiB CPU tier; the AMD and XPU native overlays give 100 GiB and the LMCache overlays 100 GB (20 GB on XPU), and every lane uses the same router values. This affects routing quality, not what a client sees.

</details>
