# fast-model-actuation

Profiles for llm-d's [fast-model-actuation](https://github.com/llm-d/llm-d/tree/main/guides/fast-model-actuation) guide, written against llm-d `9ca8ff0`. See [../README.md](../README.md) for how to run a combination, and the [profile skill](../../../.agents/skills/writing-an-infra-profile/SKILL.md#how-profilesllm-d-is-built) for the layout and provenance tags.

## Lanes

The guide has no support table; `guide.yaml` offers `INFRA_PROVIDER` `ocp` or `gke`. Both nightly lanes stand the guide up with `kubectl apply -k platform/<INFRA_PROVIDER>`: the FMA InferenceServerConfig, LauncherConfig and LauncherPopulationPolicy, and the requester Deployment. The vLLM instance runs inside an FMA launcher pod with `--model Qwen/Qwen3-0.6B --enable-sleep-mode --gpu-memory-utilization 0.9` and no parsers; the EPP reaches it through the requester's TCP proxy on port 8000. The two providers change only how the launcher reaches the node's GPUs, so one combination covers both.

| CI lane (`nightly-e2e-fast-model-actuation-<lane>`) | What it applies | Profile | Identity flags |
| --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `platform/gke` | `vllm-qwen3-0.6b-inline-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `ibm-acc-gpu-vllm-x` | `platform/ocp` | `vllm-qwen3-0.6b-inline-standalone` | `--identity accelerator=gpu --identity provider=ocp` |

The combination reuses `../parts/models/qwen3-0.6b-inline.yaml` (the model id, the derived 40960-token window and the absence of parsers are the same as optimized-baseline's XPU overlay). The `modelserver/gpu/vllm/{base,gke,ocp}` overlays the benchmark harness renders are detection-only stubs (a `replicas: 0` Deployment that names the model) and are never applied.

The client calls the `<GUIDE_NAME>-epp` Service on port 80, in standalone mode. Run `llmdv` after the requester Deployment's rollout completes (guide.yaml deploy.modelserver) and before the guide's sleep/wake step, so both requesters are bound to awake instances.

## What `llmdv verify` selects

This applies to every runnable profile in this folder: `vllm-qwen3-0.6b-inline-standalone.yaml`.

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
| `transport` | `transport.connection`, `transport.health_status`, `transport.chunked_request`, `transport.body_edge`, `transport.long_header`, `transport.header_name_case`, `transport.bodiless_header`, `transport.content_type`, `transport.accept_event_stream`, `transport.response_encoding`, `transport.query_string`, `transport.path_variant`, `transport.expect_continue` |
| `tools` | `tools.auto_call`, `tools.auto_stream` |
| `long_context` | `long_context.*` |

**Named sets:** `default` (above) and the built-in `smoke`, which every profile has ([its cases](../../README.md#what-a-verification-command-selects)). `--all-test-sets` combines the profile-defined sets only, so here it selects exactly `default`; `smoke` is never included in it.

**What each scope selects**, as `llmdv plan` counts it offline. Counts are case instances (one per ingress and variant). *Run* means the profile's declarations, capabilities and fleet policy let the case run; a case can still skip during the run (throttled, `requires_instrumentation`, or a declaration it reads). *Skip* names the skip classes:

| Profile | no flag, or `--test-set default`, or `--all-test-sets` | `--test-set smoke` | `--full-suite` (every case in the library) |
| --- | --- | --- | --- |
| `vllm-qwen3-0.6b-inline-standalone.yaml` | 119 selected, 107 run, 12 skip (12 undeclared) | 9 selected, 9 run | 1082 selected, 683 run, 399 skip (399 undeclared) |

Other filters (`-k`, `-m`, `--surface`, `--client`, `--ingress`) only narrow the chosen scope, and a filter that leaves no case exits 2. `--identity` flags label the run and never change what it selects.

To inspect the definitions, the cases and every skip reason, offline:

```sh
P=profiles/llm-d/fast-model-actuation/vllm-qwen3-0.6b-inline-standalone.yaml
llmdv profile show $P --test-sets        # every set and its patterns
llmdv plan --profile $P                  # the default: each case, and why one skips
llmdv plan --profile $P --test-set smoke
llmdv plan --profile $P --full-suite
llmdv plan --profile $P --json           # the same, as JSON
```
## What the profile checks that is particular to this guide

The requester's TCP proxy sits between the EPP and vLLM on every request. It should pass vLLM's responses through byte for byte, so the profile declares vLLM's behaviour; what the proxy does when its vLLM instance refuses or drops a connection, and any error it answers itself, have not been measured. A failure in the error-envelope or disconnect cases that the optimized-baseline vLLM lanes do not show points at the proxy.

## Not covered, and why

- **Sleep and wake** (`verify.tests.sleep_wake`: scaling the requesters to zero puts vLLM to sleep, scaling back up wakes it): `llmdv` never scales a deployment, and the sleeping state is a pod label, which is cluster state.
- **Hot and warm start times**: timing is a benchmark's measurement, not `llmdv`'s.
- **The FMA controllers' lifecycle** (binding, LRU eviction of sleeping instances, launcher population): cluster state.
- **The cache suite**: the vLLM options do not include `--enable-prompt-tokens-details`, and the router has no prefix-cache plugin, so `cache_reuse` is not declared (see `parts/common.yaml`).
- **Gateway mode** and the other `GATEWAY_CLASS` values: no lane runs them.
- **fast-model-actuation-keda** (`fast-model-actuation/fast-model-actuation-keda/`, lanes `nightly-e2e-fast-model-actuation-keda-{gke,ibm}-acc-gpu-vllm-x`): deferred. It scales the requesters from zero on EPP flow-control metrics, so the first request waits for the scale-up (the EPP holds it up to `noEndpointRequestTTL: 300s`, `router/fast-model-actuation-keda.values.yaml`), and the pool returns to zero after about 60 s idle (`cooldownPeriod` 30 s plus the HPA's 30 s stabilization, `keda/base/kustomization.yaml`). A profile cannot express that without loosening every latency check, and a run with gaps between cases would keep paying the cold start.

## Upstream notes

- The FMA launcher image (`ghcr.io/llm-d-incubation/llm-d-fast-model-actuation/launcher:v0.8.0`, `modelserver/launcherconfig.yaml`) is built on `vllm-openai:v0.26.0` (`llm-d-fast-model-actuation@v0.8.0:dockerfiles/Dockerfile.launcher.benchmark:1`); nothing in the guide pins a vLLM version, so a new launcher tag changes the engine. vLLM v0.26 serves `/v1/chat/completions/render` and `/v1/completions/render` without `--enable-scale-out` and has no Messages or Responses render route, so the combination declares `chat_render` and `completions_render`.
