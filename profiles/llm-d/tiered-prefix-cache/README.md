# tiered-prefix-cache

Profiles for llm-d's [tiered-prefix-cache](https://github.com/llm-d/llm-d/tree/main/guides/tiered-prefix-cache) guide. Engines offload evicted KV-cache blocks to CPU RAM (and, with the `fs` variant, a shared filesystem) and load them back instead of recomputing them; the router scores pods on both tiers. Written from the shipped configuration (each lane's overlay rendered with `kubectl kustomize`) and the API definitions. The layout and provenance tags are in the [profile skill](../../../.agents/skills/writing-an-infra-profile/SKILL.md#how-profilesllm-d-is-built), and the shared router claims in [../optimized-baseline/parts/common.yaml](../optimized-baseline/parts/common.yaml); this guide's claims and where each lands are in [parts/common.yaml](parts/common.yaml).

## Lanes

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

```sh
export LLMDV_EPP_URL=<the EPP's URL>
llmdv verify --profile profiles/llm-d/tiered-prefix-cache/<profile>.yaml <identity flags>
```

## What `llmdv verify` selects

This applies to every runnable profile in this folder: `sglang-qwen3-32b-ctx16k-parsers-standalone.yaml`, `sglang-qwen3-32b-parsers-standalone.yaml`, `vllm-qwen3-32b-inline-standalone.yaml`, `vllm-qwen3-32b-parsers-standalone.yaml`, `vllm-qwen3-8b-ctx20k-inline-standalone.yaml`.

**Default.** A `plan`, `verify` or `job` with no scope flag runs the `default` test set. It is defined once, in [`parts/common.yaml`](parts/common.yaml), and every profile above inherits it unchanged; none redefines it. There is no other profile-defined set.

**The `default` set's case-ID patterns, by the suite they select from** (11 suites, each confirmed by `llmdv plan`). A pattern ending in `.*` takes every case of its suite; any other names one case, or one variant in brackets. The rest of each suite is not selected:

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

**Named sets:** `default` (above) and the built-in `smoke`, which every profile has ([its cases](../../README.md#what-a-verification-command-selects)). `--all-test-sets` combines the profile-defined sets only, so here it selects exactly `default`; `smoke` is never included in it.

**What each scope selects**, as `llmdv plan` counts it offline. Counts are case instances (one per ingress and variant). *Run* means the profile's declarations, capabilities and fleet policy let the case run; a case can still skip during the run (throttled, `requires_instrumentation`, or a declaration it reads). *Skip* names the skip classes:

| Profile | no flag, or `--test-set default`, or `--all-test-sets` | `--test-set smoke` | `--full-suite` (every case in the library) |
| --- | --- | --- | --- |
| `sglang-qwen3-32b-ctx16k-parsers-standalone.yaml` | 88 selected, 82 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1082 selected, 911 run, 171 skip (171 undeclared) |
| `sglang-qwen3-32b-parsers-standalone.yaml` | 88 selected, 82 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1082 selected, 911 run, 171 skip (171 undeclared) |
| `vllm-qwen3-32b-inline-standalone.yaml` | 88 selected, 76 run, 12 skip (12 undeclared) | 9 selected, 9 run | 1082 selected, 677 run, 405 skip (405 undeclared) |
| `vllm-qwen3-32b-parsers-standalone.yaml` | 88 selected, 82 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1082 selected, 911 run, 171 skip (171 undeclared) |
| `vllm-qwen3-8b-ctx20k-inline-standalone.yaml` | 88 selected, 76 run, 12 skip (12 undeclared) | 9 selected, 9 run | 1082 selected, 677 run, 405 skip (405 undeclared) |

Other filters (`-k`, `-m`, `--surface`, `--client`, `--ingress`) only narrow the chosen scope, and a filter that leaves no case exits 2. `--identity` flags label the run and never change what it selects.

To inspect the definitions, the cases and every skip reason, offline:

```sh
P=profiles/llm-d/tiered-prefix-cache/sglang-qwen3-32b-ctx16k-parsers-standalone.yaml
llmdv profile show $P --test-sets        # every set and its patterns
llmdv plan --profile $P                  # the default: each case, and why one skips
llmdv plan --profile $P --test-set smoke
llmdv plan --profile $P --full-suite
llmdv plan --profile $P --json           # the same, as JSON
```
## Not covered

- **Configurations no lane runs:** MooncakeStore, the NVIDIA and XPU `fs` variants, TPU v6e and v7 (including multi-host and the TPU v7 `fs` tier), SGLang `fs`; gateway mode.
- **Everything the offload does**: offload and load-back counters, the native connector's offload gate, the shared filesystem tier (a prefix primed on one pod hits on another) and the fs lanes' PVC. They need per-pod URLs, engine metrics or cluster objects a committed profile does not carry, and no response field shows a tier hit. A run of these profiles checks that the stack serves correctly with offloading on, not that offloading works.
- **`cache_reuse`**: no overlay passes `--enable-prompt-tokens-details` or `--enable-cache-report`.

## Upstream notes

- **Unlisted providers.** The four AMD lanes deploy `amd-ci`, which is among `guide.yaml`'s `INFRA_PROVIDER` values but not in the `amd` providers (`base` only). The two `ibm-*` lanes deploy `ocp`, which is in neither the `INFRA_PROVIDER` values (`base`, `gke`, `amd-ci`) nor the `gpu` providers.
- **Providers of the no-provider lanes.** The two TPU lanes pass an empty provider and apply `base`; `guide.yaml` lists only `gke` for `tpu/v5p`.
- **The XPU support row names one model for two.** `guide.yaml` gives `Qwen/Qwen3-32B` for `xpu`, and its notes and `MODEL` comment say the `lmcache-connector` overlay serves `Qwen/Qwen3-8B` (rendered: `--max-model-len 20480`, one XPU). The guide's own `model` check fails a run that leaves `MODEL` at the default.
- **Router CPU-tier capacity is sized for one overlay.** `router/single-host/tiered-prefix-cache-cpu.values.yaml` sets `lruCapacityPerServer: 19200`, sized for the NVIDIA native overlay's 300 GiB CPU tier; the AMD and XPU native overlays give 100 GiB and the LMCache overlays 100 GB (20 GB on XPU), and every lane uses the same router values. This affects routing quality, not what a client sees.
