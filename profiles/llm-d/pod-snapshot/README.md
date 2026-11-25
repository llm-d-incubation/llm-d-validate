# pod-snapshot

Profiles for llm-d's [pod-snapshot](https://github.com/llm-d/llm-d/tree/main/guides/pod-snapshot) guide. See [../README.md](../README.md) for how to run a combination, and the [profile skill](../../../.agents/skills/writing-an-infra-profile/SKILL.md#how-profilesllm-d-is-built) for the layout and provenance tags.

## Lanes

The guide has no support table; `guide.yaml` offers `MODEL_SERVER` `vllm` or `sglang`, on GKE only. Both nightly lanes deploy through `guides/pod-snapshot/scripts/nightly-deploy-gke.sh`, which applies `modelserver/gpu/<MODEL_SERVER>/gke` and finishes with two Ready replicas: one cold started and checkpointed, one restored from that snapshot. The two engines differ to a client, so each has its own combination. Both serve `Qwen/Qwen3-32B` at an 8192-token window with no parsers.

| CI lane (`nightly-e2e-pod-snapshot-<lane>`) | Overlay the deploy script applies | Profile | Identity flags |
| --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `modelserver/gpu/vllm/gke` (vLLM v0.31.0, `--max-model-len 8192`, `--enable-sleep-mode`) | `vllm-qwen3-32b-ctx8k-inline-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `gke-acc-gpu-sglang-x` | `modelserver/gpu/sglang/gke` (SGLang v0.5.22, `--context-length 8192`, `--enable-memory-saver --enable-weights-cpu-backup`) | `sglang-qwen3-32b-ctx8k-inline-standalone` | `--identity accelerator=gpu --identity provider=gke` |

The client calls the `<GUIDE_NAME>-epp` Service on port 80 (the lanes' `gateway_host`), in standalone mode. Run `llmdv` after the deploy script returns, so the run reaches both replicas.

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
