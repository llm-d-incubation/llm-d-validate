# optimized-baseline

Profiles for llm-d's [optimized-baseline](https://github.com/llm-d/llm-d/tree/main/guides/optimized-baseline) guide. See [../README.md](../README.md) for how to run a combination, and the [profile skill](../../../.agents/skills/writing-an-infra-profile/SKILL.md#how-profilesllm-d-is-built) for the layout and provenance tags, and its [optimized-baseline coverage](../README.md#optimized-baseline-coverage) section for why the lanes group as they do.

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

These lanes run the guide's `test_sets.default` (`parts/common.yaml`). The lanes that run every case with `--full-suite`, one per configuration, are listed in [Which lane runs everything](../README.md#which-lane-runs-everything).

## What `llmdv verify` selects

This applies to every runnable profile in this folder: `atom-qwen3-32b-ctxunknown-inline-standalone.yaml`, `sglang-qwen3-32b-ctx16k-parsers-standalone.yaml`, `sglang-qwen3-32b-inline-standalone.yaml`, `sglang-qwen3-32b-parsers-standalone.yaml`, `sim-qwen3-32b-standalone.yaml`, `vllm-qwen3-0.6b-inline-standalone.yaml`, `vllm-qwen3-32b-inline-standalone.yaml`, `vllm-qwen3-32b-parsers-standalone.yaml`.

**Default.** A `plan`, `verify` or `job` with no scope flag runs the `default` test set. It is defined once, in [`parts/common.yaml`](parts/common.yaml), and every profile above inherits it unchanged; none redefines it. There is no other profile-defined set.

**The `default` set's case-ID patterns, by the suite they select from** (11 suites, each confirmed by `llmdv plan`). A pattern ending in `.*` takes every case of its suite; any other names one case, or one variant in brackets. The rest of each suite is not selected:

| Suite | Patterns |
| --- | --- |
| `contract` | `contract.*` |
| `discovery` | `discovery.models_lists_declared_model`, `discovery.unknown_model_no_fallback`, `discovery.models_parses_with_openai_sdk` |
| `openai_chat` | `openai_chat.basic_non_streaming`, `openai_chat.streaming_sse`, `openai_chat.stop_reason`, `openai_chat.configured_reasoning` |
| `openai_completions` | `openai_completions.basic_non_streaming`, `openai_completions.streaming_matches_json`, `openai_completions.stop_reason`, `openai_completions.prompt_forms` |
| `anthropic_messages` | `anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse` |
| `openai_responses` | `openai_responses.basic_non_streaming`, `openai_responses.streaming_sse` |
| `streaming` | `streaming.sse_framing`, `streaming.*` |
| `unhappy` | `unhappy.unknown_path`, `unhappy.malformed_json`, `unhappy.missing_model`, `unhappy.wrong_method`, `unhappy.error_envelope_fields`, `unhappy.context_overflow`, `unhappy.token_limit_over_context` |
| `transport` | `transport.connection`, `transport.health_status` |
| `tools` | `tools.auto_call`, `tools.auto_stream` |
| `long_context` | `long_context.*` |

**Named sets:** `default` (above) and the built-in `smoke`, which every profile has ([its cases](../../README.md#what-a-verification-command-selects)). `--all-test-sets` combines the profile-defined sets only, so here it selects exactly `default`; `smoke` is never included in it.

**What each scope selects**, as `llmdv plan` counts it offline. Counts are case instances (one per ingress and variant). *Run* means the profile's declarations, capabilities and fleet policy let the case run; a case can still skip during the run (throttled, `requires_instrumentation`, or a declaration it reads). *Skip* names the skip classes:

| Profile | no flag, or `--test-set default`, or `--all-test-sets` | `--test-set smoke` | `--full-suite` (every case in the library) |
| --- | --- | --- | --- |
| `atom-qwen3-32b-ctxunknown-inline-standalone.yaml` | 106 selected, 94 run, 12 skip (12 undeclared) | 9 selected, 9 run | 1082 selected, 677 run, 405 skip (405 undeclared) |
| `sglang-qwen3-32b-ctx16k-parsers-standalone.yaml` | 106 selected, 100 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1082 selected, 911 run, 171 skip (171 undeclared) |
| `sglang-qwen3-32b-inline-standalone.yaml` | 106 selected, 94 run, 12 skip (12 undeclared) | 9 selected, 9 run | 1082 selected, 677 run, 405 skip (405 undeclared) |
| `sglang-qwen3-32b-parsers-standalone.yaml` | 106 selected, 100 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1082 selected, 911 run, 171 skip (171 undeclared) |
| `sim-qwen3-32b-standalone.yaml` | 106 selected, 92 run, 14 skip (14 undeclared) | 9 selected, 9 run | 1082 selected, 647 run, 435 skip (435 undeclared) |
| `vllm-qwen3-0.6b-inline-standalone.yaml` | 106 selected, 94 run, 12 skip (12 undeclared) | 9 selected, 9 run | 1082 selected, 677 run, 405 skip (405 undeclared) |
| `vllm-qwen3-32b-inline-standalone.yaml` | 106 selected, 94 run, 12 skip (12 undeclared) | 9 selected, 9 run | 1082 selected, 677 run, 405 skip (405 undeclared) |
| `vllm-qwen3-32b-parsers-standalone.yaml` | 106 selected, 100 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1082 selected, 911 run, 171 skip (171 undeclared) |

Other filters (`-k`, `-m`, `--surface`, `--client`, `--ingress`) only narrow the chosen scope, and a filter that leaves no case exits 2. `--identity` flags label the run and never change what it selects.

To inspect the definitions, the cases and every skip reason, offline:

```sh
P=profiles/llm-d/optimized-baseline/atom-qwen3-32b-ctxunknown-inline-standalone.yaml
llmdv profile show $P --test-sets        # every set and its patterns
llmdv plan --profile $P                  # the default: each case, and why one skips
llmdv plan --profile $P --test-set smoke
llmdv plan --profile $P --full-suite
llmdv plan --profile $P --json           # the same, as JSON
```
## Not covered, and why

See [optimized-baseline coverage](../README.md#optimized-baseline-coverage): the community overlays, gateway mode and agentgateway as the standalone sidecar proxy are not run by any lane.
