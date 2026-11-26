# pod-snapshot

Profiles for testing a deployment of llm-d's [pod-snapshot](https://github.com/llm-d/llm-d/tree/main/guides/pod-snapshot) guide, in which one replica cold starts and is checkpointed, and a second replica is restored from that snapshot instead of cold starting.

[What it tests](#what-it-tests) · [Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## What it tests

**Validation focus: serving correctness with restored replicas in the pool.** Exercise the API contract after restore to detect user-visible regressions in the deployed pool.

**What a pass establishes:** the checks that actually executed held at the reachable ingress
during this run. Skipped checks remain coverage gaps. **Coverage boundary:** Responses do not identify whether a cold-started or restored replica answered; snapshot creation and restore need cluster evidence.

These focused defaults are a starting point for validating a production deployment. Add coverage
for the APIs, clients and operating conditions you rely on; see
[Growing coverage toward production](../../../docs/getting-started.md#widen-coverage).

<details>
<summary>Why these checks relate to the guide</summary>

Nothing beyond the base tests every guide gets (each API, error handling and the context window): snapshot and restore are cluster state a client cannot see. With both replicas Ready behind the router, the run reaches a cold-started and a restored replica alike, so it checks that the stack serves correctly after a restore, though no response says which replica answered: see [Not covered](#not-covered).

The declaration rationale and claim-to-case mapping live in
[`parts/common.yaml`](parts/common.yaml). [Not covered](#not-covered) explains the remaining
access, instrumentation and load requirements.

</details>

## Pick your profile

Pick the profile for the engine you deployed (`MODEL_SERVER`, under `guides/pod-snapshot/modelserver/`). The guide runs on GKE only.

| Model a client sees | Overlays | Profile |
| --- | --- | --- |
| Qwen3-32B on vLLM, 8k window, reasoning inline, no tool parser | `gpu/vllm/gke` | [vllm-qwen3-32b-ctx8k-inline-standalone](vllm-qwen3-32b-ctx8k-inline-standalone.yaml) |
| Qwen3-32B on SGLang, 8k window, reasoning inline, no tool parser | `gpu/sglang/gke` | [sglang-qwen3-32b-ctx8k-inline-standalone](sglang-qwen3-32b-ctx8k-inline-standalone.yaml) |

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL='https://your-router.example' # replace with your router's URL
llmdv verify --profile 'profiles/llm-d/pod-snapshot/<profile>.yaml'
```

Run it once both replicas are Ready, so the run's requests reach the cold-started one and the restored one. To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional for verification. Set them when comparing deployments that share a profile, so their recorded configuration dimensions remain distinct; for example `--identity accelerator=gpu --identity provider=gke`, read off your overlay path.


## What `llmdv verify` selects

The default is defined once in [`parts/common.yaml`](parts/common.yaml) and used unchanged by every profile here.
See [selection rules](../../../docs/cli.md#test-sets-and-coverage) for smoke, full-library coverage and filters.

<details>
<summary>The default's case-ID patterns</summary>

`*` selects the whole library; `suite.*` selects a suite. Other patterns select matching cases.

**contract**

- `contract.*`

**discovery**

- `discovery.models_lists_declared_model`
- `discovery.unknown_model_no_fallback`
- `discovery.models_parses_with_openai_sdk`

**openai_chat**

- `openai_chat.basic_non_streaming`
- `openai_chat.streaming_sse`
- `openai_chat.stop_reason`
- `openai_chat.configured_reasoning`

**openai_completions**

- `openai_completions.basic_non_streaming`
- `openai_completions.streaming_matches_json`
- `openai_completions.stop_reason`

**anthropic_messages**

- `anthropic_messages.basic_non_streaming`
- `anthropic_messages.streaming_sse`

**openai_responses**

- `openai_responses.basic_non_streaming`
- `openai_responses.streaming_sse`

**streaming**

- `streaming.sse_framing`

**unhappy**

- `unhappy.unknown_path`
- `unhappy.malformed_json`
- `unhappy.missing_model`
- `unhappy.wrong_method`
- `unhappy.error_envelope_fields`
- `unhappy.context_overflow`
- `unhappy.token_limit_over_context`

**transport**

- `transport.connection`
- `transport.health_status`

**tools**

- `tools.auto_call`
- `tools.auto_stream`

**long_context**

- `long_context.*`


</details>

Counted offline by `llmdv plan`, one case per ingress and variant. Planned runs can still skip at runtime. Expand a profile for its scope counts and separate skip classes.

The default is also `--all-test-sets` for these profiles.

<details>
<summary>sglang-qwen3-32b-ctx8k-inline-standalone.yaml</summary>

[Open the profile](sglang-qwen3-32b-ctx8k-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 85 | 73 | 12 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 678 | 414 |

Planned skips by class:

- **Default:** 12 `undeclared`.
- **Full library:** 414 `undeclared`.

</details>

<details>
<summary>vllm-qwen3-32b-ctx8k-inline-standalone.yaml</summary>

[Open the profile](vllm-qwen3-32b-ctx8k-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 85 | 73 | 12 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 678 | 414 |

Planned skips by class:

- **Default:** 12 `undeclared`.
- **Full library:** 414 `undeclared`.

</details>

Preview cases and reasons with `llmdv plan --profile <profile>`.

## Not covered

- **Snapshot and restore** (a PodSnapshot reaches Ready; Pod 2 restores rather than cold starts; GPU memory is released before the checkpoint and woken after): cluster state and pod logs, which a committed profile does not read. `llmdv` never scales or restarts a deployment, so it cannot drive a restore either.
- **Startup and restore timing** (`scripts/check_thresholds.py`, `benchmark-results/`): timing is a benchmark's measurement, not `llmdv`'s.
- **Which replica answered**: a restored replica should serve exactly like a cold-started one, and the run's requests reach both through the router, but no response says which replica served it, so `llmdv` cannot compare them.
- **Single GPU, TP=1**, and the engine flags: fleet configuration rules need cluster access.
- **SGLang's startup `/health` 503**: startup behaviour; `llmdv` runs against a Ready stack.
- **The cache suite**: neither overlay asks its engine to report `cached_tokens`, so `cache_reuse` is not declared (see `parts/common.yaml`).
- **Gateway mode**: `guide.yaml` has a gateway deploy step, but these profiles target the standalone router.

## For maintainers

<details>
<summary>llm-d's nightly workflows, and how each maps to a profile</summary>

The guide has no support table; `guide.yaml` offers `MODEL_SERVER` `vllm` or `sglang`, on GKE only. Both nightly workflows deploy through `guides/pod-snapshot/scripts/nightly-deploy-gke.sh`, which applies `modelserver/gpu/<MODEL_SERVER>/gke` and finishes with two Ready replicas: one cold started and checkpointed, one restored from that snapshot. The two engines differ to a client, so each has its own combination. Both serve `Qwen/Qwen3-32B` at an 8192-token window with no parsers.

| upstream workflow (`nightly-e2e-pod-snapshot-<workflow-suffix>`) | Overlay the deploy script applies | Profile | Identity flags |
| --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `modelserver/gpu/vllm/gke` (vLLM v0.31.0, `--max-model-len 8192`, `--enable-sleep-mode`) | `vllm-qwen3-32b-ctx8k-inline-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `gke-acc-gpu-sglang-x` | `modelserver/gpu/sglang/gke` (SGLang v0.5.22, `--context-length 8192`, `--enable-memory-saver --enable-weights-cpu-backup`) | `sglang-qwen3-32b-ctx8k-inline-standalone` | `--identity accelerator=gpu --identity provider=gke` |

The client calls the `<GUIDE_NAME>-epp` Service on port 80 (the workflows' `gateway_host`), in standalone mode. Run `llmdv` after the deploy script returns, so the run reaches both replicas.

</details>

<details>
<summary>How these profiles are built</summary>

See [../README.md](../README.md) for how to run a combination, and the [profile provenance guide](../../../docs/profile-provenance.md#how-profilesllm-d-is-built) for the layout and provenance tags.

</details>

<details>
<summary>Where llm-d's own sources disagree</summary>

None found beyond what the guide documents itself.

</details>
