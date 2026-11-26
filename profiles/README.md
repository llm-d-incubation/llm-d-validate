# Choose a profile

A profile describes the model, reachable layers and expected API behavior. Choose one that
matches your deployment; [Getting started](../docs/getting-started.md) covers installation,
credentials, the first run and its results.

| Your deployment | Starting point |
| --- | --- |
| An llm-d guide | [Guide catalog](llm-d/README.md): match the deployed overlay |
| GLM, DeepSeek or Muse matching a measured example | [Measured profiles](examples/measured/README.md): review deployment-specific expectations and prerequisites |
| Another gateway, router or engine | [Your own stack](#your-own-stack): adapt a starter |

## llm-d guide validation profiles

The [guide catalog](llm-d/README.md) links each guide to its configuration mappings, commands,
coverage and limitations. These profiles target the guide's router and read `LLMDV_ROUTER_URL`.
A guide profile checks its declared client-visible behavior; it does not necessarily prove
load-dependent or internal cluster behavior. Read the guide's **Not covered** section.

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

Keep only the ones you can reach. `llmdv` then tests each layer separately, so the evidence helps locate where responses differ.

## Your own stack

Use the smallest example that describes what you can reach. One endpoint is enough.

| Example | Use it for |
| --- | --- |
| [Minimal](examples/example-minimal.yaml) | A first profile with model ID, endpoint, authentication and Chat Completions |
| [Engine only](examples/example-engine-only.yaml) | A vLLM engine reached directly |
| [Gateway only](examples/example-gateway-only.yaml) | A LiteLLM gateway |
| [Two ingresses](examples/example-two-ingress.yaml) | An engine and router with declared local setup |
| [Three ingresses](examples/example-three-ingress.yaml) | Gateway, router and engine |
| [Staging overlay](examples/example-two-ingress-staging.yaml) | Reusing a profile with `extends` |
| [Custom llm-d](examples/custom-llm-d/README.md) | Model and layer declarations composed from shared parts |

These are starting expectations, not a discovery of your deployment. Copy a starter to a
`*.local.yaml` file, replace placeholders, and review its declarations. See the
[custom-stack first run](../docs/getting-started.md#run-your-own-stack) or
[profile-authoring walkthrough](../docs/writing-profiles.md).

## What a verification command selects

Use [Selection and coverage](../docs/cli.md#test-sets-and-coverage) for the shared rules.
Each profile folder's README shows its own default patterns and generated planning counts.
Preview a particular configuration with `llmdv plan --profile <path>`.
