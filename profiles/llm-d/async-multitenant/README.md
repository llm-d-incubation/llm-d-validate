# async-multitenant

Profiles for testing a deployment of llm-d's [multitenant async-processing](https://github.com/llm-d/llm-d/tree/main/guides/batch-serving/asynchronous-processing/multitenant) guide (`guide.yaml` name `async-multitenant`), in which llm-d-async dispatches queued work from Redis through an llm-d router with flow control and six priority bands, beside realtime traffic, with the router coordinator as an HTTP front door.

[What it tests](#what-it-tests) · [Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## What it tests

**Validation focus: router behavior beneath the async coordinator.** Check the promised flow-control header and exercise concurrent, cancelled and disconnected requests to expose failures in the serving path.

**What a pass establishes:** the selected checks held for the declared APIs and reachable
ingress during this run. **Coverage boundary:** Coordinator scheduling, Redis and eviction/holdback policy need their own observations.

These focused defaults are a starting point for validating a production deployment. Add coverage
for the APIs, clients and operating conditions you rely on; see
[Growing coverage toward production](../../../docs/getting-started.md#widen-coverage).

<details>
<summary>Why these checks relate to the guide</summary>

The llm-d router only, in standalone mode (through `../parts/topology/standalone.yaml`), not the coordinator, llm-d-async or Redis: see [Not covered](#not-covered). The router runs flow control, which a client sees as the `x-llm-d-flow-queue-duration-ms` response header on every admitted request; `parts/common.yaml` declares it `promised`, so a router running without flow control fails. On top of the base tests every guide gets (each API, error handling, the parsers and the context window), the profile sends concurrent, cancelled and disconnected requests, which a router running flow control with eviction or holdback must still serve.

The declaration rationale and claim-to-case mapping live in
[`parts/common.yaml`](parts/common.yaml). [Not covered](#not-covered) explains the remaining
access, instrumentation and load requirements.

</details>

## Pick your profile

There is one profile, for the guide's `modelserver/gpu/vllm/gke` overlay (optimized-baseline's `gpu/vllm/gke` relabelled, one replica). The router values file you deployed doesn't change what a client sending one request at a time sees, so both share it.

| Model a client sees | Router values | Profile |
| --- | --- | --- |
| Qwen3-32B on vLLM, with reasoning and tool-call parsers | `values/router/flow-control-evictable.yaml`, `values/router/flow-control-holdback.yaml` | [vllm-qwen3-32b-parsers-standalone](vllm-qwen3-32b-parsers-standalone.yaml) |

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/async-multitenant/vllm-qwen3-32b-parsers-standalone.yaml
```

To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional for verification. Set them when comparing deployments that share a profile, so their recorded configuration dimensions remain distinct; for example `--identity accelerator=gpu --identity provider=gke --identity flavor=evictable`, with `flavor` naming your router values file (`evictable` or `holdback`).


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
| `openai_chat` | `openai_chat.basic_non_streaming`, `openai_chat.streaming_sse`, `openai_chat.stop_reason`, `openai_chat.configured_reasoning`, `openai_chat.disconnect_recovery` |
| `openai_completions` | `openai_completions.basic_non_streaming`, `openai_completions.streaming_matches_json`, `openai_completions.stop_reason` |
| `anthropic_messages` | `anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse`, `anthropic_messages.disconnect_recovery` |
| `openai_responses` | `openai_responses.basic_non_streaming`, `openai_responses.streaming_sse`, `openai_responses.disconnect_recovery` |
| `streaming` | `streaming.sse_framing`, `streaming.cancel_then_next`, `streaming.disconnect_before_output` |
| `unhappy` | `unhappy.unknown_path`, `unhappy.malformed_json`, `unhappy.missing_model`, `unhappy.wrong_method`, `unhappy.error_envelope_fields`, `unhappy.context_overflow`, `unhappy.token_limit_over_context` |
| `transport` | `transport.connection`, `transport.health_status` |
| `tools` | `tools.auto_call`, `tools.auto_stream` |
| `long_context` | `long_context.*` |
| `scenarios` | `scenarios.cancel_burst` |

</details>

<details>
<summary>How many cases each scope selects, per profile</summary>

Counted offline by `llmdv plan`, one per ingress and variant. *Run* cases can still skip during a run (for example `throttled`); *skip* cases are ruled out by the profile's declarations, with their skip class.

| Profile | Default (= `--all-test-sets`) | `smoke` | `--full-suite` |
| --- | --- | --- | --- |
| `vllm-qwen3-32b-parsers-standalone.yaml` | 95 selected, 89 run, 6 skip: 6 undeclared | 9 selected, 9 run | 1094 selected, 922 run, 172 skip: 172 undeclared |

</details>

Preview individual cases and skip reasons with `llmdv plan --profile <profile>`.

## Not covered

- **Eviction, holdback, priority and fairness under saturation** (`requires_load`, #23). llm-d's own validation runs an inference-perf experiment with realtime and async traffic at 20/80/90/100 % of the router's capacity; `llmdv` has no load mode. The objective and fairness headers are declared `consumed` in the topology part and skip as `requires_load`.
- **The coordinator** (`llm-d-coordinator`, port 8080): `x-llm-d-async-mode` (`passthrough`, `wait`, `enqueue`) and `x-llm-d-tenant`. Covering it needs a coordinator ingress in front of the router (gateway position, `fronts: [router]`, #24), measured surfaces and error statuses, and, for `enqueue`, a submit-and-poll surface `llmdv` does not have.
- **llm-d-async and Redis**: queues, quota gates, tier priority and objective stamping are not HTTP surfaces.
- **The `GPUS=1` option** (`modelserver/gpu/vllm/single-gpu`, Qwen/Qwen3-8B) **and the Pub/Sub queue backend**: llm-d does not test them nightly, so no profile covers them.

`parts/common.yaml` maps each of the guide's claims.

## For maintainers

<details>
<summary>llm-d's nightly lanes, and how each maps to a profile</summary>

Both lanes run `guides/batch-serving/asynchronous-processing/multitenant/scripts/nightly-deploy-gke.sh` through `reusable-nightly-e2e-gke`, render the guide's `modelserver/gpu/vllm/gke` (optimized-baseline's `gpu/vllm/gke` relabelled, one replica), and differ only in the router values file. A client sending one request at a time cannot tell the two apart, so they share one profile and pass `--identity flavor`. Both run flow control, which a client sees as the `x-llm-d-flow-queue-duration-ms` response header on every admitted request; `parts/common.yaml` declares it `promised`.

| CI lane (`nightly-e2e-<lane>`) | Router values | Profile | Identity flags |
| --- | --- | --- | --- |
| `async-multitenant-gke-acc-gpu-vllm-x` | `values/router/flow-control-evictable.yaml` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=evictable` |
| `async-multitenant-holdback-gke-acc-gpu-vllm-x` | `values/router/flow-control-holdback.yaml` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=holdback` |

The Service is the router release's `<release>-epp` on port 80, where `<release>` is the guide's `POOL_NAME` (the lanes' `gateway_host`).

</details>

<details>
<summary>Where llm-d's own sources disagree</summary>

- `guide.yaml` has no `support:` table, so no configuration is marked `validated`; these profiles follow the two lanes.
- The holdback lane passes `guide_name: async-multitenant-holdback` to the reusable workflow, a name no `guide.yaml` declares; the router's own release is the same as the other lane's.
- `guide.yaml`'s `GPUS=1` option (`modelserver/gpu/vllm/single-gpu`, Qwen/Qwen3-8B) and the Pub/Sub queue backend are `skip_in: [ci]`; no lane runs them, so no profile covers them.

</details>
