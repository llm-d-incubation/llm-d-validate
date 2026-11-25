# Profiles

A profile describes your stack and defines its expected behavior. `llmdv` uses it to select
relevant test cases and verify that each layer of the stack meets those expectations.

Each [supported llm-d guide](#testing-llm-d-guides) includes a ready-to-use profile. After
deploying a guide, use its profile to verify that your deployment behaves as specified by the
guide and its APIs. By default, these profiles target the guide's router. To include a gateway or
engine in your tests, simply [add it as another ingress](#test-through-your-gateway).

For other model-serving stacks, `llmdv` also provides [example profiles](#your-own-stack) as a
starting point, whether or not your deployment uses llm-d.

Find your starting point:

| You have | Start here |
| --- | --- |
| a deployed llm-d guide | [Testing llm-d guides](#testing-llm-d-guides): the guide's ready profile, and its EPP's URL |
| any other OpenAI- or Anthropic-compatible stack | [Your own stack](#your-own-stack): write a profile |

## Testing llm-d guides

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
export LLMDV_EPP_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

Each guide's folder holds a profile for every configuration llm-d's nightly lanes cover, and a
README that maps each lane to its profile and `--identity` flags and says what is not tested, and
why. [llm-d/README.md](llm-d/README.md) explains how to pick one, and has the
[lane counts and the guides not covered](llm-d/README.md#coverage). The profiles are written
against llm-d `9ca8ff0` and none has been run against a real engine yet: each says
`WRITTEN, NOT RUN`, and its first run is its measurement.

### Test through your gateway

If your guide already sits behind a gateway (Istio, agentgateway, LiteLLM or your own), `llmdv` can
test through it too. Nothing in your stack changes: name the gateway's URL in a short local file
that extends the guide's profile:

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

`llmdv` then tests the gateway and the router separately, so a failure shows which one caused it.

## Your own stack

Any OpenAI- or Anthropic-compatible gateway, router or engine, with or without llm-d, gets a
profile of its own. Copy the closest example, fill in its placeholders, and run it:

```sh
mkdir -p profiles/<system>
cp profiles/examples/example-gateway-only.yaml profiles/<system>/<model>.local.yaml
llmdv profile validate profiles/<system>/<model>.local.yaml
llmdv verify --profile profiles/<system>/<model>.local.yaml -m contract
```

Each other layer you can reach, such as a gateway in front of a router, is one more ingress in
the same file ([step 3](../docs/writing-profiles.md#the-steps)). `*.local.yaml` files are
gitignored, so your hosts stay out of the repository.
[Writing a profile](../docs/writing-profiles.md) walks through every step, the
[examples](../docs/writing-profiles.md#start-from-the-closest-example) and `extends`;
[REFERENCE.md](REFERENCE.md) lists every field.
