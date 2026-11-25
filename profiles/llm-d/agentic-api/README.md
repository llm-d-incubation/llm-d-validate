# agentic-api

Profiles for llm-d's [agentic-api guide](https://github.com/llm-d/llm-d/tree/main/guides/agentic-api) at llm-d `9ca8ff0`. The guide puts vLLM Agentic API (`vllm/agentic-api:v0.9.0`, Service `agentic-api` on port 9000), backed by PostgreSQL, in front of an llm-d router and optimized-baseline's vLLM model server.

**These profiles cover only the router.** They test the guide's llm-d router (EPP), in standalone mode, through `../parts/topology/standalone.yaml`. The helm release is `agentic-api`, so the Service is `<GUIDE_NAME>-epp` on port 80 (the lane's `gateway_host`). Its router config is optimized-baseline's, unchanged, so the router behaves as optimized-baseline's does.

## Lanes

| CI lane (`nightly-e2e-agentic-api-<lane>`) | Reusable workflow | Overlay it renders | Profile | Identity flags |
| --- | --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `reusable-nightly-e2e-gke` | `agentic-api/modelserver/gpu/vllm/gke` (= `optimized-baseline/modelserver/gpu/vllm/gke`) | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |

The lane's deploy script is `guides/agentic-api/scripts/nightly-deploy-gke.sh` (one model-server replica), and its validator is `.github/scripts/e2e/e2e-validate-agentic-api.sh`, which runs the guide's `verify.py` against agentic-api.

```sh
export LLMDV_EPP_URL=<the EPP's URL>
llmdv verify --profile profiles/llm-d/agentic-api/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

## What `llmdv verify` selects

This applies to every runnable profile in this folder: `vllm-qwen3-32b-parsers-standalone.yaml`.

**Default.** A `plan`, `verify` or `job` with no scope flag runs the `default` test set. It is defined once, in [`parts/common.yaml`](parts/common.yaml), and every profile above inherits it unchanged; none redefines it. There is no other profile-defined set.

**The `default` set's case-ID patterns, by the suite they select from** (11 suites, each confirmed by `llmdv plan`). A pattern ending in `.*` takes every case of its suite; any other names one case, or one variant in brackets. The rest of each suite is not selected:

| Suite | Patterns |
| --- | --- |
| `contract` | `contract.*` |
| `discovery` | `discovery.models_lists_declared_model`, `discovery.unknown_model_no_fallback`, `discovery.models_parses_with_openai_sdk` |
| `openai_chat` | `openai_chat.basic_non_streaming`, `openai_chat.streaming_sse`, `openai_chat.stop_reason`, `openai_chat.configured_reasoning` |
| `openai_completions` | `openai_completions.basic_non_streaming`, `openai_completions.streaming_matches_json`, `openai_completions.stop_reason` |
| `anthropic_messages` | `anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse` |
| `openai_responses` | `openai_responses.basic_non_streaming`, `openai_responses.streaming_sse`, `openai_responses.input_items`, `openai_responses.instructions`, `openai_responses.function_output`, `openai_responses.streamed_tool_loop`, `openai_responses.stateless_contract`, `openai_responses.previous_id_unstored`, `openai_responses.response_object`, `openai_responses.sdk_parse`, `openai_responses.input_tokens` |
| `streaming` | `streaming.sse_framing` |
| `unhappy` | `unhappy.unknown_path`, `unhappy.malformed_json`, `unhappy.missing_model`, `unhappy.wrong_method`, `unhappy.error_envelope_fields`, `unhappy.context_overflow`, `unhappy.token_limit_over_context` |
| `transport` | `transport.connection`, `transport.health_status` |
| `tools` | `tools.auto_call`, `tools.auto_stream` |
| `long_context` | `long_context.*` |

**Named sets:** `default` (above) and the built-in `smoke`, which every profile has ([its cases](../../README.md#what-a-verification-command-selects)). `--all-test-sets` combines the profile-defined sets only, so here it selects exactly `default`; `smoke` is never included in it.

**What each scope selects**, as `llmdv plan` counts it offline. Counts are case instances (one per ingress and variant). *Run* means the profile's declarations, capabilities and fleet policy let the case run; a case can still skip during the run (throttled, `requires_instrumentation`, or a declaration it reads). *Skip* names the skip classes:

| Profile | no flag, or `--test-set default`, or `--all-test-sets` | `--test-set smoke` | `--full-suite` (every case in the library) |
| --- | --- | --- | --- |
| `vllm-qwen3-32b-parsers-standalone.yaml` | 107 selected, 101 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1082 selected, 911 run, 171 skip (171 undeclared) |

Other filters (`-k`, `-m`, `--surface`, `--client`, `--ingress`) only narrow the chosen scope, and a filter that leaves no case exits 2. `--identity` flags label the run and never change what it selects.

To inspect the definitions, the cases and every skip reason, offline:

```sh
P=profiles/llm-d/agentic-api/vllm-qwen3-32b-parsers-standalone.yaml
llmdv profile show $P --test-sets        # every set and its patterns
llmdv plan --profile $P                  # the default: each case, and why one skips
llmdv plan --profile $P --test-set smoke
llmdv plan --profile $P --full-suite
llmdv plan --profile $P --json           # the same, as JSON
```
## Not covered: agentic-api itself

The guide's own subject is not covered: health and `/v1/models` on agentic-api, stateful `/v1/responses` continued by `previous_response_id` from PostgreSQL, the webhook tool loop (`function_call`, then `function_call_output` against `previous_response_id`), and WebSocket `/v1/responses`. `parts/common.yaml` maps each claim.

### Follow-up

To cover agentic-api, a profile declares a second ingress in the gateway position (`kind: gateway`) for the `agentic-api` Service on port 9000, in front of the router, with `fronts: [epp]` naming the router it sends to (#24, A5). That ingress then needs:

- its own surfaces and error statuses, measured: which paths agentic-api serves (at least `responses`, and whether it passes chat completions and completions through), what it answers for an unknown model, path or method, and whether its errors carry the OpenAI envelope;
- stateful Responses cases (`store`, `previous_response_id`, a tool loop continued by `function_call_output`) that `llmdv`'s Responses suite can run against a stateful ingress;
- a WebSocket transport for `ws://…/v1/responses`, which `llmdv` does not have.

Until a run measures agentic-api's behaviour, nothing about it is declared.

## Upstream notes

- `guide.yaml` marks only `gpu`/`vllm` `validated`, with providers `base` and `gke`; the one lane runs `gke`.
- The guide's model server keeps optimized-baseline's labels (`llm-d.ai/guide: optimized-baseline`), and the router selects on that label.
- `guide.yaml`'s support notes describe 2 replicas; the lane runs one (`nightly-deploy-gke.sh`, CI-only deviation 1).
