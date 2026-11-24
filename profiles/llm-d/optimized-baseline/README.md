# optimized-baseline

Profiles for llm-d's [optimized-baseline](https://github.com/llm-d/llm-d/tree/main/guides/optimized-baseline) guide, written against llm-d `9ca8ff0`. Status: written, not run. See [../README.md](../README.md) for how to run a combination, and the [profile skill](../../../.agents/skills/writing-an-infra-profile/SKILL.md#how-profilesllm-d-is-built) for the layout and provenance tags, and its [optimized-baseline coverage](../README.md#optimized-baseline-coverage) section for why the lanes group as they do.

## Lanes

Every lane runs standalone: the client calls the `<GUIDE_NAME>-epp` Service on port 80 (the lanes' `gateway_host`). Lanes that share a profile are told apart by `--identity`; the provider value is the overlay's `INFRA_PROVIDER` folder, and `base` for a lane that sets none, which applies the `base` overlay.

| CI lane (`nightly-e2e-optimized-baseline-<lane>`) | Overlay it deploys (`ACCELERATOR_TYPE/MODEL_SERVER/INFRA_PROVIDER`) | Profile | Identity flags |
| --- | --- | --- | --- |
| `cks-acc-gpu-vllm-x` | `gpu/vllm/base` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=base` |
| `gke-acc-gpu-vllm-x` | `gpu/vllm/gke` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `ibm-acc-gpu-vllm-x` | `gpu/vllm/ocp` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=ocp` |
| `amd-acc-rocm-vllm-x` | `amd/vllm/amd-ci` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=amd-ci` |
| `gke-acc-tpu-vllm-x` | `tpu/v5p/vllm` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=tpu/v5p --identity provider=base` |
| `intel-acc-xpu-vllm-x` | `xpu/vllm` | `vllm-qwen3-0.6b-inline-standalone` | `--identity accelerator=xpu --identity provider=base` |
| `gke-acc-gpu-sglang-x` | `gpu/sglang/gke` | `sglang-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `gke-acc-tpu-sglang-x` | `tpu/v5p/sglang` | `sglang-qwen3-32b-ctx16k-parsers-standalone` | `--identity accelerator=tpu/v5p --identity provider=base` |
| `amd-acc-rocm-sglang-x` | `amd/sglang/amd-ci` | `sglang-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=amd-ci` |
| `amd-acc-rocm-atom-x` | `amd/atom/amd-ci` | `atom-qwen3-32b-ctxunknown-inline-standalone` | `--identity accelerator=amd --identity provider=amd-ci` |

`sim-qwen3-32b-standalone` is not a lane: it runs the guide's router in front of llm-d-inference-sim, for kind or CI without accelerators.

These lanes run the guide's `selection` (`parts/common.yaml`). The lanes that run every case with `--all`, one per configuration, are listed in [Which lane runs everything](../README.md#which-lane-runs-everything).

## Not covered, and why

See [optimized-baseline coverage](../README.md#optimized-baseline-coverage): the community overlays, gateway mode and agentgateway as the standalone sidecar proxy are not run by any lane.
