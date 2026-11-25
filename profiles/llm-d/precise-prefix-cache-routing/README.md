# precise-prefix-cache-routing

Profiles for llm-d's [precise-prefix-cache-routing](https://github.com/llm-d/llm-d/tree/main/guides/precise-prefix-cache-routing) guide at llm-d `9ca8ff0`. The router tokenizes every prompt by calling vLLM's render routes on the pool's own pods, looks the token blocks up in an index fed by each engine's KV-cache events, and routes to the pod that holds the prefix. Written from the shipped configuration (each lane's overlay rendered with `kubectl kustomize`) and the API definitions. The layout and provenance tags are in the [profile skill](../../../.agents/skills/writing-an-infra-profile/SKILL.md#how-profilesllm-d-is-built), and the shared router claims in [../optimized-baseline/parts/common.yaml](../optimized-baseline/parts/common.yaml); this guide's claims and where each lands are in [parts/common.yaml](parts/common.yaml).

## Lanes

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

```sh
export LLMDV_EPP_URL=<the EPP's URL>
llmdv verify --profile profiles/llm-d/precise-prefix-cache-routing/<profile>.yaml <identity flags>
```

## What `llmdv verify` selects

This applies to every runnable profile in this folder: `vllm-qwen3-0.6b-inline-standalone.yaml`, `vllm-qwen3-32b-ctx4k-inline-standalone.yaml`, `vllm-qwen3-32b-inline-standalone.yaml`, `vllm-qwen3-32b-parsers-standalone.yaml`.

**Default.** A `plan`, `verify` or `job` with no scope flag runs the `default` test set. It is defined once, in [`parts/common.yaml`](parts/common.yaml), and every profile above inherits it unchanged; none redefines it. There is no other profile-defined set.

**The `default` set's case-ID patterns, by the suite they select from** (13 suites, each confirmed by `llmdv plan`). A pattern ending in `.*` takes every case of its suite; any other names one case, or one variant in brackets. The rest of each suite is not selected:

| Suite | Patterns |
| --- | --- |
| `contract` | `contract.*` |
| `discovery` | `discovery.models_lists_declared_model`, `discovery.unknown_model_no_fallback`, `discovery.models_parses_with_openai_sdk` |
| `openai_chat` | `openai_chat.basic_non_streaming`, `openai_chat.streaming_sse`, `openai_chat.stop_reason`, `openai_chat.configured_reasoning` |
| `openai_completions` | `openai_completions.basic_non_streaming`, `openai_completions.streaming_matches_json`, `openai_completions.stop_reason` |
| `anthropic_messages` | `anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse`, `anthropic_messages.count_tokens`, `anthropic_messages.token_count_matches_usage`, `anthropic_messages.stream_matches_message` |
| `openai_responses` | `openai_responses.basic_non_streaming`, `openai_responses.streaming_sse`, `openai_responses.input_tokens` |
| `streaming` | `streaming.sse_framing` |
| `unhappy` | `unhappy.unknown_path`, `unhappy.malformed_json`, `unhappy.missing_model`, `unhappy.wrong_method`, `unhappy.error_envelope_fields`, `unhappy.context_overflow`, `unhappy.token_limit_over_context` |
| `transport` | `transport.connection`, `transport.health_status` |
| `tools` | `tools.auto_call`, `tools.auto_stream` |
| `long_context` | `long_context.*` |
| `render` | `render.*` |
| `evolution` | `evolution.prompt_pin`, `evolution.prompt_pin_tools` |

**Named sets:** `default` (above) and the built-in `smoke`, which every profile has ([its cases](../../README.md#what-a-verification-command-selects)). `--all-test-sets` combines the profile-defined sets only, so here it selects exactly `default`; `smoke` is never included in it.

**What each scope selects**, as `llmdv plan` counts it offline. Counts are case instances (one per ingress and variant). *Run* means the profile's declarations, capabilities and fleet policy let the case run; a case can still skip during the run (throttled, `requires_instrumentation`, or a declaration it reads). *Skip* names the skip classes:

| Profile | no flag, or `--test-set default`, or `--all-test-sets` | `--test-set smoke` | `--full-suite` (every case in the library) |
| --- | --- | --- | --- |
| `vllm-qwen3-0.6b-inline-standalone.yaml` | 107 selected, 91 run, 16 skip (16 undeclared) | 9 selected, 9 run | 1082 selected, 691 run, 391 skip (391 undeclared) |
| `vllm-qwen3-32b-ctx4k-inline-standalone.yaml` | 107 selected, 81 run, 26 skip (26 undeclared) | 9 selected, 9 run | 1082 selected, 677 run, 405 skip (405 undeclared) |
| `vllm-qwen3-32b-inline-standalone.yaml` | 107 selected, 91 run, 16 skip (16 undeclared) | 9 selected, 9 run | 1082 selected, 691 run, 391 skip (391 undeclared) |
| `vllm-qwen3-32b-parsers-standalone.yaml` | 107 selected, 100 run, 7 skip (7 undeclared) | 9 selected, 9 run | 1082 selected, 929 run, 153 skip (153 undeclared) |

Other filters (`-k`, `-m`, `--surface`, `--client`, `--ingress`) only narrow the chosen scope, and a filter that leaves no case exits 2. `--identity` flags label the run and never change what it selects.

To inspect the definitions, the cases and every skip reason, offline:

```sh
P=profiles/llm-d/precise-prefix-cache-routing/vllm-qwen3-0.6b-inline-standalone.yaml
llmdv profile show $P --test-sets        # every set and its patterns
llmdv plan --profile $P                  # the default: each case, and why one skips
llmdv plan --profile $P --test-set smoke
llmdv plan --profile $P --full-suite
llmdv plan --profile $P --json           # the same, as JSON
```
## Not covered

- **Configurations no lane runs:** SGLang on NVIDIA GPU and Intel XPU (with their render pools), TPU v6e and v7, and CPU, all `community` in `guide.yaml`; gateway mode.
- **Render surfaces on TPU v5p.** The GPU, AMD and XPU combinations declare vLLM's four render surfaces (`chat_render`, `completions_render`, `messages_render`, `responses_render`) on the `epp` ingress, with `prefix_stability` on `chat_render` and `messages_render`; their engines pass `--enable-scale-out`, which vLLM v0.30 and later require to register the routes (`vllm@v0.31.0:vllm/entrypoints/scale_out/factories.py`, `register_scale_out_api_routers`). The TPU v5p combination declares none, since its engine runs without the flag (see Upstream notes). No combination declares `render_pins` yet: an undeclared pin skips, and they are added from the first run.
- **The guide's per-pod render check, router render metrics, per-pod prefix-cache counters and KV-event indexing**: they need per-pod URLs or cluster access a committed profile does not carry. Without the router render metrics, a router whose renders all fail still answers 200, so a green run does not show that precise routing is on.
- **Prefix affinity** (`routing.prefix_affinity`, #27) and `cache_reuse`: no overlay passes `--enable-prompt-tokens-details`, so no response carries `cached_tokens`.

## Upstream notes

- **TPU v5p and `--enable-scale-out`.** `modelserver/tpu/v5p/vllm/base/patch-vllm.yaml` is the only lane overlay without `--enable-scale-out`, and it runs `vllm-tpu:v0.31.0` (from `recipes/modelserver/components/images/tpu-vllm/release`). vLLM v0.30 and later register `/v1/*/render` only with that flag (`vllm@v0.31.0:vllm/entrypoints/scale_out/factories.py`), so on this lane every render call the router makes answers 404 and it routes without cache information, while requests still answer 200. The README ("Render (Tokenizer) Endpoints", the support table) and `guide.yaml` say the TPU overlays use vLLM 0.29, which serves the routes without the flag, and the router values' comment on `messagesRenderMode` repeats it; the only vLLM 0.29 image in llm-d `9ca8ff0` is `vllm-torchtpu:v0.29.0` (`recipes/modelserver/components/images/torch-tpu-vllm/release`), which this guide's `tpu/v6` and `tpu/v7` overlays use and no lane runs.
- **Intel XPU model name.** The router's `token-producer` `modelName` is `Qwen/Qwen3-32B` (`router/precise-prefix-cache-routing.values.yaml`), while the XPU overlay serves `Qwen/Qwen3-0.6B`. The README ("Model" note under the support table) asks the operator to edit it by hand; nothing in llm-d `9ca8ff0` changes it for the lane. Unedited, every render call names a model the pods do not serve and is refused. The README ("Verify that the router renders") says requests still answer 200 when the router's renders fail, routed without cache information; if so, a client sees nothing wrong and `llmdv`'s cases pass while precise routing is off. If the router instead fails the request, every case through the EPP fails on this lane. The guide's own `router_render` check tells the two apart; `llmdv` cannot without cluster access.
- **`ocp` provider.** The `ibm-acc-gpu-vllm-x` lane deploys `gpu/vllm/ocp`, but `ocp` is not among `guide.yaml`'s `INFRA_PROVIDER` values (`base`, `gke`) or the `gpu` providers.
- **Providers of the no-provider lanes.** `guide.yaml` lists `gke` as the only provider for `tpu/v5p` and `base` for `amd` and `xpu`; those lanes pass an empty provider and apply `base`.
- **vLLM version in the docs.** The README's render table says vLLM 0.30 for NVIDIA, AMD and Intel; the GPU and AMD overlays render `v0.31.0`.
