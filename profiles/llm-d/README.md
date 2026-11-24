# llm-d guide profiles

Profiles that validate the [llm-d](https://github.com/llm-d/llm-d) guides (llm-d's docs also call them well-lit paths; this repository says guide). Each combination states what a correct stack does when a guide is deployed as shipped, with one model-server overlay, so a lane that deploys the guide can run `llmdv verify` against it. There is a profile for every configuration a nightly lane deploys at llm-d `9ca8ff0`; see [Coverage](#coverage).

They are data on top of llmdv, not part of it: nothing in `src/` knows about them, and a stack with no llm-d guide is described by its own profile exactly as before.

## Run one

Deploy the guide with an overlay a combination covers (its header lists them), give llmdv the router's URL, and run:

```sh
export LLMDV_EPP_URL=http://<GUIDE_NAME>-epp.<namespace>.svc.cluster.local   # standalone mode
llmdv profile validate profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml
llmdv verify --profile profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

Any address that reaches the EPP works: a route, the Service's address from inside the cluster, or a port-forward you run yourself. Pass the lane's accelerator and provider with `--identity`: one combination covers several overlays, and identity dimensions keep their runs apart.

## Layout: a sum, not a product

A guide ships one router configuration, several engines, many model configurations and two topologies. One profile per combination would repeat every fact in dozens of files. So each fact lives in one part, and a combination lists the parts:

```
profiles/llm-d/
  parts/                               building blocks every guide shares; never run on their own
    base.yaml                          what every guide's stack shares: API intent, engine-independent
    topology/standalone.yaml           the EPP's own listener: the ingress, error statuses, header dispositions
    engines/<engine>.yaml              an engine's own request extensions (vllm, sglang, atom)
    models/<model-config>.yaml         model id, context window, reasoning and tool-call shape, vision
    sentinels/unmeasured*.yaml         placeholder change sentinels until a run measures them
  <guide>/                             one folder per guide, named as llm-d names it
    parts/common.yaml                  what every profile of this guide shares: its claims, cache decisions, block sizes
    <engine>-<model-config>-<topology>.yaml   a combination: what you run
```

Anything inside a `parts/` folder is a building block; every other file is a profile you can run.

A combination is `extends: [../parts/base.yaml, ../parts/topology/<t>.yaml, ../parts/engines/<e>.yaml, ../parts/models/<m>.yaml, ../parts/sentinels/<s>.yaml, parts/common.yaml]`, then its `name` and the parser names (the engine's own flag values, where the model part has parsers). The sentinel part holds placeholders (`unmeasured-with-tools.yaml` when the model has a tool-call parser, `unmeasured.yaml` otherwise); once a run of that exact stack measures them, the combination sets its own `prompt_pins` and `contract_baseline`, which override the part.

Rules that keep this simple:

- **Parts never extend anything.** Only combinations name parents. A file reached by two routes is an error ([REFERENCE.md](../REFERENCE.md#extends)), and with flat parts it cannot happen.
- **No key is set by two parts,** so the order of the list changes nothing. The one exception is the simulator, whose model part overrides four `base.yaml` keys and whose combination overrides the standalone surfaces, on purpose; each says so in a comment. A run's `profile.used.yaml` names the file that set each key.
- **A part is not a profile.** It has no ingress or no model of its own. Validate a combination, never a part; `tests/test_example_profiles.py` checks every part through the combinations that list it.
- **Ingress-keyed facts live in the topology part.** llmdv keys surfaces, error statuses and header dispositions by ingress id, and the ids differ by topology (`epp`, `gateway`). So the SLO headers, which this guide leaves without effect, are declared there rather than in the guide's `parts/common.yaml`.
- **Model parts are named by the configuration a client sees** (`qwen3-32b-ctx16k-parsers`), never by accelerator; their headers list the overlays that use them.

## Which source owns which fact

| Source | Owns |
| --- | --- |
| the guide's `guide.yaml` | which overlays exist, which are validated, how a client reaches the stack |
| router values and model-server overlays | the deployed configuration: model id, window, parsers, router plugins, block sizes |
| the APIs (OpenAI, Anthropic, the EPP reference) | behaviour: statuses, envelopes, fields, header handling |
| a run | existence (surfaces served, the window an engine resolves) and the truth of every line above |

## Provenance

Every declaration carries a tag in the comment above it:

| Tag | Means |
| --- | --- |
| `FROM values: <path>` | read from the guide's shipped configuration at llm-d `9ca8ff0`, overlays rendered with `kubectl kustomize` |
| `FROM docs: <path>` | read from the llm-d documentation at that commit, or from this repository's profile reference |
| `INTENT` | how a correct stack behaves, as the API itself defines it |
| `DERIVED` | computed from the model's own config, not stated by the guide (a default context window) |
| `MEASURED 2026-11-16 on kind (real EPP, simulator engine)` | observed through a real endpoint picker in front of llm-d-inference-sim; a fact about the router layer only |
| inferred | a reading of the config no source states outright; says so in the comment |

Behaviour lines change only after the team triages a failure, and the comment then cites the decision.

## Coverage

Every guide that llm-d runs in a nightly lane (`.github/workflows/nightly-e2e-<guide>-*.yaml` at `9ca8ff0`) has a folder here, named as its `guide.yaml` names it. Each folder's README maps every lane to its profile and gives the exact `--identity` flags; lanes that look the same to a client share a profile, and `--identity flavor=` separates lanes that would otherwise share an identity.

| Guide | Lanes | Profiles | Notes |
| --- | --- | --- | --- |
| [optimized-baseline](optimized-baseline/) | 10 | 7, plus a simulator profile | see below |
| [pd-disaggregation](pd-disaggregation/README.md) | 8 | 5 | requests pass the decode pod's routing sidecar |
| [wide-ep](wide-ep/README.md) | 5 | 3 | the XPU lane runs aggregated, without prefill/decode |
| [precise-prefix-cache-routing](precise-prefix-cache-routing/README.md) | 6 | 4 | |
| [predicted-latency-routing](predicted-latency-routing/README.md) | 5 | 2 | |
| [tiered-prefix-cache](tiered-prefix-cache/README.md) | 15 | 5 | `flavor` names the connector and tier |
| [multimodal-serving](multimodal-serving/README.md) | 3 | 1 | `flavor` names aggregation or e-pd |
| [pod-snapshot](pod-snapshot/README.md) | 2 | 2 | |
| [fast-model-actuation](fast-model-actuation/README.md) | 2 | 1 | |
| [flow-control](flow-control/README.md) | 2 | 2 | |
| [agentic-api](agentic-api/README.md) | 1 | 1 | the router only |
| [async-multitenant](async-multitenant/README.md) | 2 | 1 | the router only; `flavor` names evictable or holdback |
| [keda-epp](keda-epp/README.md) | 3 | 2 | |

Every profile tests at the router (`<release>-epp`, standalone mode), the only layer every lane exposes the same way. None has been run against a real engine: each says `WRITTEN, NOT RUN`, and its first run is its measurement.

**Not covered, and why:**

- **fast-model-actuation-keda** (2 lanes): the pool scales from zero, so the first request can wait up to 300 s and the pool returns to zero after about a minute idle; a profile cannot say "the first request may be slow" without loosening every latency check.
- **Layers in front of or beside the router**: agentic-api's stateful Responses server, the async coordinator and its queues, and the prefill/decode routing sidecar. Each needs its own ingress, declared in front of the router with `fronts`, and measured behaviour.
- **Claims that only hold under load or need cluster reads**: queueing, priority and fairness, eviction and shedding, scaling events, prefill/decode decisions, offload and cache counters, snapshot restore, prefix affinity (#23, #27).
- **Guides no nightly lane runs**: coord-disaggregation, p2p-kv-cache-sharing, multi-model-routing, omni-serving, turn-priority-fairness, modelexpress-p2p, hyperconverged-cephfs, the per-model guides under `guides/models/`, and the other workload-autoscaling variants. Each is added when a lane runs it.

## Which lane runs everything

A guide's `selection` (in its `parts/common.yaml`) is what its lanes run by default: the router path, the overlay's parsers and window, and what the guide changes. The rest of the suite is engine and model conformance, whose answer depends only on the engine build, the model and its client-visible flags. One lane per configuration runs it, with `llmdv verify --all`; every other lane with that configuration relies on that run. Where optimized-baseline deploys a configuration, its lane is the one.

| Engine | Model part | Accelerator | Runs `--all` | Also deploys it (selection only) |
| --- | --- | --- | --- | --- |
| vllm | `deepseek-r1-0528-tools` | NVIDIA GPU | `wide-ep/vllm-deepseek-r1-0528-tools-standalone` | — |
| vllm | `deepseek-v2-lite-chat-ctx2k-inline` | Intel XPU | `wide-ep/vllm-deepseek-v2-lite-chat-ctx2k-inline-standalone` | — |
| vllm | `deepseek-v3-ctx36k-inline` | AMD | `wide-ep/vllm-deepseek-v3-ctx36k-inline-standalone` | — |
| sglang | `gpt-oss-120b-inline` | NVIDIA GPU | `pd-disaggregation/sglang-gpt-oss-120b-inline-standalone` | — |
| vllm | `gpt-oss-120b-parsers` | NVIDIA GPU | `pd-disaggregation/vllm-gpt-oss-120b-parsers-standalone` | — |
| vllm | `gpt-oss-120b-parsers` | AMD | `pd-disaggregation/vllm-gpt-oss-120b-parsers-standalone` | — |
| vllm | `qwen3-0.6b-ctx32000-inline` | Intel XPU | `pd-disaggregation/vllm-qwen3-0.6b-ctx32000-inline-standalone` | — |
| vllm | `qwen3-0.6b-inline` | NVIDIA GPU | `fast-model-actuation/vllm-qwen3-0.6b-inline-standalone` | — |
| vllm | `qwen3-0.6b-inline` | Intel XPU | `optimized-baseline/vllm-qwen3-0.6b-inline-standalone` | precise-prefix-cache-routing, predicted-latency-routing |
| vllm | `qwen3-32b-ctx128k-parsers` | NVIDIA GPU | `predicted-latency-routing/vllm-qwen3-32b-ctx128k-parsers-standalone` | — |
| vllm | `qwen3-32b-ctx128k-parsers` | AMD | `predicted-latency-routing/vllm-qwen3-32b-ctx128k-parsers-standalone` | — |
| sglang | `qwen3-32b-ctx16k-parsers` | TPU | `optimized-baseline/sglang-qwen3-32b-ctx16k-parsers-standalone` | tiered-prefix-cache |
| vllm | `qwen3-32b-ctx4k-inline` | TPU | `precise-prefix-cache-routing/vllm-qwen3-32b-ctx4k-inline-standalone` | — |
| sglang | `qwen3-32b-ctx8k-inline` | NVIDIA GPU | `pod-snapshot/sglang-qwen3-32b-ctx8k-inline-standalone` | — |
| vllm | `qwen3-32b-ctx8k-inline` | NVIDIA GPU | `pod-snapshot/vllm-qwen3-32b-ctx8k-inline-standalone` | — |
| vllm | `qwen3-32b-ctx9k-parsers` | TPU | `pd-disaggregation/vllm-qwen3-32b-ctx9k-parsers-standalone` | — |
| atom | `qwen3-32b-ctxunknown-inline` | AMD | `optimized-baseline/atom-qwen3-32b-ctxunknown-inline-standalone` | — |
| sglang | `qwen3-32b-inline` | AMD | `optimized-baseline/sglang-qwen3-32b-inline-standalone` | — |
| vllm | `qwen3-32b-inline` | AMD | `optimized-baseline/vllm-qwen3-32b-inline-standalone` | flow-control, keda-epp, pd-disaggregation, precise-prefix-cache-routing, tiered-prefix-cache |
| vllm | `qwen3-32b-inline` | TPU | `optimized-baseline/vllm-qwen3-32b-inline-standalone` | tiered-prefix-cache |
| vllm | `qwen3-32b-inline` | Intel XPU | `tiered-prefix-cache/vllm-qwen3-32b-inline-standalone` | — |
| sglang | `qwen3-32b-parsers` | NVIDIA GPU | `optimized-baseline/sglang-qwen3-32b-parsers-standalone` | tiered-prefix-cache |
| vllm | `qwen3-32b-parsers` | NVIDIA GPU | `optimized-baseline/vllm-qwen3-32b-parsers-standalone` | agentic-api, async-multitenant, flow-control, keda-epp, precise-prefix-cache-routing, tiered-prefix-cache |
| vllm | `qwen3-8b-ctx20k-inline` | Intel XPU | `tiered-prefix-cache/vllm-qwen3-8b-ctx20k-inline-standalone` | — |
| vllm | `qwen3-vl-32b-instruct-inline` | NVIDIA GPU | `multimodal-serving/vllm-qwen3-vl-32b-instruct-inline-standalone` | — |
| vllm | `qwen3-vl-32b-instruct-inline` | TPU | `multimodal-serving/vllm-qwen3-vl-32b-instruct-inline-standalone` | — |

Two lanes run a different engine build from the rest of their configuration, so each runs `--all` too: pd-disaggregation's `amd-ci-acc-rocm-vllm-moriio` (vllm-openai-rocm v0.32.0-rc.2, an llm-d build) and keda-epp's `ibm-acc-gpu-vllm-x` (vLLM's floating `nightly` image). A lane whose engine image changes runs `--all` once on the new build; `llmdv compare` reports the build change (`the stack changed`).

## optimized-baseline coverage

These profiles follow llm-d's CI: there is one for every configuration the guide marks `validated` and a nightly lane deploys, and none for any other. The guide's ten lanes (`nightly-e2e-optimized-baseline-<lane>.yaml` in llm-d, each in standalone mode) deploy seven configurations that differ to a client:

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

Three lanes set no provider. They apply the accelerator and engine folder's own kustomization, which includes only `base`, so they deploy what the `base` overlay does.

Lanes share a profile when the overlays they deploy give a client the same model, context window, parsers and engine: a provider overlay changes only node, cache and NCCL settings. Pass the lane's accelerator and provider with `--identity` to keep their runs apart. Lanes that share a profile and still differ in something those do not name, such as tiered-prefix-cache's lmcache and native offload, add `--identity flavor=<value>` (`flavor=lmcache-cpu`). `sim-qwen3-32b-standalone` is not a lane: it runs the guide's router in front of llm-d-inference-sim, for kind or CI without accelerators.

**Not covered, because no lane runs them:** the community overlays (TPU v6e and v7, TPU v7 dynamic slicing, Rebellions NPU, CPU, Iluvatar, MetaX, Biren, Enflame, Moore Threads, TensorRT-LLM, and SGLang on Intel XPU), gateway mode, and agentgateway as the standalone sidecar proxy. Each follows the same pattern, a model part and a few-line combination, and is added when a lane runs it.

**What has been measured.** Only the router layer, in standalone mode, on kind with a simulator engine (2026-11-16): the EPP's error envelopes, header handling, path handling and pass-through, recorded in `parts/topology/standalone.yaml`. No profile has run against a real engine. Every engine and model line is written from the overlay each lane deploys and from the API definitions; the first run of each profile is its measurement, and it is expected to find errors.

## Known gaps

- **Reasoning field name**: vLLM is declared as answering `reasoning` (seen on vLLM 0.32 elsewhere) and SGLang `reasoning_content` (inferred). Neither has been observed on this guide's builds.
- **Prefix affinity** (`routing.prefix_affinity`, #27): the guide's central claim, that shared-prefix requests stay on one pod, needs a case family that reads per-pod counters. Kind showed it holding for sequential requests (ten of ten on one replica), but llmdv cannot check it yet. The cache suite cannot see it here either: no overlay asks the engine to report `cached_tokens`, so `cache_reuse` is left undeclared.
- **Gateway role** (#24): llmdv has no role for a Gateway in front of the EPP yet, and no lane runs gateway mode.
- **Surfaces**: the EPP also routes `/v1/embeddings` and `/inference/v1/generate` (both answered on kind), which llmdv has no surface for.
- **Engine kinds**: no profile declares an engine ingress, because a committed profile names no pod and per-replica URLs are a lane's to provide. When an engine layer is added, `vllm` is the only engine kind and would misdescribe SGLang's and Atom's error envelopes, health paths and metric names.
- **Non-vLLM extension vocabularies**: llmdv's extension vocabularies (`extension_options` and the extension entries of `chat_fields` and `completions_fields`) are vLLM's. `parts/engines/sglang.yaml` and `atom.yaml` declare none of them until a run shows which each engine serves.
- **`ocp` provider**: the `ibm-acc-gpu-vllm-x` lane deploys `gpu/vllm/ocp`, but `ocp` is not among `guide.yaml`'s `INFRA_PROVIDER` values or the README's support table.
- **Unknown context window**: the Atom overlay sets no window and no source states its default, so that profile's context cases skip as undeclared until someone states the value.
- **Model-name rewrite header**: the EPP overrides the body's model with `x-llm-d-model-name-rewrite`, but `transport.model_rewrite_header` requires the answer to echo the client's name, which the EPP does not promise.
- **Per-pod metrics and fleet rules**: engine metrics, replica counts and router settings need cluster access, which a committed profile does not carry; a lane adds an `environment` block in its own local file.
- **Load mode** (#23): the objective and fairness headers, the dropped-reason values and load-aware scoring show only under contention, so they skip as `requires_load`.
