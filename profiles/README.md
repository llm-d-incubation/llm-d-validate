# Profiles

A profile tells `llmdv` what your stack is and how a correct one behaves. `llmdv` picks the cases
that apply and tests every layer you defined against it.

| You have | Start here |
| --- | --- |
| a deployed llm-d guide | [Test your guide](#test-your-guide) |
| any other OpenAI- or Anthropic-compatible stack | [Your own stack](#your-own-stack) |

## Test your guide

Every llm-d guide below has ready profiles. They name the guide's router (the EPP), as the guides
deploy it by default. Give `llmdv` the EPP's URL, which needs no key, and run the profile for your
configuration:

```sh
export LLMDV_EPP_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

Each guide's README maps its nightly lanes to a profile and `--identity` flags, and says what is
not tested, and why; [llm-d/README.md](llm-d/README.md) explains how to pick one. The profiles are
written against llm-d `9ca8ff0` and are `WRITTEN, NOT RUN`: each one's first run is its
measurement.

### What each guide is tested on

Every guide gets the same base tests: each API a client uses, the errors the router must return,
the overlay's reasoning and tool parsers, and its context window. On top of those:

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

### Test through your gateway

A gateway in front of the router (Istio, agentgateway, LiteLLM or your own), or the engine behind
it, is one more ingress in a short local file that extends the guide's profile. Nothing in your
stack changes:

```yaml
# profiles/my-guide.local.yaml (gitignored)
extends: llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml
name: my-optimized-baseline
ingresses:
  - id: gateway
    kind: gateway
    base_url_env: MY_GATEWAY_URL
    auth: {type: bearer, key_env: MY_GATEWAY_KEY}   # or {type: none}
    surfaces: [chat_completions, completions]       # the APIs your gateway routes
```

`llmdv` then tests each layer separately, so a failure shows which one caused it.

## Your own stack

Any OpenAI- or Anthropic-compatible gateway, router or engine, with or without llm-d, gets its own
profile, with one ingress per layer you can reach. Copy the closest example, fill in its
placeholders, and run it:

```sh
mkdir -p profiles/<system>
cp profiles/examples/example-gateway-only.yaml profiles/<system>/<model>.local.yaml
llmdv profile validate profiles/<system>/<model>.local.yaml
llmdv verify --profile profiles/<system>/<model>.local.yaml -m contract
```

`*.local.yaml` files are gitignored, so your hosts stay out of the repository.
[Writing a profile](../docs/writing-profiles.md) walks through every step, and
[REFERENCE.md](REFERENCE.md) lists every field.
