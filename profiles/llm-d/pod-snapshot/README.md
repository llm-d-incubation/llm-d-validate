# pod-snapshot

Profiles for llm-d's [pod-snapshot](https://github.com/llm-d/llm-d/tree/main/guides/pod-snapshot) guide. See [../README.md](../README.md) for how to run a combination, and the [profile skill](../../../.agents/skills/writing-an-infra-profile/SKILL.md#how-profilesllm-d-is-built) for the layout and provenance tags.

## Lanes

The guide has no support table; `guide.yaml` offers `MODEL_SERVER` `vllm` or `sglang`, on GKE only. Both nightly lanes deploy through `guides/pod-snapshot/scripts/nightly-deploy-gke.sh`, which applies `modelserver/gpu/<MODEL_SERVER>/gke` and finishes with two Ready replicas: one cold started and checkpointed, one restored from that snapshot. The two engines differ to a client, so each has its own combination. Both serve `Qwen/Qwen3-32B` at an 8192-token window with no parsers.

| CI lane (`nightly-e2e-pod-snapshot-<lane>`) | Overlay the deploy script applies | Profile | Identity flags |
| --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `modelserver/gpu/vllm/gke` (vLLM v0.31.0, `--max-model-len 8192`, `--enable-sleep-mode`) | `vllm-qwen3-32b-ctx8k-inline-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `gke-acc-gpu-sglang-x` | `modelserver/gpu/sglang/gke` (SGLang v0.5.22, `--context-length 8192`, `--enable-memory-saver --enable-weights-cpu-backup`) | `sglang-qwen3-32b-ctx8k-inline-standalone` | `--identity accelerator=gpu --identity provider=gke` |

The client calls the `<GUIDE_NAME>-epp` Service on port 80 (the lanes' `gateway_host`), in standalone mode. Run `llmdv` after the deploy script returns, so the run reaches both replicas.

## What `llmdv verify` selects

This applies to every runnable profile in this folder: `sglang-qwen3-32b-ctx8k-inline-standalone.yaml`, `vllm-qwen3-32b-ctx8k-inline-standalone.yaml`.

**Default.** A `plan`, `verify` or `job` with no scope flag runs the `default` test set. It is defined once, in [`parts/common.yaml`](parts/common.yaml), and every profile above inherits it unchanged; none redefines it. There is no other profile-defined set.

**The `default` set's case-ID patterns, by the suite they select from** (11 suites, each confirmed by `llmdv plan`). A pattern ending in `.*` takes every case of its suite; any other names one case, or one variant in brackets. The rest of each suite is not selected:

| Suite | Patterns |
| --- | --- |
| `contract` | `contract.*` |
| `discovery` | `discovery.models_lists_declared_model`, `discovery.unknown_model_no_fallback`, `discovery.models_parses_with_openai_sdk` |
| `openai_chat` | `openai_chat.basic_non_streaming`, `openai_chat.streaming_sse`, `openai_chat.stop_reason`, `openai_chat.configured_reasoning` |
| `openai_completions` | `openai_completions.basic_non_streaming`, `openai_completions.streaming_matches_json`, `openai_completions.stop_reason` |
| `anthropic_messages` | `anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse` |
| `openai_responses` | `openai_responses.basic_non_streaming`, `openai_responses.streaming_sse` |
| `streaming` | `streaming.sse_framing` |
| `unhappy` | `unhappy.unknown_path`, `unhappy.malformed_json`, `unhappy.missing_model`, `unhappy.wrong_method`, `unhappy.error_envelope_fields`, `unhappy.context_overflow`, `unhappy.token_limit_over_context` |
| `transport` | `transport.connection`, `transport.health_status` |
| `tools` | `tools.auto_call`, `tools.auto_stream` |
| `long_context` | `long_context.*` |

**Named sets:** `default` (above) and the built-in `smoke`, which every profile has ([its cases](../../README.md#what-a-verification-command-selects)). `--all-test-sets` combines the profile-defined sets only, so here it selects exactly `default`; `smoke` is never included in it.

**What each scope selects**, as `llmdv plan` counts it offline. Counts are case instances (one per ingress and variant). *Run* means the profile's declarations, capabilities and fleet policy let the case run; a case can still skip during the run (throttled, `requires_instrumentation`, or a declaration it reads). *Skip* names the skip classes:

| Profile | no flag, or `--test-set default`, or `--all-test-sets` | `--test-set smoke` | `--full-suite` (every case in the library) |
| --- | --- | --- | --- |
| `sglang-qwen3-32b-ctx8k-inline-standalone.yaml` | 85 selected, 73 run, 12 skip (12 undeclared) | 9 selected, 9 run | 1082 selected, 677 run, 405 skip (405 undeclared) |
| `vllm-qwen3-32b-ctx8k-inline-standalone.yaml` | 85 selected, 73 run, 12 skip (12 undeclared) | 9 selected, 9 run | 1082 selected, 677 run, 405 skip (405 undeclared) |

Other filters (`-k`, `-m`, `--surface`, `--client`, `--ingress`) only narrow the chosen scope, and a filter that leaves no case exits 2. `--identity` flags label the run and never change what it selects.

To inspect the definitions, the cases and every skip reason, offline:

```sh
P=profiles/llm-d/pod-snapshot/sglang-qwen3-32b-ctx8k-inline-standalone.yaml
llmdv profile show $P --test-sets        # every set and its patterns
llmdv plan --profile $P                  # the default: each case, and why one skips
llmdv plan --profile $P --test-set smoke
llmdv plan --profile $P --full-suite
llmdv plan --profile $P --json           # the same, as JSON
```
## Not covered, and why

- **Snapshot and restore** (a PodSnapshot reaches Ready; Pod 2 restores rather than cold starts; GPU memory is released before the checkpoint and woken after): cluster state and pod logs, which a committed profile does not read. `llmdv` never scales or restarts a deployment, so it cannot drive a restore either.
- **Startup and restore timing** (`scripts/check_thresholds.py`, `benchmark-results/`): timing is a benchmark's measurement, not `llmdv`'s.
- **Which replica answered**: a restored replica should serve exactly like a cold-started one, and the run's requests reach both through the router, but no response says which replica served it, so `llmdv` cannot compare them.
- **Single GPU, TP=1**, and the engine flags: fleet configuration rules need cluster access.
- **SGLang's startup `/health` 503**: startup behaviour; `llmdv` runs against a Ready stack.
- **The cache suite**: neither overlay asks its engine to report `cached_tokens`, so `cache_reuse` is not declared (see `parts/common.yaml`).
- **Gateway mode**: `guide.yaml` has a gateway deploy step, but no lane runs it.

## Upstream notes

None found beyond what the guide documents itself.
