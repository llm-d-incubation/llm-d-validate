# Profiles

A profile tells llmdv what your stack is and how a correct one behaves. llmdv reads it, picks the
cases that apply, and tests every layer against it. Find your starting point:

| You have | Start here |
| --- | --- |
| a deployed llm-d guide | [Test your guide](#test-your-guide): ready profiles, give llmdv the EPP's URL |
| an llm-d guide with a gateway in front | [Add your gateway](#add-your-gateway): one more ingress |
| any other OpenAI- or Anthropic-compatible stack | [Your own stack](#your-own-stack): write a profile |

## Supported llm-d guides

Each guide folder under [`llm-d/`](llm-d/) holds ready profiles for the configurations llm-d's
nightly lanes deploy, and a README that maps every lane to its profile and `--identity` flags.
The profiles are written against llm-d `9ca8ff0` and test the guide's router (`<GUIDE_NAME>-epp`,
standalone mode). None has been run against a real engine yet: each says `WRITTEN, NOT RUN`, and
its first run is its measurement.

| Guide | What it deploys | Profiles |
| --- | --- | --- |
| [agentic-api](llm-d/agentic-api/README.md) | vLLM Agentic API, backed by PostgreSQL, in front of optimized-baseline's router and vLLM; the router only | 1 |
| [async-multitenant](llm-d/async-multitenant/README.md) | llm-d-async dispatching queued work through a router with flow control and six priority bands; the router only | 1 |
| [fast-model-actuation](llm-d/fast-model-actuation/README.md) | vLLM in sleep-capable launcher pods behind a requester proxy | 1 |
| [flow-control](llm-d/flow-control/README.md) | the router with flow control on: priority bands, fairness and FCFS ordering | 2 |
| [keda-epp](llm-d/keda-epp/README.md) | optimized-baseline's model server, autoscaled by KEDA on a router metric | 3 |
| [multimodal-serving](llm-d/multimodal-serving/README.md) | a vision model (Qwen3-VL), aggregated or with encode disaggregated | 1 |
| [optimized-baseline](llm-d/optimized-baseline/README.md) | the reference stack: a prefix-aware router over vLLM, SGLang or Atom | 7, plus a simulator profile |
| [pd-disaggregation](llm-d/pd-disaggregation/README.md) | prefill and decode on separate pods, joined by the decode pod's routing sidecar | 5 |
| [pod-snapshot](llm-d/pod-snapshot/README.md) | one replica cold started and one restored from its snapshot | 2 |
| [precise-prefix-cache-routing](llm-d/precise-prefix-cache-routing/README.md) | routing on an index fed by each engine's KV-cache events | 4 |
| [predicted-latency-routing](llm-d/predicted-latency-routing/README.md) | routing on latency predicted from the router's own completed requests | 2 |
| [tiered-prefix-cache](llm-d/tiered-prefix-cache/README.md) | KV-cache blocks offloaded to CPU RAM or a shared filesystem | 5 |
| [wide-ep](llm-d/wide-ep/README.md) | wide expert parallelism with prefill/decode disaggregation | 3 |

[llm-d/README.md](llm-d/README.md#coverage) has the lane counts, the guides and layers not covered,
and why.

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

## Add your gateway

A gateway in front of the router (Istio, agentgateway, LiteLLM or your own) is one more ingress.
Extend the guide's combination in a short local file:

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

`*.local.yaml` files are gitignored, so your hosts stay out of the repository.
[Writing a profile](../docs/writing-profiles.md) walks through every step, the
[examples](../docs/writing-profiles.md#start-from-the-closest-example) and `extends`;
[REFERENCE.md](REFERENCE.md) lists every field.
