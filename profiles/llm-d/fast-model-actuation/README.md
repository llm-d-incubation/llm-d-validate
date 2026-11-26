# fast-model-actuation

Profiles for testing a deployment of llm-d's [fast-model-actuation](https://github.com/llm-d/llm-d/tree/main/guides/fast-model-actuation) guide, in which the vLLM instance runs inside an FMA launcher pod with sleep mode on, and the router reaches it through a requester Deployment's TCP proxy.

[Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What it tests](#what-it-tests) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## Pick your profile

Pick the profile for the platform you applied (`kubectl apply -k platform/<INFRA_PROVIDER>`). The two platforms change only how the launcher reaches the node's GPUs, so they share a profile.

| Profile | Model a client sees | What you applied |
| --- | --- | --- |
| `vllm-qwen3-0.6b-inline-standalone` | Qwen3-0.6B on vLLM, the model's 40960-token window, no reasoning or tool-call parsers | `platform/gke`, `platform/ocp` |

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/fast-model-actuation/vllm-qwen3-0.6b-inline-standalone.yaml
```

Run it after the requester Deployment's rollout completes and before the guide's sleep/wake step, so both requesters are bound to awake instances.

To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional: they label a run so `llmdv compare` never compares two configurations, for example `--identity accelerator=gpu --identity provider=gke`, with `provider` the platform you applied.

## What it tests

The base tests every guide gets (each API, error handling and the context window), plus every streaming case and a wider set of HTTP transport cases (chunked requests, body edges, long and differently cased headers, `Expect: 100-continue` and others). They are there for the requester's TCP proxy, which sits between the router and vLLM on every request. It should pass vLLM's responses through byte for byte, so the profile declares vLLM's behaviour; what the proxy does when its vLLM instance refuses or drops a connection, and any error it answers itself, have not been measured. A failure in the error-envelope or disconnect cases that optimized-baseline's vLLM profiles do not show points at the proxy.

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
| `openai_completions` | `openai_completions.basic_non_streaming`, `openai_completions.streaming_matches_json`, `openai_completions.stop_reason` |
| `anthropic_messages` | `anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse` |
| `openai_responses` | `openai_responses.basic_non_streaming`, `openai_responses.streaming_sse` |
| `streaming` | `streaming.sse_framing`, `streaming.*` |
| `unhappy` | `unhappy.unknown_path`, `unhappy.malformed_json`, `unhappy.missing_model`, `unhappy.wrong_method`, `unhappy.error_envelope_fields`, `unhappy.context_overflow`, `unhappy.token_limit_over_context` |
| `transport` | `transport.connection`, `transport.health_status`, `transport.chunked_request`, `transport.body_edge`, `transport.long_header`, `transport.header_name_case`, `transport.bodiless_header`, `transport.content_type`, `transport.accept_event_stream`, `transport.response_encoding`, `transport.query_string`, `transport.path_variant`, `transport.expect_continue` |
| `tools` | `tools.auto_call`, `tools.auto_stream` |
| `long_context` | `long_context.*` |

</details>

<details>
<summary>How many cases each scope selects, per profile</summary>

Counted offline by `llmdv plan`, one per ingress and variant. *Run* cases can still skip during a run (for example `throttled`); *skip* cases are ruled out by the profile's declarations, with their skip class.

| Profile | Default (= `--all-test-sets`) | `smoke` | `--full-suite` |
| --- | --- | --- | --- |
| `vllm-qwen3-0.6b-inline-standalone.yaml` | 119 selected, 107 run, 12 skip: 12 undeclared | 9 selected, 9 run | 1092 selected, 684 run, 408 skip: 408 undeclared |

</details>

Preview individual cases and skip reasons with `llmdv plan --profile <profile>`.

## Not covered

- **Sleep and wake** (`verify.tests.sleep_wake`: scaling the requesters to zero puts vLLM to sleep, scaling back up wakes it): `llmdv` never scales a deployment, and the sleeping state is a pod label, which is cluster state.
- **Hot and warm start times**: timing is a benchmark's measurement, not `llmdv`'s.
- **The FMA controllers' lifecycle** (binding, LRU eviction of sleeping instances, launcher population): cluster state.
- **The cache suite**: the vLLM options do not include `--enable-prompt-tokens-details`, and the router has no prefix-cache plugin, so `cache_reuse` is not declared (see `parts/common.yaml`).
- **Gateway mode** and the other `GATEWAY_CLASS` values: llm-d does not test them nightly.
- **fast-model-actuation-keda** (`fast-model-actuation/fast-model-actuation-keda/`, lanes `nightly-e2e-fast-model-actuation-keda-{gke,ibm}-acc-gpu-vllm-x`): deferred. It scales the requesters from zero on EPP flow-control metrics, so the first request waits for the scale-up (the EPP holds it up to `noEndpointRequestTTL: 300s`, `router/fast-model-actuation-keda.values.yaml`), and the pool returns to zero after about 60 s idle (`cooldownPeriod` 30 s plus the HPA's 30 s stabilization, `keda/base/kustomization.yaml`). A profile cannot express that without loosening every latency check, and a run with gaps between cases would keep paying the cold start.

## For maintainers

See the [profile provenance guide](../../../docs/profile-provenance.md#how-profilesllm-d-is-built) for the layout and provenance tags.

<details>
<summary>llm-d's nightly lanes, and how each maps to a profile</summary>

The guide has no support table; `guide.yaml` offers `INFRA_PROVIDER` `ocp` or `gke`. Both nightly lanes stand the guide up with `kubectl apply -k platform/<INFRA_PROVIDER>`: the FMA InferenceServerConfig, LauncherConfig and LauncherPopulationPolicy, and the requester Deployment. The vLLM instance runs inside an FMA launcher pod with `--model Qwen/Qwen3-0.6B --enable-sleep-mode --gpu-memory-utilization 0.9` and no parsers; the EPP reaches it through the requester's TCP proxy on port 8000. The two providers change only how the launcher reaches the node's GPUs, so one combination covers both.

| CI lane (`nightly-e2e-fast-model-actuation-<lane>`) | What it applies | Profile | Identity flags |
| --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `platform/gke` | `vllm-qwen3-0.6b-inline-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `ibm-acc-gpu-vllm-x` | `platform/ocp` | `vllm-qwen3-0.6b-inline-standalone` | `--identity accelerator=gpu --identity provider=ocp` |

The combination reuses `../parts/models/qwen3-0.6b-inline.yaml` (the model id, the derived 40960-token window and the absence of parsers are the same as optimized-baseline's XPU overlay). The `modelserver/gpu/vllm/{base,gke,ocp}` overlays the benchmark harness renders are detection-only stubs (a `replicas: 0` Deployment that names the model) and are never applied.

The client calls the `<GUIDE_NAME>-epp` Service on port 80, in standalone mode. Run `llmdv` after the requester Deployment's rollout completes (guide.yaml deploy.modelserver) and before the guide's sleep/wake step, so both requesters are bound to awake instances.

</details>

<details>
<summary>Where llm-d's own sources disagree</summary>

- The FMA launcher image (`ghcr.io/llm-d-incubation/llm-d-fast-model-actuation/launcher:v0.8.0`, `modelserver/launcherconfig.yaml`) is built on `vllm-openai:v0.26.0` (`llm-d-fast-model-actuation@v0.8.0:dockerfiles/Dockerfile.launcher.benchmark:1`); nothing in the guide pins a vLLM version, so a new launcher tag changes the engine. vLLM v0.26 serves `/v1/chat/completions/render` and `/v1/completions/render` without `--enable-scale-out` and has no Messages or Responses render route, so the combination declares `chat_render` and `completions_render`.

</details>
