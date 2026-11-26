# llm-d guide validation profiles

Ready-made profiles for testing a deployment of an [llm-d](https://github.com/llm-d/llm-d) guide. Deploy the guide your way, point `llmdv` at it, and it checks that the deployment behaves as the guide and its APIs say. Any other stack uses a profile of its own ([profiles/README.md](../README.md)).

[Find your guide](#find-your-guide) · [Test your deployment](#test-your-deployment) · [Profile names and `--identity`](#profile-names-and---identity) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## Find your guide

Each guide's README says which profile matches what you deployed, how to run it and what it tests.

| Guide | Validation focus |
| --- | --- |
| [agentic-api](agentic-api/README.md) | Stateless Responses calls used by agentic-api |
| [async-multitenant](async-multitenant/README.md) | Router behavior beneath the async coordinator |
| [fast-model-actuation](fast-model-actuation/README.md) | API integrity through the requester TCP proxy |
| [flow-control](flow-control/README.md) | Admission signals and cancellation recovery |
| [keda-epp](keda-epp/README.md) | Serving through an autoscaled pool |
| [multimodal-serving](multimodal-serving/README.md) | Image understanding across the serving path |
| [optimized-baseline](optimized-baseline/README.md) | The baseline router-to-model API contract |
| [pd-disaggregation](pd-disaggregation/README.md) | API correctness through the disaggregated path |
| [pod-snapshot](pod-snapshot/README.md) | Serving correctness with restored replicas in the pool |
| [precise-prefix-cache-routing](precise-prefix-cache-routing/README.md) | Rendering and token contracts used by precise routing |
| [predicted-latency-routing](predicted-latency-routing/README.md) | Serving correctness with predictor routing enabled |
| [tiered-prefix-cache](tiered-prefix-cache/README.md) | Serving correctness with cache offloading configured |
| [wide-ep](wide-ep/README.md) | API integrity through the expert-parallel serving path |

## Test your deployment

Pick the profile for the overlay you deployed, from the guide's README. The profiles test the guide's router (the EPP), so give `llmdv` its URL; it needs no key:

```sh
export LLMDV_ROUTER_URL='https://your-router.example' # replace with your router's URL
llmdv profile validate profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml
llmdv verify --profile profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml
```

To test a gateway or the engine as well, [add it as another ingress](../README.md#include-a-gateway-or-engine) in a local file that extends the profile.

## Profile names and `--identity`

A profile is `<engine>-<model configuration>[-<variant>]-<topology>.yaml`, named by what a client sees: `vllm-qwen3-32b-parsers-standalone` is Qwen3-32B on vLLM with reasoning and tool-call parsers, behind the standalone router. Files in a `parts/` folder are building blocks and never run on their own.

Deployments that look the same to a client share a profile. `--identity` flags record the dimensions comparison uses to keep configurations apart. They are optional for verification, but set the relevant dimensions when comparing deployments that share a profile. Omitted dimensions cannot distinguish those deployments; free-form `--label` values do not affect matching. Read them off your overlay path:

| Flag | Value |
| --- | --- |
| `accelerator` | the accelerator folder of your overlay (`gpu`, `amd`, `tpu/v5p`, `xpu`) |
| `provider` | the provider folder it renders (`gke`, `ocp`, `amd-ci`), with any connector the guide folds into it (`nixl/amd-ci`); `base` when there is none |
| `flavor` | anything else the guide lets you vary (connector, tier, topology, scaler), joined with `/` in overlay-path order (`lmcache-connector/cpu`) |

Each guide's README gives an example.

## Validation scope

Each profile's default selects checks tied to the guide's serving behavior. Start there,
then expand coverage for the APIs, clients and operating conditions your deployment needs.
`--full-suite` selects the existing library before filters and applicability gates; it does
not guarantee complete API or production coverage. See
[growing coverage toward production](../../docs/getting-started.md#widen-coverage).

The broader-validation assignments below are maintainer guidance for avoiding duplicate
qualification work across shared configurations. They do not restrict what you can test.

## Not covered

- **Layers other than the router**: agentic-api's stateful Responses server, the async coordinator and its queues, and the prefill/decode routing sidecar. Each needs its own ingress, placed with `fronts`, and measured behaviour.
- **Claims that hold only under load or need cluster reads**: queueing, priority and fairness, eviction and shedding, scaling events, prefill/decode decisions, offload and cache counters, snapshot restore, prefix affinity (#23, #27).
- **Guides without a profile yet**: fast-model-actuation-keda (#130), and these additional guides (coord-disaggregation, p2p-kv-cache-sharing, multi-model-routing, omni-serving, turn-priority-fairness, modelexpress-p2p, hyperconverged-cephfs, the per-model guides and the other workload-autoscaling variants). The details are under [For maintainers](#for-maintainers).

## For maintainers

How the folders are built and where each value comes from: [profile evidence and provenance](../../docs/profile-provenance.md#how-profilesllm-d-is-built). To find what changed in llm-d since these profiles were written: [the drift skill](../../.agents/skills/tracking-llm-d-guide-drift/SKILL.md).

[`.overlays.yaml`](.overlays.yaml) is for llm-d's nightly CI, not for choosing a profile: for each listed guide it maps every overlay (the path under `guides/<guide>/modelserver/` a workflow deploys) to the profile that workflow runs, and an overlay it does not list is skipped. It says exactly what the guide's "Pick your profile" table says, which [`tests/test_llmd_overlays.py`](../../tests/test_llmd_overlays.py) enforces, so a change to that table changes this file in the same commit. Listing a guide here is how it starts running in llm-d's nightly; only guides whose table has an "Overlay" or "Overlays" column can be listed. The CI side is [#230](https://github.com/llm-d-incubation/llm-d-validate/issues/230).

<details>
<summary>Coverage: which guides have profiles, and how many nightly workflows each covers</summary>

The guide profiles and coverage below are based on llm-d commit `9ca8ff0`.

Every guide llm-d runs in a nightly workflow, which llm-d's guide policy calls
[Tier 3, Validated](https://github.com/llm-d/llm-d/blob/main/guides/GUIDES-POLICY.md#5-production-readiness-tiers) (`.github/workflows/nightly-e2e-*.yaml`; keda-epp's are `nightly-e2e-workload-autoscaling-keda-epp-*`), has a folder here,
named as its `guide.yaml` names it, except those listed under [Not covered](#not-covered). Workflows that look the same to a
client share a profile.

| Guide | Workflows | Profiles | Notes |
| --- | --- | --- | --- |
| [optimized-baseline](optimized-baseline/) | 10 | 7, plus a simulator profile | see [optimized-baseline coverage](optimized-baseline/README.md#for-maintainers) |
| [pd-disaggregation](pd-disaggregation/README.md) | 8 | 5 | requests pass the decode pod's routing sidecar |
| [wide-ep](wide-ep/README.md) | 5 | 3 | the XPU workflow runs aggregated, without prefill/decode |
| [precise-prefix-cache-routing](precise-prefix-cache-routing/README.md) | 6 | 4 | |
| [predicted-latency-routing](predicted-latency-routing/README.md) | 5 | 2 | |
| [tiered-prefix-cache](tiered-prefix-cache/README.md) | 15 | 5 | `flavor` names the connector and tier |
| [multimodal-serving](multimodal-serving/README.md) | 3 | 1 | `flavor` names the topology |
| [pod-snapshot](pod-snapshot/README.md) | 2 | 2 | |
| [fast-model-actuation](fast-model-actuation/README.md) | 2 | 1 | |
| [flow-control](flow-control/README.md) | 2 | 2 | |
| [agentic-api](agentic-api/README.md) | 1 | 1 | the router only |
| [async-multitenant](async-multitenant/README.md) | 2 | 1 | the router only; `flavor` names evictable or holdback |
| [keda-epp](keda-epp/README.md) | 3 | 3 | `flavor` names the scaling signal; the router runs flow control on the queue workflows only |

Every profile tests at the router (`<release>-epp`, standalone mode), the only layer every workflow exposes the same way.

**Not covered, in detail:**

- **fast-model-actuation-keda** (2 workflows, #130). The pool scales from zero on the router's
  flow-control queue size only (`minReplicaCount: 0`, `cooldownPeriod: 30`; llm-d
  `guides/fast-model-actuation/fast-model-actuation-keda/keda/base/kustomization.yaml`), and
  `llmdv` does not sustain the load needed to keep a queue-based scaler active. The pool can
  drop to zero between checks and
  cold-start, holding a request up to the router's `noEndpointRequestTTL` of 300 s
  (`guides/fast-model-actuation/fast-model-actuation-keda/router/fast-model-actuation-keda.values.yaml`).
  `llmdv` has no warm-up; tolerating that means raising `stream_idle` and `stream_budget`
  ([parts/topology/standalone.yaml](parts/topology/standalone.yaml)) for every streaming check.
- **ci-async-kind-e2e** runs on pull requests and by hand, never nightly (llm-d
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
- **Guides no nightly workflow runs**: coord-disaggregation, p2p-kv-cache-sharing,
  multi-model-routing, omni-serving, turn-priority-fairness, modelexpress-p2p,
  hyperconverged-cephfs, the per-model guides under `guides/models/`, and the other
  workload-autoscaling variants. Each is added when a workflow runs it.

</details>

<details>
<summary>Broader validation assignments</summary>

For maintained upstream coverage, assign a full-library validation run to each engine/model
configuration and distinct engine build, in addition to the focused guide checks. The table
identifies which profile supplies that broader coverage when several guides share a
configuration. Where optimized-baseline covers it, use that profile's validation run.

Each profile below is `<guide>/<engine>-<model part>[-<variant>]-standalone`. GPU means NVIDIA GPU, XPU
Intel XPU.

| Profile that runs `--full-suite` | Accelerator | Also deploys it (default test set only) |
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

\* One of this guide's workflows for the configuration runs `--full-suite` too. Two workflows run a different
engine build from the rest of their configuration, so each runs `--full-suite`: pd-disaggregation's
`amd-ci-acc-rocm-vllm-moriio` (vllm-openai-rocm v0.32.0-rc.2, an llm-d build) and keda-epp's
`ibm-acc-gpu-vllm-x` (vLLM's floating `nightly` image). A workflow whose engine image changes runs
`--full-suite` once on the new build; `llmdv compare` reports the build change (`the stack changed`).

</details>

The [optimized-baseline guide page](optimized-baseline/README.md#for-maintainers) owns its
workflow-to-profile mapping. The shared router declarations were measured on a simulator-backed
router; engine and model declarations come from the documented workflow configurations and API
contracts. Preserve that distinction when interpreting a pass.

<details>
<summary>Known gaps in the optimized-baseline profiles</summary>

- **Reasoning field name**: declared `reasoning` for vLLM (seen on vLLM 0.32 elsewhere) and
  `reasoning_content` for SGLang (inferred); neither observed on this guide's builds.
- **Prefix affinity** (`routing.prefix_affinity`, #27): the guide's central claim, that shared-prefix
  requests stay on one pod, needs a case family reading per-pod counters. Kind showed it for sequential requests (ten of
  ten on one replica), but `llmdv` cannot check it yet. Nor can the cache suite: no overlay asks the
  engine to report `cached_tokens`, so `cache_reuse` is undeclared.
- **Gateway mode** (#24): no workflow runs it, so no profile has a `kind: gateway` ingress.
- **Surfaces**: the EPP also routes `/v1/embeddings` and `/inference/v1/generate` (both answered
  on kind), which `llmdv` has no surface for.
- **Engine ingresses**: none, because a committed profile names no pod and per-replica URLs are a
  workflow's to provide. An engine layer, when added, is `kind: engine`, with SGLang's and Atom's
  error statuses, health paths and metric names declared on it, not implied by the kind.
- **Non-vLLM extensions**: `extension_options` and the extension entries of `chat_fields` and
  `completions_fields` are vLLM's vocabularies; `parts/engines/sglang.yaml` and `atom.yaml`
  declare none until a run shows which each engine serves.
- **`ocp` provider**: the `ibm-acc-gpu-vllm-x` workflow deploys `gpu/vllm/ocp`, but `ocp` is not among
  `guide.yaml`'s `INFRA_PROVIDER` values or the README's support table.
- **Unknown context window**: no source states the Atom overlay's window, so that profile's
  context cases skip as undeclared.
- **Model-name rewrite header**: the EPP overrides the body's model with
  `x-llm-d-model-name-rewrite`, but `transport.model_rewrite_header` requires the answer to echo
  the client's name, which the EPP does not promise.
- **Per-pod metrics and fleet rules**: engine metrics, replica counts and router settings need
  cluster access; a workflow adds an `environment` block in its own local file.
- **Load mode** (#23): the objective and fairness headers, the dropped-reason values and
  load-aware scoring show only under contention, so they skip as `requires_load`.

</details>
