# fast-model-actuation

Profiles for llm-d's [fast-model-actuation](https://github.com/llm-d/llm-d/tree/main/guides/fast-model-actuation) guide. See [../README.md](../README.md) for how to run a combination, and the [profile skill](../../../.agents/skills/writing-an-infra-profile/SKILL.md#how-profilesllm-d-is-built) for the layout and provenance tags.

## Lanes

The guide has no support table; `guide.yaml` offers `INFRA_PROVIDER` `ocp` or `gke`. Both nightly lanes stand the guide up with `kubectl apply -k platform/<INFRA_PROVIDER>`: the FMA InferenceServerConfig, LauncherConfig and LauncherPopulationPolicy, and the requester Deployment. The vLLM instance runs inside an FMA launcher pod with `--model Qwen/Qwen3-0.6B --enable-sleep-mode --gpu-memory-utilization 0.9` and no parsers; the EPP reaches it through the requester's TCP proxy on port 8000. The two providers change only how the launcher reaches the node's GPUs, so one combination covers both.

| CI lane (`nightly-e2e-fast-model-actuation-<lane>`) | What it applies | Profile | Identity flags |
| --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `platform/gke` | `vllm-qwen3-0.6b-inline-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `ibm-acc-gpu-vllm-x` | `platform/ocp` | `vllm-qwen3-0.6b-inline-standalone` | `--identity accelerator=gpu --identity provider=ocp` |

The combination reuses `../parts/models/qwen3-0.6b-inline.yaml` (the model id, the derived 40960-token window and the absence of parsers are the same as optimized-baseline's XPU overlay). The `modelserver/gpu/vllm/{base,gke,ocp}` overlays the benchmark harness renders are detection-only stubs (a `replicas: 0` Deployment that names the model) and are never applied.

The client calls the `<GUIDE_NAME>-epp` Service on port 80, in standalone mode. Run `llmdv` after the requester Deployment's rollout completes (guide.yaml deploy.modelserver) and before the guide's sleep/wake step, so both requesters are bound to awake instances.

## What the profile checks that is particular to this guide

The requester's TCP proxy sits between the EPP and vLLM on every request. It should pass vLLM's responses through byte for byte, so the profile declares vLLM's behaviour; what the proxy does when its vLLM instance refuses or drops a connection, and any error it answers itself, have not been measured. A failure in the error-envelope or disconnect cases that the optimized-baseline vLLM lanes do not show points at the proxy.

## Not covered, and why

- **Sleep and wake** (`verify.tests.sleep_wake`: scaling the requesters to zero puts vLLM to sleep, scaling back up wakes it): `llmdv` never scales a deployment, and the sleeping state is a pod label, which is cluster state.
- **Hot and warm start times**: timing is a benchmark's measurement, not `llmdv`'s.
- **The FMA controllers' lifecycle** (binding, LRU eviction of sleeping instances, launcher population): cluster state.
- **The cache suite**: the vLLM options do not include `--enable-prompt-tokens-details`, and the router has no prefix-cache plugin, so `cache_reuse` is not declared (see `parts/common.yaml`).
- **Gateway mode** and the other `GATEWAY_CLASS` values: no lane runs them.
- **fast-model-actuation-keda** (`fast-model-actuation/fast-model-actuation-keda/`, lanes `nightly-e2e-fast-model-actuation-keda-{gke,ibm}-acc-gpu-vllm-x`): deferred. It scales the requesters from zero on EPP flow-control metrics, so the first request waits for the scale-up (the EPP holds it up to `noEndpointRequestTTL: 300s`, `router/fast-model-actuation-keda.values.yaml`), and the pool returns to zero after about 60 s idle (`cooldownPeriod` 30 s plus the HPA's 30 s stabilization, `keda/base/kustomization.yaml`). A profile cannot express that without loosening every latency check, and a run with gaps between cases would keep paying the cold start.

## Upstream notes

- The FMA launcher image (`ghcr.io/llm-d-incubation/llm-d-fast-model-actuation/launcher:v0.8.0`, `modelserver/launcherconfig.yaml`) is built on `vllm-openai:v0.26.0` (`llm-d-fast-model-actuation@v0.8.0:dockerfiles/Dockerfile.launcher.benchmark:1`); nothing in the guide pins a vLLM version, so a new launcher tag changes the engine. vLLM v0.26 serves `/v1/chat/completions/render` and `/v1/completions/render` without `--enable-scale-out` and has no Messages or Responses render route, so the combination declares `chat_render` and `completions_render`.
