# Example profiles

Templates to copy. Each is complete and validates offline; replace the placeholders with values
you measure. Copy to `profiles/<system>/<model>.local.yaml` (gitignored) for real hosts.

| Profile | Shape |
| --- | --- |
| [example-gateway-only.yaml](example-gateway-only.yaml) | one gateway, no cluster access: a URL and a key are all you have |
| [example-two-ingress.yaml](example-two-ingress.yaml) | an engine plus a router gateway |
| [example-three-ingress.yaml](example-three-ingress.yaml) | a proxy (LiteLLM) in front of both |

See the [profile guide](../README.md) and the [field reference](../REFERENCE.md).
