# overlays

Kustomize overlays on [`../base/`](../base/). Real overlays carry cluster detail, so they live
with the fleet's own manifests or in a gitignored `*.local/` directory here, never in the
repository.

| Path | What it is |
| --- | --- |
| [`example/`](example/) | a placeholder overlay to copy |
