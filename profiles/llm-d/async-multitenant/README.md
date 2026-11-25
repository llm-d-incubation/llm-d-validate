# async-multitenant

Profiles for llm-d's [multitenant async-processing guide](https://github.com/llm-d/llm-d/tree/main/guides/batch-serving/asynchronous-processing/multitenant) (`guide.yaml` name `async-multitenant`) at llm-d `9ca8ff0`: llm-d-async dispatching queued work from Redis through an llm-d router with flow control and six priority bands, beside realtime traffic, with the router coordinator as an HTTP front door.

**These profiles cover only the router.** They test the guide's llm-d router (EPP), in standalone mode, through `../parts/topology/standalone.yaml`. The Service is the router release's `<release>-epp` on port 80, where `<release>` is the guide's `POOL_NAME` (the lanes' `gateway_host`).

## Lanes

Both lanes run `guides/batch-serving/asynchronous-processing/multitenant/scripts/nightly-deploy-gke.sh` through `reusable-nightly-e2e-gke`, render the guide's `modelserver/gpu/vllm/gke` (optimized-baseline's `gpu/vllm/gke` relabelled, one replica), and differ only in the router values file. A client sending one request at a time cannot tell the two apart, so they share one profile and pass `--identity flavor`. Both run flow control, which a client sees as the `x-llm-d-flow-queue-duration-ms` response header on every admitted request; `parts/common.yaml` declares it `promised`.

| CI lane (`nightly-e2e-<lane>`) | Router values | Profile | Identity flags |
| --- | --- | --- | --- |
| `async-multitenant-gke-acc-gpu-vllm-x` | `values/router/flow-control-evictable.yaml` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=evictable` |
| `async-multitenant-holdback-gke-acc-gpu-vllm-x` | `values/router/flow-control-holdback.yaml` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=holdback` |

```sh
export LLMDV_EPP_URL=<the EPP's URL>
llmdv verify --profile profiles/llm-d/async-multitenant/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke --identity flavor=evictable
```

## What `llmdv verify` selects

This applies to every runnable profile in this folder: `vllm-qwen3-32b-parsers-standalone.yaml`.

**Default.** A `plan`, `verify` or `job` with no scope flag runs the `default` test set. It is defined once, in [`parts/common.yaml`](parts/common.yaml), and every profile above inherits it unchanged; none redefines it. There is no other profile-defined set.

**The `default` set's case-ID patterns, by the suite they select from** (12 suites, each confirmed by `llmdv plan`). A pattern ending in `.*` takes every case of its suite; any other names one case, or one variant in brackets. The rest of each suite is not selected:

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

**Named sets:** `default` (above) and the built-in `smoke`, which every profile has ([its cases](../../README.md#what-a-verification-command-selects)). `--all-test-sets` combines the profile-defined sets only, so here it selects exactly `default`; `smoke` is never included in it.

**What each scope selects**, as `llmdv plan` counts it offline. Counts are case instances (one per ingress and variant). *Run* means the profile's declarations, capabilities and fleet policy let the case run; a case can still skip during the run (throttled, `requires_instrumentation`, or a declaration it reads). *Skip* names the skip classes:

| Profile | no flag, or `--test-set default`, or `--all-test-sets` | `--test-set smoke` | `--full-suite` (every case in the library) |
| --- | --- | --- | --- |
| `vllm-qwen3-32b-parsers-standalone.yaml` | 95 selected, 89 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1084 selected, 913 run, 171 skip (171 undeclared) |

Other filters (`-k`, `-m`, `--surface`, `--client`, `--ingress`) only narrow the chosen scope, and a filter that leaves no case exits 2. `--identity` flags label the run and never change what it selects.

To inspect the definitions, the cases and every skip reason, offline:

```sh
P=profiles/llm-d/async-multitenant/vllm-qwen3-32b-parsers-standalone.yaml
llmdv profile show $P --test-sets        # every set and its patterns
llmdv plan --profile $P                  # the default: each case, and why one skips
llmdv plan --profile $P --test-set smoke
llmdv plan --profile $P --full-suite
llmdv plan --profile $P --json           # the same, as JSON
```
## Not covered

- **Eviction, holdback, priority and fairness under saturation** (`requires_load`, #23). The lanes' validator runs an inference-perf experiment with realtime and async traffic at 20/80/90/100 % of the router's capacity; `llmdv` has no load mode. The objective and fairness headers are declared `consumed` in the topology part and skip as `requires_load`.
- **The coordinator** (`llm-d-coordinator`, port 8080): `x-llm-d-async-mode` (`passthrough`, `wait`, `enqueue`) and `x-llm-d-tenant`. Covering it needs a coordinator ingress in front of the router (gateway position, `fronts: [epp]`, #24), measured surfaces and error statuses, and, for `enqueue`, a submit-and-poll surface `llmdv` does not have.
- **llm-d-async and Redis**: queues, quota gates, tier priority and objective stamping are not HTTP surfaces.

`parts/common.yaml` maps each of the guide's claims.

## Upstream notes

- `guide.yaml` has no `support:` table, so no configuration is marked `validated`; these profiles follow the two lanes.
- The holdback lane passes `guide_name: async-multitenant-holdback` to the reusable workflow, a name no `guide.yaml` declares; the router's own release is the same as the other lane's.
- `guide.yaml`'s `GPUS=1` option (`modelserver/gpu/vllm/single-gpu`, Qwen/Qwen3-8B) and the Pub/Sub queue backend are `skip_in: [ci]`; no lane runs them, so no profile covers them.
