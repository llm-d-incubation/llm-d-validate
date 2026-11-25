# async-multitenant

Profiles for testing a deployment of llm-d's [multitenant async-processing](https://github.com/llm-d/llm-d/tree/main/guides/batch-serving/asynchronous-processing/multitenant) guide (`guide.yaml` name `async-multitenant`), in which llm-d-async dispatches queued work from Redis through an llm-d router with flow control and six priority bands, beside realtime traffic, with the router coordinator as an HTTP front door.

[Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What it tests](#what-it-tests) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## Pick your profile

There is one profile, for the guide's `modelserver/gpu/vllm/gke` overlay (optimized-baseline's `gpu/vllm/gke` relabelled, one replica). The router values file you deployed doesn't change what a client sending one request at a time sees, so both share it.

| Profile | Model a client sees | Router values |
| --- | --- | --- |
| `vllm-qwen3-32b-parsers-standalone` | Qwen3-32B on vLLM, with reasoning and tool-call parsers | `values/router/flow-control-evictable.yaml`, `values/router/flow-control-holdback.yaml` |

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_EPP_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/async-multitenant/vllm-qwen3-32b-parsers-standalone.yaml
```

To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional: they label a run so `llmdv compare` never compares two configurations, for example `--identity accelerator=gpu --identity provider=gke --identity flavor=evictable`, with `flavor` naming your router values file (`evictable` or `holdback`).

## What it tests

The llm-d router only, in standalone mode (through `../parts/topology/standalone.yaml`), not the coordinator, llm-d-async or Redis: see [Not covered](#not-covered). The router runs flow control, which a client sees as the `x-llm-d-flow-queue-duration-ms` response header on every admitted request; `parts/common.yaml` declares it `promised`, so a router running without flow control fails. On top of the base tests every guide gets (each API, error handling, the parsers and the context window), the profile sends concurrent, cancelled and disconnected requests, which a router running flow control with eviction or holdback must still serve.

## What `llmdv verify` selects

A plain `llmdv verify` runs the `default` test set, defined once in [`parts/common.yaml`](parts/common.yaml) and used unchanged by every profile here. `--test-set smoke` runs the [built-in smoke checks](../../README.md#what-a-verification-command-selects), and `--full-suite` runs every case in the library. There are no other sets, so `--all-test-sets` is the same as the default. Filters such as `-k` only narrow the run, and `--identity` never changes it.

<details>
<summary>The default's case-ID patterns, in 12 suites</summary>

`suite.*` takes the whole suite; any other pattern names one case. The rest of each suite is not selected.

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
| `vllm-qwen3-32b-parsers-standalone.yaml` | 95 selected, 89 run, 6 skip: 6 undeclared | 9 selected, 9 run | 1084 selected, 913 run, 171 skip: 171 undeclared |

</details>

To see every case and why one skips: `llmdv plan --profile <profile>` (add `--test-set smoke` or `--full-suite` for the other scopes), and `llmdv profile show <profile> --test-sets` for the definitions.
## Not covered

- **Eviction, holdback, priority and fairness under saturation** (`requires_load`, #23). llm-d's own validation runs an inference-perf experiment with realtime and async traffic at 20/80/90/100 % of the router's capacity; `llmdv` has no load mode. The objective and fairness headers are declared `consumed` in the topology part and skip as `requires_load`.
- **The coordinator** (`llm-d-coordinator`, port 8080): `x-llm-d-async-mode` (`passthrough`, `wait`, `enqueue`) and `x-llm-d-tenant`. Covering it needs a coordinator ingress in front of the router (gateway position, `fronts: [epp]`, #24), measured surfaces and error statuses, and, for `enqueue`, a submit-and-poll surface `llmdv` does not have.
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
