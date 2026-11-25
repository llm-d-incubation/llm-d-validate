# pd-disaggregation

Profiles for llm-d's [pd-disaggregation](https://github.com/llm-d/llm-d/tree/main/guides/pd-disaggregation) guide. Every lane deploys the guide in standalone mode, so every profile has one ingress, the router (`<GUIDE_NAME>-epp`, port 80). The EPP schedules each request onto a prefill pod and a decode pod; the decode pod's routing sidecar runs the prefill, then the decode, and returns one response. In every lane, prefill and decode carry the same model id, window and parser flags, so a client sees one model.

Each combination's header names its lanes and overlays; `parts/common.yaml` maps the guide's claims to cases or to what is not covered.

## Lanes

One profile per configuration a client can tell apart. Each lane is one of llm-d's `nightly-e2e-pd-disaggregation-<lane>` workflows; the overlay is `modelserver/<ACCELERATOR_TYPE>/<MODEL_SERVER>/<INFRA_PROVIDER>/`, rendered locally with `kubectl kustomize`.

| Lane | Overlay | Engine, model | Profile | Identity flags |
| --- | --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `gpu/vllm/gke/base` | vLLM v0.31.0, gpt-oss-120b, parsers | `vllm-gpt-oss-120b-parsers-standalone.yaml` | `--identity accelerator=gpu --identity provider=gke/base` |
| `cks-acc-gpu-vllm-x` | `gpu/vllm/coreweave` | vLLM v0.31.0, gpt-oss-120b, parsers | `vllm-gpt-oss-120b-parsers-standalone.yaml` | `--identity accelerator=gpu --identity provider=coreweave` |
| `ibm-acc-gpu-vllm-x` | `gpu/vllm/ocp` | vLLM v0.31.0, gpt-oss-120b, parsers | `vllm-gpt-oss-120b-parsers-standalone.yaml` | `--identity accelerator=gpu --identity provider=ocp` |
| `amd-ci-acc-rocm-vllm-nixl` | `amd/vllm/nixl/amd-ci` | vLLM v0.31.0 (ROCm), gpt-oss-120b, parsers | `vllm-gpt-oss-120b-parsers-standalone.yaml` | `--identity accelerator=amd --identity provider=nixl/amd-ci` |
| `gke-acc-gpu-sglang-x` | `gpu/sglang/gke` | SGLang v0.5.22, gpt-oss-120b, no parsers | `sglang-gpt-oss-120b-inline-standalone.yaml` | `--identity accelerator=gpu --identity provider=gke` |
| `amd-ci-acc-rocm-vllm-moriio` | `amd/vllm/moriio/amd-ci-1p1d-tp8` | vLLM v0.32.0-rc.2 (llm-d ROCm build), Qwen3-32B, no parsers | `vllm-qwen3-32b-inline-standalone.yaml` | `--identity accelerator=amd --identity provider=moriio/amd-ci-1p1d-tp8` |
| `gke-acc-tpu-vllm-x` | `tpu/v5p/vllm` (no provider: the folder's own kustomization, which includes `base`) | vllm-tpu v0.31.0, Qwen3-32B at 9216 tokens, parsers | `vllm-qwen3-32b-ctx9k-parsers-standalone.yaml` | `--identity accelerator=tpu/v5p --identity provider=base` |
| `intel-acc-xpu-vllm-x` | `xpu/vllm` (no provider: the folder's own kustomization, which includes `base`) | llm-d-xpu v1.0.0, Qwen3-0.6B at 32000 tokens, no parsers | `vllm-qwen3-0.6b-ctx32000-inline-standalone.yaml` | `--identity accelerator=xpu --identity provider=base` |

The workflows pass `infra_provider` and `connector` separately to llm-d-infra's reusable workflow (`amd-ci` with `nixl`, `amd-ci-1p1d-tp8` with `moriio`); the overlay paths above are the `nixl/amd-ci` and `moriio/amd-ci-1p1d-tp8` values guide.yaml lists for `INFRA_PROVIDER`, and the provider identity uses them so the AMD lanes never share an identity. Four lanes share the gpt-oss-120b vLLM profile: accelerator and provider tell their runs apart, and no `flavor` is needed.

To test against the deployed guide:

```sh
export LLMDV_EPP_URL=<the EPP's URL>
llmdv verify --profile profiles/llm-d/pd-disaggregation/<profile> <identity flags from the table>
```

## What `llmdv verify` selects

This applies to every runnable profile in this folder: `sglang-gpt-oss-120b-inline-standalone.yaml`, `vllm-gpt-oss-120b-parsers-standalone.yaml`, `vllm-qwen3-0.6b-ctx32000-inline-standalone.yaml`, `vllm-qwen3-32b-ctx9k-parsers-standalone.yaml`, `vllm-qwen3-32b-inline-standalone.yaml`.

**Default.** A `plan`, `verify` or `job` with no scope flag runs the `default` test set. It is defined once, in [`parts/common.yaml`](parts/common.yaml), and every profile above inherits it unchanged; none redefines it. There is no other profile-defined set.

**The `default` set's case-ID patterns, by the suite they select from** (11 suites, each confirmed by `llmdv plan`). A pattern ending in `.*` takes every case of its suite; any other names one case, or one variant in brackets. The rest of each suite is not selected:

| Suite | Patterns |
| --- | --- |
| `contract` | `contract.*` |
| `discovery` | `discovery.models_lists_declared_model`, `discovery.unknown_model_no_fallback`, `discovery.models_parses_with_openai_sdk` |
| `openai_chat` | `openai_chat.basic_non_streaming`, `openai_chat.streaming_sse`, `openai_chat.stop_reason`, `openai_chat.configured_reasoning`, `openai_chat.streaming_include_usage`, `openai_chat.continuous_usage` |
| `openai_completions` | `openai_completions.basic_non_streaming`, `openai_completions.streaming_matches_json`, `openai_completions.stop_reason`, `openai_completions.prompt_forms`, `openai_completions.multiple_choices` |
| `anthropic_messages` | `anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse` |
| `openai_responses` | `openai_responses.basic_non_streaming`, `openai_responses.streaming_sse` |
| `streaming` | `streaming.sse_framing`, `streaming.*` |
| `unhappy` | `unhappy.unknown_path`, `unhappy.malformed_json`, `unhappy.missing_model`, `unhappy.wrong_method`, `unhappy.error_envelope_fields`, `unhappy.context_overflow`, `unhappy.token_limit_over_context`, `unhappy.invalid_token_limit`, `unhappy.streaming_validation`, `unhappy.wrong_field_type` |
| `transport` | `transport.connection`, `transport.health_status` |
| `tools` | `tools.auto_call`, `tools.auto_stream` |
| `long_context` | `long_context.*` |

**Named sets:** `default` (above) and the built-in `smoke`, which every profile has ([its cases](../../README.md#what-a-verification-command-selects)). `--all-test-sets` combines the profile-defined sets only, so here it selects exactly `default`; `smoke` is never included in it.

**What each scope selects**, as `llmdv plan` counts it offline. Counts are case instances (one per ingress and variant). *Run* means the profile's declarations, capabilities and fleet policy let the case run; a case can still skip during the run (throttled, `requires_instrumentation`, or a declaration it reads). *Skip* names the skip classes:

| Profile | no flag, or `--test-set default`, or `--all-test-sets` | `--test-set smoke` | `--full-suite` (every case in the library) |
| --- | --- | --- | --- |
| `sglang-gpt-oss-120b-inline-standalone.yaml` | 121 selected, 107 run, 14 skip (14 undeclared) | 9 selected, 9 run | 1082 selected, 658 run, 424 skip (424 undeclared) |
| `vllm-gpt-oss-120b-parsers-standalone.yaml` | 121 selected, 115 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1082 selected, 911 run, 171 skip (171 undeclared) |
| `vllm-qwen3-0.6b-ctx32000-inline-standalone.yaml` | 121 selected, 109 run, 12 skip (12 undeclared) | 9 selected, 9 run | 1082 selected, 677 run, 405 skip (405 undeclared) |
| `vllm-qwen3-32b-ctx9k-parsers-standalone.yaml` | 121 selected, 115 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1082 selected, 911 run, 171 skip (171 undeclared) |
| `vllm-qwen3-32b-inline-standalone.yaml` | 121 selected, 109 run, 12 skip (12 undeclared) | 9 selected, 9 run | 1082 selected, 677 run, 405 skip (405 undeclared) |

Other filters (`-k`, `-m`, `--surface`, `--client`, `--ingress`) only narrow the chosen scope, and a filter that leaves no case exits 2. `--identity` flags label the run and never change what it selects.

To inspect the definitions, the cases and every skip reason, offline:

```sh
P=profiles/llm-d/pd-disaggregation/sglang-gpt-oss-120b-inline-standalone.yaml
llmdv profile show $P --test-sets        # every set and its patterns
llmdv plan --profile $P                  # the default: each case, and why one skips
llmdv plan --profile $P --test-set smoke
llmdv plan --profile $P --full-suite
llmdv plan --profile $P --json           # the same, as JSON
```
## Not covered, and why

- **That requests are actually split** (prefill counters grow on prefill pods, `vllm:external_prefix_cache_hits_total` on decode pods, the router's `llm_d_epp_disagg_decision_total`): no response says which pod prefilled it, and a decode sidecar can serve a request on its own. These need per-role pod and EPP metrics read before and after, which need replica sets and cluster reads a committed profile does not carry (#24).
- **Prefill prefix affinity and decode active-request balancing**: needs `routing.prefix_affinity` against the prefill role and per-pod counter deltas (#27).
- **Prompt cache reuse** (`cache_reuse`): no lane's engine reports `cached_tokens`; only the community `tpu/v7` overlays pass `--enable-prompt-tokens-details` or `--enable-cache-report`.
- **Latency, throughput, KV-transfer stalls**: a benchmark or load mode (#23), not `llmdv`.
- **Configurations no lane runs**: every `community` cell in guide.yaml, and the validated cells' other providers (`gpu` base, `gke/a4x`, `gke/a4xmax`, `aws`, `cks-mooncake`; `amd` `nixl/base`, `nixl/oci`, `nixl/tensorwave`, `moriio/base`, `moriio/amd-ci`; `xpu` `rdma`). Gateway mode: no lane runs it.

## Upstream notes

Facts found while writing these profiles, for llm-d to decide on:

- `ibm-acc-gpu-vllm-x` deploys `gpu/vllm/ocp`, but `ocp` is not among the gpu `providers` in `guides/pd-disaggregation/guide.yaml`, nor among the `INFRA_PROVIDER` values there.
- `amd-ci-acc-rocm-vllm-moriio` deploys `amd/vllm/moriio/amd-ci-1p1d-tp8`, which serves `Qwen/Qwen3-32B` with no reasoning or tool parser, while guide.yaml's `amd` cell advertises `openai/gpt-oss-120b` (only the `MODEL` comment mentions the MoRI-IO model). The NIXL lane on the same accelerator serves gpt-oss-120b with both parsers, so the two AMD lanes present different models and API shapes under one validated cell.
- The MoRI-IO overlay's routing sidecar takes `--secure-proxy=false` where every other lane's takes `--secure-serving=false`; it also passes `--kv-connector=nixlv2` beside its MoRI-IO flags (`guides/pd-disaggregation/modelserver/amd/vllm/moriio/`).
- `gke-acc-gpu-sglang-x` serves gpt-oss-120b on SGLang with no `--reasoning-parser` and no `--tool-call-parser`, while the vLLM lanes of the same cell pass `openai_gptoss` and `openai`: a client of the SGLang lane gets harmony analysis text in `content` and no tool calls.
- `gke-acc-gpu-vllm-x` passes `infra_provider` `gke/base`, which is not among guide.yaml's `INFRA_PROVIDER` values (`gke` is; `gpu/vllm/gke/kustomization.yaml` includes `./base`, and the two render the same engine arguments). Likewise the AMD lanes' `nixl/amd-ci` and `moriio/amd-ci-1p1d-tp8` are `INFRA_PROVIDER` values but not among the `amd` cell's `providers`.
- `gke-acc-tpu-vllm-x` and `intel-acc-xpu-vllm-x` set no `infra_provider`, so they apply the accelerator folder's own kustomization; guide.yaml lists only `gke` for `tpu/v5p` (the folder includes `base`, which renders the same engine arguments).
