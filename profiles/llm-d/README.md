# llm-d guide profiles

Profiles for the [llm-d](https://github.com/llm-d/llm-d) guides (llm-d's docs also say "well-lit
paths"; this repository says guide). Each states what a correct stack does when a guide is
deployed as shipped with one model-server overlay, for every configuration a nightly lane deploys
at llm-d `9ca8ff0`.

They are data on top of `llmdv`: nothing in `src/` knows about them, and any other stack is
described by its own profile ([profiles/README.md](../README.md)). How the folders are built and
where each value comes from:
[the profile skill](../../.agents/skills/writing-an-infra-profile/SKILL.md#how-profilesllm-d-is-built).

## Run one

Deploy the guide with an overlay a combination covers (its header lists them), give `llmdv` the
router's URL, and run:

```sh
export LLMDV_EPP_URL=<your EPP's URL>
llmdv profile validate profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml
llmdv verify --profile profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

The EPP needs no key. If a gateway already sits in front of it, name its URL as one more ingress in
a local file that extends the combination: [test through your gateway](../README.md#test-through-your-gateway).

A combination is `<engine>-<model-config>[-<variant>]-<topology>.yaml`, with a variant only where
lanes of one configuration differ to a client (keda-epp's `queue` and `inflight`); files in a `parts/` folder never run
on their own. The guide folder's README maps each lane's overlay to its combination and gives the
exact `--identity` flags, which keep apart lanes that share a profile. They follow llm-d's lane
inputs:

| Flag | Value |
| --- | --- |
| `accelerator` | the guide's `ACCELERATOR_TYPE` |
| `provider` | the `INFRA_PROVIDER` folder the lane renders, with any connector the guide folds into it (`nixl/amd-ci`); `base` when the lane sets none |
| `flavor` | every other variable the guide's lanes vary (connector, variant, topology, scaler), llm-d's values joined with `/` in overlay-path order (`lmcache-connector/cpu`), passed on every lane of that guide |

## Coverage

Every guide llm-d runs in a nightly lane (`.github/workflows/nightly-e2e-*.yaml` at `9ca8ff0`;
keda-epp's are `nightly-e2e-workload-autoscaling-keda-epp-*`) has a folder here, named as its `guide.yaml` names it. Lanes that look the same to a
client share a profile.

| Guide | Lanes | Profiles | Notes |
| --- | --- | --- | --- |
| [optimized-baseline](optimized-baseline/) | 10 | 7, plus a simulator profile | see [below](#optimized-baseline-coverage) |
| [pd-disaggregation](pd-disaggregation/README.md) | 8 | 5 | requests pass the decode pod's routing sidecar |
| [wide-ep](wide-ep/README.md) | 5 | 3 | the XPU lane runs aggregated, without prefill/decode |
| [precise-prefix-cache-routing](precise-prefix-cache-routing/README.md) | 6 | 4 | |
| [predicted-latency-routing](predicted-latency-routing/README.md) | 5 | 2 | |
| [tiered-prefix-cache](tiered-prefix-cache/README.md) | 15 | 5 | `flavor` names the connector and tier |
| [multimodal-serving](multimodal-serving/README.md) | 3 | 1 | `flavor` names the topology |
| [pod-snapshot](pod-snapshot/README.md) | 2 | 2 | |
| [fast-model-actuation](fast-model-actuation/README.md) | 2 | 1 | |
| [flow-control](flow-control/README.md) | 2 | 2 | |
| [agentic-api](agentic-api/README.md) | 1 | 1 | the router only |
| [async-multitenant](async-multitenant/README.md) | 2 | 1 | the router only; `flavor` names evictable or holdback |
| [keda-epp](keda-epp/README.md) | 3 | 3 | `flavor` names the scaling signal; the router runs flow control on the queue lanes only |

Every profile tests at the router (`<release>-epp`, standalone mode), the only layer every lane
exposes the same way. None has run against a real engine: each says `WRITTEN, NOT RUN`.

### Not covered

- **fast-model-actuation-keda** (2 lanes, #130). The pool scales from zero on the router's
  flow-control queue size only (`minReplicaCount: 0`, `cooldownPeriod: 30`; llm-d 9ca8ff0
  `guides/fast-model-actuation/fast-model-actuation-keda/keda/base/kustomization.yaml`), and
  `llmdv`'s one-at-a-time requests never queue. So the pool can drop to zero mid-run and
  cold-start, holding a request up to the router's `noEndpointRequestTTL` of 300 s
  (`guides/fast-model-actuation/fast-model-actuation-keda/router/fast-model-actuation-keda.values.yaml`).
  `llmdv` has no warm-up; tolerating that means raising `stream_idle` and `stream_budget`
  ([parts/topology/standalone.yaml](parts/topology/standalone.yaml)) for every streaming check.
- **ci-async-kind-e2e** runs on pull requests and by hand, never nightly (llm-d 9ca8ff0
  `.github/workflows/ci-async-kind-e2e.yaml`). Its temporary kind cluster (optimized-baseline's
  router over a CPU vLLM serving `facebook/opt-125m` at `--max_model_len=2048`, plus Redis and
  llm-d-async v1.0.0; `guides/batch-serving/asynchronous-processing/test/kind-e2e.sh`) is deleted
  on exit, so there is no stack to run against. Its router is covered by
  [optimized-baseline/sim-qwen3-32b-standalone](optimized-baseline/sim-qwen3-32b-standalone.yaml).
- **Layers other than the router**: agentic-api's stateful Responses server and the async
  coordinator and its queues (in front of or beside it), and the prefill/decode routing sidecar
  (behind it). Each needs its own ingress, placed with `fronts`, and measured behaviour.
- **Claims that hold only under load or need cluster reads**: queueing, priority and fairness,
  eviction and shedding, scaling events, prefill/decode decisions, offload and cache counters,
  snapshot restore, prefix affinity (#23, #27).
- **Guides no nightly lane runs**: coord-disaggregation, p2p-kv-cache-sharing,
  multi-model-routing, omni-serving, turn-priority-fairness, modelexpress-p2p,
  hyperconverged-cephfs, the per-model guides under `guides/models/`, and the other
  workload-autoscaling variants. Each is added when a lane runs it.

## Which lane runs everything

A guide's `selection` (in its `parts/common.yaml`) is what its lanes run by default: the router
path, the overlay's parsers and window, and what the guide changes. The rest of the suite is
engine and model conformance, which depends only on the engine build, the model and its
client-visible flags, so one lane per configuration runs it with `llmdv verify --all` and the
others rely on that run. Where optimized-baseline deploys a configuration, its lane is the one.

Each profile below is `<guide>/<engine>-<model part>[-<variant>]-standalone`. GPU means NVIDIA GPU, XPU
Intel XPU.

| Profile that runs `--all` | Accelerator | Also deploys it (selection only) |
| --- | --- | --- |
| `wide-ep/vllm-deepseek-r1-0528-tools` | GPU | — |
| `wide-ep/vllm-deepseek-v2-lite-chat-ctx2k-inline` | XPU | — |
| `wide-ep/vllm-deepseek-v3-ctx36k-inline` | AMD | — |
| `pd-disaggregation/sglang-gpt-oss-120b-inline` | GPU | — |
| `pd-disaggregation/vllm-gpt-oss-120b-parsers` | GPU, AMD | — |
| `pd-disaggregation/vllm-qwen3-0.6b-ctx32000-inline` | XPU | — |
| `pd-disaggregation/vllm-qwen3-32b-ctx9k-parsers` | TPU | — |
| `fast-model-actuation/vllm-qwen3-0.6b-inline` | GPU | — |
| `predicted-latency-routing/vllm-qwen3-32b-ctx128k-parsers` | GPU, AMD | — |
| `precise-prefix-cache-routing/vllm-qwen3-32b-ctx4k-inline` | TPU | — |
| `pod-snapshot/sglang-qwen3-32b-ctx8k-inline` | GPU | — |
| `pod-snapshot/vllm-qwen3-32b-ctx8k-inline` | GPU | — |
| `tiered-prefix-cache/vllm-qwen3-32b-inline` | XPU | — |
| `tiered-prefix-cache/vllm-qwen3-8b-ctx20k-inline` | XPU | — |
| `multimodal-serving/vllm-qwen3-vl-32b-instruct-inline` | GPU, TPU | — |
| `optimized-baseline/atom-qwen3-32b-ctxunknown-inline` | AMD | — |
| `optimized-baseline/sglang-qwen3-32b-inline` | AMD | — |
| `optimized-baseline/sglang-qwen3-32b-ctx16k-parsers` | TPU | tiered-prefix-cache |
| `optimized-baseline/sglang-qwen3-32b-parsers` | GPU | tiered-prefix-cache |
| `optimized-baseline/vllm-qwen3-0.6b-inline` | XPU | precise-prefix-cache-routing, predicted-latency-routing |
| `optimized-baseline/vllm-qwen3-32b-inline` | AMD | flow-control, keda-epp, pd-disaggregation\*, precise-prefix-cache-routing, tiered-prefix-cache |
| `optimized-baseline/vllm-qwen3-32b-inline` | TPU | tiered-prefix-cache |
| `optimized-baseline/vllm-qwen3-32b-parsers` | GPU | agentic-api, async-multitenant, flow-control, keda-epp\*, precise-prefix-cache-routing, tiered-prefix-cache |

\* One of this guide's lanes for the configuration runs `--all` too. Two lanes run a different
engine build from the rest of their configuration, so each runs `--all`: pd-disaggregation's
`amd-ci-acc-rocm-vllm-moriio` (vllm-openai-rocm v0.32.0-rc.2, an llm-d build) and keda-epp's
`ibm-acc-gpu-vllm-x` (vLLM's floating `nightly` image). A lane whose engine image changes runs
`--all` once on the new build; `llmdv compare` reports the build change (`the stack changed`).

## optimized-baseline coverage

The guide's ten lanes (`nightly-e2e-optimized-baseline-<lane>.yaml` in llm-d, each in standalone
mode) deploy seven configurations that differ to a client, one profile each:

| CI lane | Overlay it deploys (`ACCELERATOR_TYPE/MODEL_SERVER/INFRA_PROVIDER`) | Profile (`optimized-baseline/`) |
| --- | --- | --- |
| `cks-acc-gpu-vllm-x` | `gpu/vllm/base` | `vllm-qwen3-32b-parsers-standalone` |
| `gke-acc-gpu-vllm-x` | `gpu/vllm/gke` | `vllm-qwen3-32b-parsers-standalone` |
| `ibm-acc-gpu-vllm-x` | `gpu/vllm/ocp` | `vllm-qwen3-32b-parsers-standalone` |
| `amd-acc-rocm-vllm-x` | `amd/vllm/amd-ci` | `vllm-qwen3-32b-inline-standalone` |
| `gke-acc-tpu-vllm-x` | `tpu/v5p/vllm` | `vllm-qwen3-32b-inline-standalone` |
| `intel-acc-xpu-vllm-x` | `xpu/vllm` | `vllm-qwen3-0.6b-inline-standalone` |
| `gke-acc-gpu-sglang-x` | `gpu/sglang/gke` | `sglang-qwen3-32b-parsers-standalone` |
| `gke-acc-tpu-sglang-x` | `tpu/v5p/sglang` | `sglang-qwen3-32b-ctx16k-parsers-standalone` |
| `amd-acc-rocm-sglang-x` | `amd/sglang/amd-ci` | `sglang-qwen3-32b-inline-standalone` |
| `amd-acc-rocm-atom-x` | `amd/atom/amd-ci` | `atom-qwen3-32b-ctxunknown-inline-standalone` |

Three lanes set no provider. They apply the accelerator and engine folder's own kustomization,
which includes only `base`, so they deploy what the `base` overlay does.

Lanes share a profile when their overlays give a client the same model, context window, parsers
and engine; a provider overlay changes only node, cache and NCCL settings. These lanes pass no
`flavor`. `sim-qwen3-32b-standalone` is not a lane: it puts the guide's router in front of
llm-d-inference-sim, for kind or CI without accelerators.

**No lane runs, so not covered:** the community overlays (TPU v6e and v7, TPU v7 dynamic slicing,
Rebellions NPU, CPU, Iluvatar, MetaX, Biren, Enflame, Moore Threads, TensorRT-LLM, and SGLang on
Intel XPU), gateway mode, and agentgateway as the standalone sidecar proxy. Each is a model part
and a few-line combination when a lane runs it.

**What has been measured.** Only the router layer, standalone, on kind with a simulator engine
(2026-11-16): the EPP's error envelopes, header and path handling and pass-through, recorded in
`parts/topology/standalone.yaml`. Engine and model lines come from each lane's overlay and the
API definitions. The first run of each profile is its measurement, and is expected to find
errors.

## Known gaps

- **Reasoning field name**: declared `reasoning` for vLLM (seen on vLLM 0.32 elsewhere) and
  `reasoning_content` for SGLang (inferred); neither observed on this guide's builds.
- **Prefix affinity** (`routing.prefix_affinity`, #27): the guide's central claim, that shared-prefix
  requests stay on one pod, needs a case family reading per-pod counters. Kind showed it for sequential requests (ten of
  ten on one replica), but `llmdv` cannot check it yet. Nor can the cache suite: no overlay asks the
  engine to report `cached_tokens`, so `cache_reuse` is undeclared.
- **Gateway mode** (#24): no lane runs it, so no profile has a `kind: gateway` ingress.
- **Surfaces**: the EPP also routes `/v1/embeddings` and `/inference/v1/generate` (both answered
  on kind), which `llmdv` has no surface for.
- **Engine ingresses**: none, because a committed profile names no pod and per-replica URLs are a
  lane's to provide. An engine layer, when added, is `kind: engine`, with SGLang's and Atom's
  error statuses, health paths and metric names declared on it, not implied by the kind.
- **Non-vLLM extensions**: `extension_options` and the extension entries of `chat_fields` and
  `completions_fields` are vLLM's vocabularies; `parts/engines/sglang.yaml` and `atom.yaml`
  declare none until a run shows which each engine serves.
- **`ocp` provider**: the `ibm-acc-gpu-vllm-x` lane deploys `gpu/vllm/ocp`, but `ocp` is not among
  `guide.yaml`'s `INFRA_PROVIDER` values or the README's support table.
- **Unknown context window**: no source states the Atom overlay's window, so that profile's
  context cases skip as undeclared.
- **Model-name rewrite header**: the EPP overrides the body's model with
  `x-llm-d-model-name-rewrite`, but `transport.model_rewrite_header` requires the answer to echo
  the client's name, which the EPP does not promise.
- **Per-pod metrics and fleet rules**: engine metrics, replica counts and router settings need
  cluster access; a lane adds an `environment` block in its own local file.
- **Load mode** (#23): the objective and fairness headers, the dropped-reason values and
  load-aware scoring show only under contention, so they skip as `requires_load`.
