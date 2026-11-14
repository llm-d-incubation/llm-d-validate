# example

A placeholder overlay to copy. Replace every value before applying it.

| File | What it is |
| --- | --- |
| `kustomization.yaml` | the namespace, the image, the `llmdv-settings` ConfigMap and the profile ConfigMap |
| `profile.yaml` | an in-cluster profile (`vantage: in-cluster`) that reaches the fleet by Service DNS |

Settings, credentials and the Secret are covered in [deploy/README.md](../../README.md).
