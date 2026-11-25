# wide-ep profiles

Profiles for llm-d's `wide-ep` guide (wide expert parallelism with P/D disaggregation), written from llm-d 9ca8ff0. How the parts and combinations fit together, and the provenance tags, are in the [profile skill](../../../.agents/skills/writing-an-infra-profile/SKILL.md#how-profilesllm-d-is-built). Every profile here is **WRITTEN, NOT RUN**: no lane has run `llmdv` against this guide yet, and the first run of each is its measurement.

## Lanes and profiles

The guide's five nightly lanes (`nightly-e2e-wide-ep-<lane>.yaml` in llm-d, each in standalone mode with `gateway_class: epponly`) deploy three configurations that differ to a client:

| CI lane | Overlay it deploys (`ACCELERATOR_TYPE/MODEL_SERVER/INFRA_PROVIDER`) | Profile (`wide-ep/`) | Identity flags |
| --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `gpu/vllm/gke` | `vllm-deepseek-r1-0528-tools-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `cks-acc-gpu-vllm-x` | `gpu/vllm/coreweave` | `vllm-deepseek-r1-0528-tools-standalone` | `--identity accelerator=gpu --identity provider=coreweave` |
| `ibm-acc-gpu-vllm-x` | `gpu/vllm/ocp` | `vllm-deepseek-r1-0528-tools-standalone` | `--identity accelerator=gpu --identity provider=ocp` |
| `amd-ci-acc-rocm-vllm-moriio` | `amd/vllm/amd-ci` | `vllm-deepseek-v3-ctx36k-inline-standalone` | `--identity accelerator=amd --identity provider=amd-ci` |
| `intel-acc-xpu-vllm-x` | `xpu/vllm/xpu-ci` | `vllm-deepseek-v2-lite-chat-ctx2k-inline-standalone` | `--identity accelerator=xpu --identity provider=xpu-ci` |

No two lanes share an identity, so none needs `--identity flavor`.

What each serves, read from the overlays rendered with `kubectl kustomize`:

- **gpu** (`vllm-openai` v0.31.0): `deepseek-ai/DeepSeek-R1-0528`, no `--max-model-len` (163840, derived from the model's config, until a run's `/v1/models` confirms it), `--tool-call-parser deepseek_v3 --enable-auto-tool-choice` with vLLM's `tool_chat_template_deepseekr1.jinja`, and no reasoning parser on purpose, so `<think>` reasoning arrives inline in `content`. The three provider overlays render identical vLLM arguments for both roles.
- **amd** (`vllm-openai-rocm` v0.32.0-rc.2): `deepseek-ai/DeepSeek-V3`, `--max-model-len 36864`, no parsers, prefix caching off. DeepSeek-V3 is not a reasoning model.
- **xpu** (`llm-d-xpu` v1.0.0): `deepseek-ai/DeepSeek-V2-Lite-Chat`, `MAX_MODEL_LEN` 2048, no parsers, aggregated (no P/D, no routing sidecar). The model part lists the cases whose budgets a 2048-token window cannot hold.

The router differs by accelerator (always-disaggregate on gpu and amd, one aggregated profile on xpu; see `parts/common.yaml`), but not in anything a client sees: the listener, APIs and header handling are the ones `../parts/topology/standalone.yaml` declares.

Run one after deploying the guide in standalone mode:

```sh
export LLMDV_EPP_URL=<the EPP's URL>
llmdv verify --profile profiles/llm-d/wide-ep/vllm-deepseek-r1-0528-tools-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

## Not covered

- **Overlays no lane runs:** `gpu/vllm/base`, `amd/vllm/base` (each the same to a client as its lane's overlay) and `xpu/vllm/base` (Qwen/Qwen3-235B-A22B with DP/EP across pods), and gateway mode.
- **The guide's mechanism checks** (`guide.yaml` `verify.tests`): the concurrent `dp_requests` batch needs load mode (#23); per-DP-rank spread and connector prefix-cache hits (`rank_metrics`) and the router's P/D decision counter (`router_metrics`) need per-pod metric reads through cluster access a committed profile does not carry, and a routing case family (#27). Rollout gating needs a rollout during the run. Each is mapped in `parts/common.yaml`.
- **Prefix reuse:** no overlay passes `--enable-prompt-tokens-details`, so `cache_reuse` is not declared.

## Upstream notes

Facts at llm-d 9ca8ff0 that do not agree with each other:

- `ibm-acc-gpu-vllm-x` deploys `INFRA_PROVIDER=ocp` (`guides/wide-ep/modelserver/gpu/vllm/ocp/`), but `guides/wide-ep/guide.yaml` lists only `base`, `gke` and `coreweave` as gpu providers, and its `INFRA_PROVIDER` values omit `ocp`.
- `intel-acc-xpu-vllm-x` deploys `xpu/vllm/xpu-ci`, which serves `deepseek-ai/DeepSeek-V2-Lite-Chat` (TP2 with expert parallelism within each pod, no data parallelism; its kustomization says CI has no RDMA network), while `guide.yaml` and the README's support table advertise `Qwen/Qwen3-235B-A22B` on 16 XPUs with DP2/EP across pods and list only `base` as an xpu provider. `xpu-ci` is in `INFRA_PROVIDER`'s values but not in the xpu provider list. The xpu-ci LeaderWorkerSet keeps the name `wide-ep-xpu-qwen3-235b`.
- The README's overview says the Intel XPU configuration uses "two plain `LeaderWorkerSet`s"; the support table and `modelserver/xpu/vllm/base/lws.yaml` have one aggregated `LeaderWorkerSet` of two pods.
- The xpu lane is aggregated, so `guide.yaml`'s mechanism text (every request disaggregated) and `router/wide-ep.values.yaml`'s P/D plugins do not apply to it; `router/xpu.values.yaml` replaces them.
