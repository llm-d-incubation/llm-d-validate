# Profiles

A profile describes your stack and defines its expected behavior. `llmdv` uses it to select
relevant test cases and verify that each layer of the stack meets those expectations.

Every [llm-d guide](#llm-d-guide-validation-profiles) at [Tier 3](https://github.com/llm-d/llm-d/blob/main/guides/GUIDES-POLICY.md#5-production-readiness-tiers)
(Validated: run end to end every night) includes a ready-to-use profile. After deploying a guide,
use its profile to verify that your deployment behaves as specified by the guide and its APIs.
By default, these profiles target the guide's router. To include a gateway or engine in your
tests, simply [add it as another ingress](#include-a-gateway-or-engine).

For other model-serving stacks, `llmdv` also provides [example profiles](#your-own-stack) as a
starting point, whether or not your deployment uses llm-d.

For a first custom profile, copy [example-minimal.yaml](examples/example-minimal.yaml), set the
model id and URL/key variables, then follow [the authoring walkthrough](../docs/writing-profiles.md).
Use `llmdv profile explain <path>` to inspect overrides and `profile show <path> --yaml` to export
the composed declarations.

Plain `plan`, `verify` and `job` commands use `test_sets.default`; a profile without one
requires an explicit scope. The examples ship with small defaults. Use `--test-set NAME`
(repeatable) for named subsets, `--all-test-sets` for all profile-defined sets, or `--full-suite`
for the library. Other filters still narrow the scope. See
[what a verification command selects](#what-a-verification-command-selects), and
[test sets](REFERENCE.md#test_sets) for the fields.

Find your starting point:

| You have | Start here |
| --- | --- |
| a deployed llm-d guide | [llm-d guide validation profiles](#llm-d-guide-validation-profiles): the guide's ready profile, and its EPP's URL |
| any other OpenAI- or Anthropic-compatible stack | [Your own stack](#your-own-stack): write a profile |

## llm-d guide validation profiles

Every guide gets the same base tests: each API a client uses, the errors the router must return,
the overlay's reasoning and tool parsers, and its context window. On top of those, each guide is
tested on what makes it different:

| Guide under test | What `llmdv` tests, and why | Profiles |
| --- | --- | --- |
| [agentic-api](llm-d/agentic-api/README.md) | The Responses API in depth (input items, instructions, tool loops, no stored state), because the agentic-api guide calls vLLM's stateless Responses API through the router | 1 |
| [async-multitenant](llm-d/async-multitenant/README.md) | Concurrent and cancelled requests still served, and flow control's queue header, because the async-multitenant guide runs the router with flow control, which queues and evicts work | 1 |
| [fast-model-actuation](llm-d/fast-model-actuation/README.md) | HTTP edge cases (chunked bodies, long headers, encodings, `Expect`) and streaming, because the fast-model-actuation guide puts a TCP proxy between the router and vLLM | 1 |
| [flow-control](llm-d/flow-control/README.md) | Flow control's queue header on every request, and a cancelled request freeing its slot, because the flow-control guide queues requests in the router | 2 |
| [keda-epp](llm-d/keda-epp/README.md) | Concurrent and cancelled requests still served, and flow control's queue header on two combinations, because the keda-epp guide autoscales the model server during a run and runs flow control in those two routers | 3 |
| [multimodal-serving](llm-d/multimodal-serving/README.md) | Images on Chat, Messages and Responses, and bad images refused, because the multimodal-serving guide serves a vision model, with an encode step on one lane | 1 |
| [optimized-baseline](llm-d/optimized-baseline/README.md) | Completions sharing a prefix, and streaming through the router, because the optimized-baseline guide claims both | 7, plus a simulator profile |
| [pd-disaggregation](llm-d/pd-disaggregation/README.md) | Streaming, usage frames, `n` choices and error shapes, because the pd-disaggregation guide sends every request through the decode pod's routing sidecar, which splits `n` and rewrites usage frames | 5 |
| [pod-snapshot](llm-d/pod-snapshot/README.md) | None, because the pod-snapshot guide changes only cluster state (snapshot and restore), which a client cannot see | 2 |
| [precise-prefix-cache-routing](llm-d/precise-prefix-cache-routing/README.md) | Token counts and rendered prompts that agree with the engine, because the precise-prefix-cache-routing guide has the router render every prompt | 4 |
| [predicted-latency-routing](llm-d/predicted-latency-routing/README.md) | Streaming and non-streaming clients at a 128k window, because the predicted-latency-routing guide serves both, at that window on GPU and AMD | 2 |
| [tiered-prefix-cache](llm-d/tiered-prefix-cache/README.md) | Completions sharing a long prefix, because the tiered-prefix-cache guide offloads that prefix's KV-cache blocks, and its own check sends these requests | 5 |
| [wide-ep](llm-d/wide-ep/README.md) | What pd-disaggregation tests, plus concurrent requests, because the wide-ep guide uses the same routing sidecar and its own check sends requests at once | 3 |

### Test against a deployed guide

Run the profile that matches your configuration. By default it targets the guide's router, so
give `llmdv` the EPP's URL, which needs no key:

```sh
export LLMDV_ROUTER_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml
```

Each guide's README says which profile matches the overlay you deployed, what it tests and what it
does not. [llm-d/README.md](llm-d/README.md) explains the profile names and the optional
`--identity` flags, and lists [what is not covered](llm-d/README.md#not-covered).

### Include a gateway or engine

The ready profiles test the guide's router. If your stack also exposes a gateway in front of it
(Istio, agentgateway, LiteLLM or your own) or the engine behind it, add each as another ingress in
a short local file that extends the guide's profile. Nothing in your stack changes:

```yaml
# profiles/my-guide.local.yaml (gitignored)
extends: llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml
name: my-optimized-baseline
ingresses:
  - id: gateway                                     # in front of the router
    kind: gateway
    base_url_env: MY_GATEWAY_URL
    auth: {type: bearer, key_env: MY_GATEWAY_KEY}   # or {type: none}
    surfaces: [chat_completions, completions]       # the APIs your gateway routes
  - id: engine                                      # behind the router
    kind: engine
    base_url_env: MY_ENGINE_URL
    auth: {type: none}
    surfaces: [chat_completions, completions, messages, responses]
```

Keep only the ones you can reach. `llmdv` then tests each layer separately, so a failure shows
which one caused it.

## Your own stack

Any OpenAI- or Anthropic-compatible gateway, router or engine, with or without llm-d, gets a
profile of its own. Copy the closest example, fill in its placeholders, and run it:

```sh
mkdir -p profiles/<system>
cp profiles/examples/example-gateway-only.yaml profiles/<system>/<model>.local.yaml
llmdv profile validate profiles/<system>/<model>.local.yaml
llmdv verify --profile profiles/<system>/<model>.local.yaml --full-suite -m contract
```

Each other layer you can reach, such as a gateway in front of a router, is another ingress in
the same file ([step 3](../docs/writing-profiles.md#the-steps)). `*.local.yaml` files are
gitignored, so your hosts stay out of the repository.
[Writing a profile](../docs/writing-profiles.md) walks through every step, the
[examples](../docs/writing-profiles.md#start-from-the-closest-example) and `extends`;
[REFERENCE.md](REFERENCE.md) lists every field.

## What a verification command selects

A profile's `test_sets` choose which cases `plan`, `verify` and `job` select. They never change
what a case asserts, and the same flags work on all three commands:

| Flags | Selects |
| --- | --- |
| none | the profile's `default` set; with no `default`, the command exits 2 and lists the choices |
| `--test-set NAME`, repeatable | the named sets, combined; `--test-set default` names the default explicitly |
| `--test-set smoke` | the built-in `smoke` set, below |
| `--all-test-sets` | every profile-defined set, combined; never `smoke` |
| `--full-suite` | every verification case in the library |

**The built-in `smoke` set** is the same for every profile: `discovery.models_lists_declared_model`,
`openai_chat.basic_non_streaming`, `openai_chat.streaming_sse`,
`openai_completions.basic_non_streaming`, `streaming.completions_lifecycle[True]`,
`openai_responses.basic_non_streaming`, `openai_responses.streaming_sse`,
`anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse`.
It is model discovery plus one plain and one streaming request on each API. A profile cannot
redefine it.

**Selected is not the same as executed.** Filters (`-k`, `-m`, `--surface`, `--client`,
`--ingress`) narrow the chosen scope, and a filter that leaves no case exits 2. The profile's
declarations, capabilities and fleet policy then skip selected cases that cannot apply, each with
its [skip class](../docs/cli.md#skip-classes). A case that would run can still skip during the run
(throttled, `requires_instrumentation`, or a declaration it reads). `--identity` flags label a run
and never change what it selects. To see all of this for a profile, offline:

```sh
llmdv profile show <profile> --test-sets     # every set and its case-ID patterns
llmdv plan --profile <profile>               # the default: each case, and why one skips
llmdv plan --profile <profile> --test-set smoke
llmdv plan --profile <profile> --full-suite
```

**Each folder's README says what its profiles select:** the default, where it is defined, the
patterns by suite, and what each scope selects for each profile, counted by `llmdv plan`:

- llm-d guides: [agentic-api](llm-d/agentic-api/README.md#what-llmdv-verify-selects),
  [async-multitenant](llm-d/async-multitenant/README.md#what-llmdv-verify-selects),
  [fast-model-actuation](llm-d/fast-model-actuation/README.md#what-llmdv-verify-selects),
  [flow-control](llm-d/flow-control/README.md#what-llmdv-verify-selects),
  [keda-epp](llm-d/keda-epp/README.md#what-llmdv-verify-selects),
  [multimodal-serving](llm-d/multimodal-serving/README.md#what-llmdv-verify-selects),
  [optimized-baseline](llm-d/optimized-baseline/README.md#what-llmdv-verify-selects),
  [pd-disaggregation](llm-d/pd-disaggregation/README.md#what-llmdv-verify-selects),
  [pod-snapshot](llm-d/pod-snapshot/README.md#what-llmdv-verify-selects),
  [precise-prefix-cache-routing](llm-d/precise-prefix-cache-routing/README.md#what-llmdv-verify-selects),
  [predicted-latency-routing](llm-d/predicted-latency-routing/README.md#what-llmdv-verify-selects),
  [tiered-prefix-cache](llm-d/tiered-prefix-cache/README.md#what-llmdv-verify-selects),
  [wide-ep](llm-d/wide-ep/README.md#what-llmdv-verify-selects)
- [examples](examples/README.md#what-llmdv-verify-selects) and the
  [custom llm-d deployment](examples/custom-llm-d/README.md#what-llmdv-verify-selects)
