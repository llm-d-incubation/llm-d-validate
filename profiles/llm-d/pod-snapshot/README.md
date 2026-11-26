# pod-snapshot

Profiles for testing a deployment of llm-d's [pod-snapshot](https://github.com/llm-d/llm-d/tree/main/guides/pod-snapshot) guide, in which one replica cold starts and is checkpointed, and a second replica is restored from that snapshot instead of cold starting.

[Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What it tests](#what-it-tests) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## Pick your profile

Pick the profile for the engine you deployed (`MODEL_SERVER`, under `guides/pod-snapshot/modelserver/`). The guide runs on GKE only.

| Profile | Model a client sees | Overlays |
| --- | --- | --- |
| `vllm-qwen3-32b-ctx8k-inline-standalone` | Qwen3-32B on vLLM, 8k window, reasoning inline, no tool parser | `gpu/vllm/gke` |
| `sglang-qwen3-32b-ctx8k-inline-standalone` | Qwen3-32B on SGLang, 8k window, reasoning inline, no tool parser | `gpu/sglang/gke` |

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/pod-snapshot/<profile>.yaml
```

Run it once both replicas are Ready, so the run's requests reach the cold-started one and the restored one. To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional: they label a run so `llmdv compare` never compares two configurations, for example `--identity accelerator=gpu --identity provider=gke`, read off your overlay path.

## What it tests

Nothing beyond the base tests every guide gets (each API, error handling and the context window): snapshot and restore are cluster state a client cannot see. With both replicas Ready behind the router, the run reaches a cold-started and a restored replica alike, so it checks that the stack serves correctly after a restore, though no response says which replica answered: see [Not covered](#not-covered).

## What `llmdv verify` selects

The default is defined once in [`parts/common.yaml`](parts/common.yaml) and used unchanged by every profile here.
See [selection rules](../../../docs/cli.md#test-sets-and-coverage) for smoke, full-library coverage and filters.

<details>
<summary>The default's case-ID patterns</summary>

`*` selects the whole library; `suite.*` selects a suite. Other patterns select matching cases.

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

</details>

<details>
<summary>How many cases each scope selects, per profile</summary>

Counted offline by `llmdv plan`, one per ingress and variant. *Run* cases can still skip during a run (for example `throttled`); *skip* cases are ruled out by the profile's declarations, with their skip class.

| Profile | Default (= `--all-test-sets`) | `smoke` | `--full-suite` |
| --- | --- | --- | --- |
| `sglang-qwen3-32b-ctx8k-inline-standalone.yaml` | 85 selected, 73 run, 12 skip: 12 undeclared | 9 selected, 9 run | 1092 selected, 678 run, 414 skip: 414 undeclared |
| `vllm-qwen3-32b-ctx8k-inline-standalone.yaml` | 85 selected, 73 run, 12 skip: 12 undeclared | 9 selected, 9 run | 1092 selected, 678 run, 414 skip: 414 undeclared |

</details>

Preview individual cases and skip reasons with `llmdv plan --profile <profile>`.

## Not covered

- **Snapshot and restore** (a PodSnapshot reaches Ready; Pod 2 restores rather than cold starts; GPU memory is released before the checkpoint and woken after): cluster state and pod logs, which a committed profile does not read. `llmdv` never scales or restarts a deployment, so it cannot drive a restore either.
- **Startup and restore timing** (`scripts/check_thresholds.py`, `benchmark-results/`): timing is a benchmark's measurement, not `llmdv`'s.
- **Which replica answered**: a restored replica should serve exactly like a cold-started one, and the run's requests reach both through the router, but no response says which replica served it, so `llmdv` cannot compare them.
- **Single GPU, TP=1**, and the engine flags: fleet configuration rules need cluster access.
- **SGLang's startup `/health` 503**: startup behaviour; `llmdv` runs against a Ready stack.
- **The cache suite**: neither overlay asks its engine to report `cached_tokens`, so `cache_reuse` is not declared (see `parts/common.yaml`).
- **Gateway mode**: `guide.yaml` has a gateway deploy step, but llm-d does not test it nightly.

## For maintainers

<details>
<summary>llm-d's nightly lanes, and how each maps to a profile</summary>

The guide has no support table; `guide.yaml` offers `MODEL_SERVER` `vllm` or `sglang`, on GKE only. Both nightly lanes deploy through `guides/pod-snapshot/scripts/nightly-deploy-gke.sh`, which applies `modelserver/gpu/<MODEL_SERVER>/gke` and finishes with two Ready replicas: one cold started and checkpointed, one restored from that snapshot. The two engines differ to a client, so each has its own combination. Both serve `Qwen/Qwen3-32B` at an 8192-token window with no parsers.

| CI lane (`nightly-e2e-pod-snapshot-<lane>`) | Overlay the deploy script applies | Profile | Identity flags |
| --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `modelserver/gpu/vllm/gke` (vLLM v0.31.0, `--max-model-len 8192`, `--enable-sleep-mode`) | `vllm-qwen3-32b-ctx8k-inline-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `gke-acc-gpu-sglang-x` | `modelserver/gpu/sglang/gke` (SGLang v0.5.22, `--context-length 8192`, `--enable-memory-saver --enable-weights-cpu-backup`) | `sglang-qwen3-32b-ctx8k-inline-standalone` | `--identity accelerator=gpu --identity provider=gke` |

The client calls the `<GUIDE_NAME>-epp` Service on port 80 (the lanes' `gateway_host`), in standalone mode. Run `llmdv` after the deploy script returns, so the run reaches both replicas.

</details>

<details>
<summary>How these profiles are built</summary>

See [../README.md](../README.md) for how to run a combination, and the [profile skill](../../../.agents/skills/writing-an-infra-profile/SKILL.md#how-profilesllm-d-is-built) for the layout and provenance tags.

</details>

<details>
<summary>Where llm-d's own sources disagree</summary>

None found beyond what the guide documents itself.

</details>
