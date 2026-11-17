# Example profiles

Templates to copy. Each is complete and validates offline; replace the placeholders with values
you measure. Copy to `profiles/<system>/<model>.local.yaml` (gitignored) for real hosts.

| Profile | Shape |
| --- | --- |
| [example-engine-only.yaml](example-engine-only.yaml) | one engine reached directly, such as `vllm serve`, with nothing in front |
| [example-gateway-only.yaml](example-gateway-only.yaml) | one gateway, no cluster access: a URL and a key are all you have |
| [example-two-ingress.yaml](example-two-ingress.yaml) | an engine plus a router gateway |
| [example-three-ingress.yaml](example-three-ingress.yaml) | a proxy (LiteLLM) in front of both |
| [example-two-ingress-staging.yaml](example-two-ingress-staging.yaml) | the two-ingress stack in a second namespace, written with `extends` as only its changes |

## A custom llm-d stack, built from parts

[`custom-llm-d/`](custom-llm-d/) shows a deployment that follows none of llm-d's guides: LiteLLM in front of the llm-d router in front of vLLM. It is written with list-form `extends`, so each fact lives in one part:

| File | What it is |
| --- | --- |
| [custom-llm-d/glm-5.3.yaml](custom-llm-d/glm-5.3.yaml) | GLM-5.3, a text-only reasoning model, on prefill/decode vLLM. Run this file |
| [custom-llm-d/deepseek-v4.1-flash.yaml](custom-llm-d/deepseek-v4.1-flash.yaml) | DeepSeek-V4.1-Flash, a multimodal model, with the engine's render routes served at the router. Run this file |
| `custom-llm-d/parts/` | the building blocks both list: what the APIs promise (`base.yaml`), the LiteLLM layer, the router layer, and one file per model. Never run on their own |

The values were measured on one deployment and say so beside each line; measure yours. Put your URLs, keys and the model ids your gateway serves in a gitignored `*.local.yaml` that extends one of the two files.

See the [profile guide](../README.md) and the [field reference](../REFERENCE.md).
