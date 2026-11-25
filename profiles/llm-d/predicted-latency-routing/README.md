# predicted-latency-routing

Profiles for llm-d's [predicted-latency-routing](https://github.com/llm-d/llm-d/tree/main/guides/predicted-latency-routing) guide. The router scores pods on latency predicted by models it trains on its own completed requests; the predictor runs as sidecars in the EPP pod and is not on the client's path. Written from the shipped configuration (each lane's overlay rendered with `kubectl kustomize`) and the API definitions. The layout and provenance tags are in the [profile skill](../../../.agents/skills/writing-an-infra-profile/SKILL.md#how-profilesllm-d-is-built), and the shared router claims in [../optimized-baseline/parts/common.yaml](../optimized-baseline/parts/common.yaml); this guide's claims and where each lands are in [parts/common.yaml](parts/common.yaml).

## Lanes

Five nightly lanes (`nightly-e2e-predicted-latency-routing-<lane>.yaml`, each `gateway_class: epponly`, so standalone, and each with the routing-only router values, `SLO_AWARE=false`) deploy two configurations that differ to a client.

| CI lane | Overlay (`ACCELERATOR_TYPE/MODEL_SERVER/INFRA_PROVIDER`) | Profile | Identity flags |
| --- | --- | --- | --- |
| `cks-acc-gpu-vllm-x` | `gpu/vllm/base` | `vllm-qwen3-32b-ctx128k-parsers-standalone` | `--identity accelerator=gpu --identity provider=base` |
| `gke-acc-gpu-vllm-x` | `gpu/vllm/gke` | `vllm-qwen3-32b-ctx128k-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `ibm-acc-gpu-vllm-x` | `gpu/vllm/ocp` | `vllm-qwen3-32b-ctx128k-parsers-standalone` | `--identity accelerator=gpu --identity provider=ocp` |
| `amd-ci-acc-rocm-vllm-x` | `amd/vllm` (no provider; includes `base`) | `vllm-qwen3-32b-ctx128k-parsers-standalone` | `--identity accelerator=amd --identity provider=base` |
| `intel-acc-xpu-vllm-x` | `xpu/vllm` (no provider; includes `base`, which is optimized-baseline's `xpu/vllm`) | `vllm-qwen3-0.6b-inline-standalone` | `--identity accelerator=xpu --identity provider=base` |

A lane that sets no provider applies the folder's own kustomization, which includes only `base`, so it passes `provider=base`. No two lanes share both a profile and an accelerator and provider, so none passes `flavor`. The NVIDIA and AMD lanes run the same model, window and parsers, so their runs are worth comparing with `llmdv compare`.

```sh
export LLMDV_EPP_URL=<the EPP's URL>
llmdv verify --profile profiles/llm-d/predicted-latency-routing/<profile>.yaml <identity flags>
```

## What `llmdv verify` selects

This applies to every runnable profile in this folder: `vllm-qwen3-0.6b-inline-standalone.yaml`, `vllm-qwen3-32b-ctx128k-parsers-standalone.yaml`.

**Default.** A `plan`, `verify` or `job` with no scope flag runs the `default` test set. It is defined once, in [`parts/common.yaml`](parts/common.yaml), and every profile above inherits it unchanged; none redefines it. There is no other profile-defined set.

**The `default` set's case-ID patterns, by the suite they select from** (11 suites, each confirmed by `llmdv plan`). A pattern ending in `.*` takes every case of its suite; any other names one case, or one variant in brackets. The rest of each suite is not selected:

| Suite | Patterns |
| --- | --- |
| `contract` | `contract.*` |
| `discovery` | `discovery.models_lists_declared_model`, `discovery.unknown_model_no_fallback`, `discovery.models_parses_with_openai_sdk` |
| `openai_chat` | `openai_chat.basic_non_streaming`, `openai_chat.streaming_sse`, `openai_chat.stop_reason`, `openai_chat.configured_reasoning` |
| `openai_completions` | `openai_completions.basic_non_streaming`, `openai_completions.streaming_matches_json`, `openai_completions.stop_reason` |
| `anthropic_messages` | `anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse` |
| `openai_responses` | `openai_responses.basic_non_streaming`, `openai_responses.streaming_sse` |
| `streaming` | `streaming.sse_framing`, `streaming.*` |
| `unhappy` | `unhappy.unknown_path`, `unhappy.malformed_json`, `unhappy.missing_model`, `unhappy.wrong_method`, `unhappy.error_envelope_fields`, `unhappy.context_overflow`, `unhappy.token_limit_over_context` |
| `transport` | `transport.connection`, `transport.health_status` |
| `tools` | `tools.auto_call`, `tools.auto_stream` |
| `long_context` | `long_context.*` |

**Named sets:** `default` (above) and the built-in `smoke`, which every profile has ([its cases](../../README.md#what-a-verification-command-selects)). `--all-test-sets` combines the profile-defined sets only, so here it selects exactly `default`; `smoke` is never included in it.

**What each scope selects**, as `llmdv plan` counts it offline. Counts are case instances (one per ingress and variant). *Run* means the profile's declarations, capabilities and fleet policy let the case run; a case can still skip during the run (throttled, `requires_instrumentation`, or a declaration it reads). *Skip* names the skip classes:

| Profile | no flag, or `--test-set default`, or `--all-test-sets` | `--test-set smoke` | `--full-suite` (every case in the library) |
| --- | --- | --- | --- |
| `vllm-qwen3-0.6b-inline-standalone.yaml` | 103 selected, 91 run, 12 skip (12 undeclared) | 9 selected, 9 run | 1082 selected, 677 run, 405 skip (405 undeclared) |
| `vllm-qwen3-32b-ctx128k-parsers-standalone.yaml` | 103 selected, 97 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1082 selected, 911 run, 171 skip (171 undeclared) |

Other filters (`-k`, `-m`, `--surface`, `--client`, `--ingress`) only narrow the chosen scope, and a filter that leaves no case exits 2. `--identity` flags label the run and never change what it selects.

To inspect the definitions, the cases and every skip reason, offline:

```sh
P=profiles/llm-d/predicted-latency-routing/vllm-qwen3-0.6b-inline-standalone.yaml
llmdv profile show $P --test-sets        # every set and its patterns
llmdv plan --profile $P                  # the default: each case, and why one skips
llmdv plan --profile $P --test-set smoke
llmdv plan --profile $P --full-suite
llmdv plan --profile $P --json           # the same, as JSON
```
## Not covered

- **Configurations no lane runs:** SGLang on NVIDIA GPU, TPU v6e and v7 (all `community`); the SLO-aware router values (`router/predicted-latency-slo.values.yaml`, `skip_in: [ci]` in `guide.yaml`), the P/D and multimodal values; gateway mode.
- **The predictor itself**: training sample counts and `/status` on the EPP pod's sidecar ports, and the router's predicted-versus-actual TTFT and TPOT histograms (`MONITORING=true`, which no lane sets). They need cluster access a committed profile does not carry.
- **Latency claims and the traffic burst**: a benchmark or load mode (#23), not functional cases.
- **SLO headers with an effect**: no lane configures an admission plugin, so the topology part's `ignored` declarations stand.
- **`cache_reuse`**: no overlay passes `--enable-prompt-tokens-details`.

## Upstream notes

- **The model servers are optimized-baseline's.** Every overlay builds on `guides/optimized-baseline/modelserver/...` (the GPU and AMD bases patch it; the XPU base includes optimized-baseline's `xpu/vllm` unchanged), so the rendered Deployments are named `optimized-baseline-*` and the pods carry `llm-d.ai/guide=optimized-baseline`. The router values select them with `modelServers.matchLabels: llm-d.ai/guide: optimized-baseline`. These profiles depend on no label; a lane that adds a fleet block must select this guide's pods by that label, not by the guide's own name.
- **AMD adds parsers.** `modelserver/amd/vllm/base/patch-vllm.yaml` adds `--reasoning-parser=qwen3`, `--tool-call-parser=hermes` and `--enable-auto-tool-choice`, which optimized-baseline's AMD overlay does not pass, so AMD here looks like NVIDIA to a client.
- **`ocp` provider.** The `ibm-acc-gpu-vllm-x` lane deploys `gpu/vllm/ocp`, but `ocp` is not among `guide.yaml`'s `INFRA_PROVIDER` values (`base`, `gke`) or the `gpu` providers.
- **TPU provider notes.** The `tpu/v6` and `tpu/v7` notes in `guide.yaml` say "`INFRA_PROVIDER`: `base`, `gke`" while their `providers` list only `gke`. No lane runs them.
