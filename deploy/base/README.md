# base

The generic kustomize base for running `llmdv job` in a cluster. It carries no cluster-identifiable
detail; an overlay supplies the namespace, image, settings and profile.

| File | What it is |
| --- | --- |
| `cronjob.yaml` | the one pod specification: scheduled nightly, and cloned by `trigger.sh` for on-demand runs |
| `role-trigger.yaml` | the `llmdv-trigger` Role a caller needs to start a run with `trigger.sh` |
| `kustomization.yaml` | lists the resources above |

Deploying is covered in [deploy/README.md](../README.md#deploying).
