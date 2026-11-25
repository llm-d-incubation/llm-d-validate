# Profiles

A profile tells llmdv what your stack is and how a correct one behaves. llmdv reads it, picks the
cases that apply, and tests every layer against it. Find your starting point:

| You have | Start here |
| --- | --- |
| a deployed llm-d guide | [Test your guide](#test-your-guide): ready profiles, give llmdv the EPP's URL |
| any other OpenAI- or Anthropic-compatible stack | [Your own stack](#your-own-stack): write a profile |

## Profiles for llm-d guides

Ready profiles for testing a guide you have already deployed. Each guide folder under
[`llm-d/`](llm-d/) holds profiles for the configurations llm-d's nightly lanes cover, and a
README that maps every lane to its profile and `--identity` flags. The profiles are written
against llm-d `9ca8ff0` and test the guide's router (`<GUIDE_NAME>-epp`, standalone mode). None
has been run against a real engine yet: each says `WRITTEN, NOT RUN`, and its first run is its
measurement.

Every guide's profiles test the router's baseline: each API a client uses, the errors it must
return, the overlay's reasoning and tool parsers, and its context window. Each adds what its guide
changes:

| Guide | What it adds, and why | Profiles |
| --- | --- | --- |
| [agentic-api](llm-d/agentic-api/README.md) | the Responses API in depth (input items, instructions, tool loops, no stored state), because agentic-api calls vLLM's stateless Responses API through the router | 1 |
| [async-multitenant](llm-d/async-multitenant/README.md) | concurrent and cancelled requests still served, and flow control's queue header, because the router queues and evicts work | 1 |
| [fast-model-actuation](llm-d/fast-model-actuation/README.md) | HTTP edge cases (chunked bodies, long headers, encodings, `Expect`) and streaming, because a TCP proxy sits between the router and vLLM | 1 |
| [flow-control](llm-d/flow-control/README.md) | flow control's queue header on every request, and a cancelled request freeing its slot, because requests queue in the router | 2 |
| [keda-epp](llm-d/keda-epp/README.md) | concurrent and cancelled requests still served, because replicas change during a run; flow control's queue header on the two combinations whose router runs it | 3 |
| [multimodal-serving](llm-d/multimodal-serving/README.md) | images on Chat, Messages and Responses, and bad images refused, because the guide serves a vision model, through an encode step on one lane | 1 |
| [optimized-baseline](llm-d/optimized-baseline/README.md) | completions sharing a prefix, and streaming through the router: the guide's own claims | 7, plus a simulator profile |
| [pd-disaggregation](llm-d/pd-disaggregation/README.md) | streaming, usage frames, `n` choices and error shapes, because every request passes the decode pod's routing sidecar, which splits `n` and rewrites usage frames | 5 |
| [pod-snapshot](llm-d/pod-snapshot/README.md) | nothing: snapshot and restore are cluster state, which a client cannot see | 2 |
| [precise-prefix-cache-routing](llm-d/precise-prefix-cache-routing/README.md) | token counts and rendered prompts that agree with the engine, because the router renders every prompt | 4 |
| [predicted-latency-routing](llm-d/predicted-latency-routing/README.md) | streaming as well as non-streaming clients, at a 128k window on GPU and AMD | 2 |
| [tiered-prefix-cache](llm-d/tiered-prefix-cache/README.md) | completions sharing a long prefix, the request the guide's own check sends | 5 |
| [wide-ep](llm-d/wide-ep/README.md) | everything pd-disaggregation adds, plus concurrent requests, because it uses the same sidecar and its check sends requests at once | 3 |

Each guide's README says what it does not cover, and why; [llm-d/README.md](llm-d/README.md#coverage)
has the lane counts and the guides not covered.

## Test your guide

Your guide is already deployed: give llmdv its EPP's URL and verify it with the profile that
matches your deployment. The EPP needs no key:

```sh
export LLMDV_EPP_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

Each guide's folder names its combinations and their `--identity` flags;
[llm-d/README.md](llm-d/README.md) explains how to pick one.

### Test through your gateway

If your guide already sits behind a gateway (Istio, agentgateway, LiteLLM or your own), llmdv can
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

llmdv then tests the gateway and the router separately, so a failure shows which one caused it.

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
