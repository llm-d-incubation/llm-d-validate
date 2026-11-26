# agentic-api

Profiles for testing a deployment of llm-d's [agentic-api](https://github.com/llm-d/llm-d/tree/main/guides/agentic-api) guide, in which vLLM Agentic API (`vllm/agentic-api:v0.9.0`, Service `agentic-api` on port 9000), backed by PostgreSQL, sits in front of an llm-d router and optimized-baseline's vLLM model server.

[Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What it tests](#what-it-tests) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## Pick your profile

Pick the profile for the model-server overlay you deployed (under `guides/agentic-api/modelserver/`). The guide has one, so there is one profile.

| Profile | Model a client sees | Overlay |
| --- | --- | --- |
| `vllm-qwen3-32b-parsers-standalone` | Qwen3-32B on vLLM, with reasoning and tool-call parsers | `gpu/vllm/gke` (= `optimized-baseline/modelserver/gpu/vllm/gke`) |

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/agentic-api/vllm-qwen3-32b-parsers-standalone.yaml
```

To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional: they label a run so `llmdv compare` never compares two configurations, for example `--identity accelerator=gpu --identity provider=gke`, read off your overlay path.

## What it tests

The llm-d router only, in standalone mode (through `../parts/topology/standalone.yaml`), not agentic-api in front of it: see [Not covered](#not-covered). The router's config is optimized-baseline's, unchanged, so the router behaves as optimized-baseline's does. On top of the base tests every guide gets (each API, error handling, the parsers and the context window), the profile tests the stateless Responses API in more depth (input items, instructions, function output, a streamed tool loop, the stateless contract), because agentic-api calls vLLM's stateless Responses API through this router.

## What `llmdv verify` selects

A plain `llmdv verify` runs the `default` test set, defined once in [`parts/common.yaml`](parts/common.yaml) and used unchanged by every profile here. `--test-set smoke` runs the [built-in smoke checks](../../README.md#what-a-verification-command-selects), and `--full-suite` runs every case in the library. There are no other sets, so `--all-test-sets` is the same as the default. Filters such as `-k` only narrow the run, and `--identity` never changes it.

<details>
<summary>The default's case-ID patterns, in 11 suites</summary>

`suite.*` takes the whole suite; any other pattern names one case. The rest of each suite is not selected.

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

</details>

<details>
<summary>How many cases each scope selects, per profile</summary>

Counted offline by `llmdv plan`, one per ingress and variant. *Run* cases can still skip during a run (for example `throttled`); *skip* cases are ruled out by the profile's declarations, with their skip class.

| Profile | Default (= `--all-test-sets`) | `smoke` | `--full-suite` |
| --- | --- | --- | --- |
| `vllm-qwen3-32b-parsers-standalone.yaml` | 107 selected, 101 run, 6 skip: 6 undeclared | 9 selected, 9 run | 1082 selected, 911 run, 171 skip: 171 undeclared |

</details>

To see every case and why one skips: `llmdv plan --profile <profile>` (add `--test-set smoke` or `--full-suite` for the other scopes), and `llmdv profile show <profile> --test-sets` for the definitions.
## Not covered

**agentic-api itself.** The guide's own subject is not covered: health and `/v1/models` on agentic-api, stateful `/v1/responses` continued by `previous_response_id` from PostgreSQL, the webhook tool loop (`function_call`, then `function_call_output` against `previous_response_id`), and WebSocket `/v1/responses`. `parts/common.yaml` maps each claim.

## For maintainers

<details>
<summary>llm-d's nightly lanes, and how each maps to a profile</summary>

| CI lane (`nightly-e2e-agentic-api-<lane>`) | Reusable workflow | Overlay it renders | Profile | Identity flags |
| --- | --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `reusable-nightly-e2e-gke` | `agentic-api/modelserver/gpu/vllm/gke` (= `optimized-baseline/modelserver/gpu/vllm/gke`) | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |

The lane's deploy script is `guides/agentic-api/scripts/nightly-deploy-gke.sh` (one model-server replica), and its validator is `.github/scripts/e2e/e2e-validate-agentic-api.sh`, which runs the guide's `verify.py` against agentic-api.

The helm release is `agentic-api`, so the Service is `<GUIDE_NAME>-epp` on port 80 (the lane's `gateway_host`).

</details>

<details>
<summary>Covering agentic-api: what a profile would need</summary>

To cover agentic-api, a profile declares a second ingress in the gateway position (`kind: gateway`) for the `agentic-api` Service on port 9000, in front of the router, with `fronts: [router]` naming the router it sends to (#24, A5). That ingress then needs:

- its own surfaces and error statuses, measured: which paths agentic-api serves (at least `responses`, and whether it passes chat completions and completions through), what it answers for an unknown model, path or method, and whether its errors carry the OpenAI envelope;
- stateful Responses cases (`store`, `previous_response_id`, a tool loop continued by `function_call_output`) that `llmdv`'s Responses suite can run against a stateful ingress;
- a WebSocket transport for `ws://…/v1/responses`, which `llmdv` does not have.

Until a run measures agentic-api's behaviour, nothing about it is declared.

</details>

<details>
<summary>Where llm-d's own sources disagree</summary>

- `guide.yaml` marks only `gpu`/`vllm` `validated`, with providers `base` and `gke`; the one lane runs `gke`.
- The guide's model server keeps optimized-baseline's labels (`llm-d.ai/guide: optimized-baseline`), and the router selects on that label.
- `guide.yaml`'s support notes describe 2 replicas; the lane runs one (`nightly-deploy-gke.sh`, CI-only deviation 1).

</details>
