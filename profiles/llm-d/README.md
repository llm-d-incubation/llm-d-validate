# llm-d guide profiles

Profiles that validate the [llm-d](https://github.com/llm-d/llm-d) well-lit-path guides. Each combination states what a correct stack does when a guide is deployed as shipped, with one model-server overlay, so a lane that deploys the guide can run `llmdv verify` against it. Only `optimized-baseline` is written so far.

They are data on top of llmdv, not part of it: nothing in `src/` knows about them, and a stack with no llm-d guide is described by its own profile exactly as before.

## Run one

Deploy the guide with an overlay a combination covers (its header lists them), give llmdv the router's URL, and run:

```sh
export LLMDV_EPP_URL=http://<GUIDE_NAME>-epp.<namespace>.svc.cluster.local   # standalone mode
llmdv profile validate profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml
llmdv verify --profile profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml \
  --label overlay=gpu/vllm/gke
```

Any address that reaches the EPP works: a route, the Service's address from inside the cluster, or a port-forward you run yourself. Pass the overlay as `--label`: one combination covers several overlays, and the labels keep their runs apart.

## Layout: a sum, not a product

A guide ships one router configuration, several engines, many model configurations and two topologies. One profile per combination would repeat every fact in dozens of files. So each fact lives in one part, and a combination lists the parts:

```
profiles/llm-d/
  parts/                               building blocks; never run on their own
    base.yaml                          what every guide's stack shares: API intent, engine-independent
    topology/standalone.yaml           the EPP's own listener: the ingress, error statuses, header dispositions
    engines/<engine>.yaml              an engine's own request extensions (vllm, sglang, atom)
    models/<model-config>.yaml         model id, context window, reasoning and tool-call shape, vision
    guides/<guide>.yaml                what one guide adds: its claims, cache decisions, block sizes
  <guide>/<engine>-<model-config>-<topology>.yaml   a combination: what you run
```

A combination is `extends: [../parts/base.yaml, ../parts/topology/<t>.yaml, ../parts/engines/<e>.yaml, ../parts/models/<m>.yaml, ../parts/guides/<g>.yaml]`, then its `name`, the parser names (the engine's own flag values, where the model part has parsers) and its change sentinels, whose values only a run of that exact stack supplies.

Rules that keep this simple:

- **Parts never extend anything.** Only combinations name parents. A file reached by two routes is an error ([REFERENCE.md](../REFERENCE.md#extends)), and with flat parts it cannot happen.
- **No key is set by two parts,** so the order of the list changes nothing. The one exception is the simulator, whose model part overrides four `base.yaml` keys and whose combination overrides the standalone surfaces, on purpose; each says so in a comment. A run's `profile.used.yaml` names the file that set each key.
- **A part is not a profile.** It has no ingress or no model of its own. Validate a combination, never a part; `tests/test_example_profiles.py` checks every part through the combinations that list it.
- **Ingress-keyed facts live in the topology part.** llmdv keys surfaces, error statuses and header dispositions by ingress id, and the ids differ by topology (`epp`, `gateway`). So the SLO headers, which this guide leaves without effect, are declared there rather than in the guide part.
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
| `FROM values: <path>` | read from the guide's shipped configuration at llm-d `565373b`, overlays rendered with `kubectl kustomize` |
| `FROM docs: <path>` | read from the llm-d documentation at that commit, or from this repository's profile reference |
| `INTENT` | how a correct stack behaves, as the API itself defines it |
| `DERIVED` | computed from the model's own config, not stated by the guide (a default context window) |
| `MEASURED 2026-11-16 on kind (real EPP, simulator engine)` | observed through a real endpoint picker in front of llm-d-inference-sim; a fact about the router layer only |
| inferred | a reading of the config no source states outright; says so in the comment |

Behaviour lines change only after the team triages a failure, and the comment then cites the decision.

## optimized-baseline coverage

Groups are the shipped overlays that look the same to a client. Overlays are `ACCELERATOR_TYPE/MODEL_SERVER/INFRA_PROVIDER`.

**Shipped now: every group the guide validates with a nightly lane, in standalone mode (the topology those lanes use), plus the simulator.** The other groups and gateway mode follow the same pattern, a new model part and a few-line combination, and are added when someone runs them or when the gateway role (#24) lands. Listing them here keeps the gap visible rather than shipping files nothing has measured.

| Group | Combination (`optimized-baseline/`) | Overlays | Guide status | Shipped |
| --- | --- | --- | --- | --- |
| A | `vllm-qwen3-32b-parsers-standalone` | `gpu/vllm/{base,gke,ocp}` | validated (`ocp` is no `INFRA_PROVIDER` value) | yes (standalone) |
| A′ | `vllm-qwen3-32b-ctx16k-parsers-standalone` | `tpu/v6/vllm/{base,gke}` | community | not yet |
| B | `vllm-qwen3-32b-inline-standalone` | `amd/vllm/{base,amd-ci}`, `tpu/v5p/vllm/{base,gke}` | validated | yes (standalone) |
| C | `sglang-qwen3-32b-parsers-standalone` | `gpu/sglang/{base,gke}` | validated | yes (standalone) |
| C′ | `sglang-qwen3-32b-ctx16k-parsers-standalone` | `tpu/{v5p,v6,v7}/sglang/{base,gke}` | v5p validated; v6, v7 community | yes (standalone; v5p is the validated overlay) |
| D | `sglang-qwen3-32b-inline-standalone` | `amd/sglang/{base,amd-ci}` | validated | yes (standalone) |
| E | `atom-qwen3-32b-ctxunknown-inline-standalone` | `amd/atom/{base,amd-ci}` | validated | yes (standalone) |
| F | `trtllm-qwen3-32b-ctxunknown-inline-standalone` | `gpu/trtllm/{base,gke}` | community | not yet |
| G | `vllm-qwen3-32b-ctx3k-inline-standalone` | `biren/vllm/base` | community | not yet |
| H | `vllm-qwen3-32b-ctx2k-inline-standalone` | `mthreads/vllm/base` | community | not yet |
| I | `vllm-qwen3-0.6b-inline-standalone` | `xpu/vllm/base`, `xpu/sglang/base` | vLLM validated, SGLang community | vLLM, standalone; SGLang not yet |
| J | `vllm-qwen3.5-397b-a17b-fp8-parsers-standalone` | `tpu/v7/vllm/{base,gke}` | community | not yet |
| K | `vllm-qwen3-coder-480b-a35b-fp8-noparsers-standalone` | `tpu/v7-dynamic-slice/vllm/{2x2x1,2x2x2}` | community | not yet |
| L | `vllm-gpt-oss-120b-parsers-standalone` | `npu/vllm/base` | community | not yet |
| M | `vllm-llama-3.2-3b-instruct-ctx8k-noparsers-standalone` | `cpu/vllm/{base,gke}` | community | not yet |
| N | `vllm-deepseek-v4-flash-ctx20k-inline-standalone` | `iluvatar/vllm/base` | community | not yet |
| O | `vllm-deepseek-r1-distill-llama-70b-ctx20k-inline-standalone` | `metax/vllm/base` | community | not yet |
| P | `sglang-deepseek-v4-flash-pathid-parsers-standalone` | `mthreads/sglang/base` | community | not yet |
| sim | `sim-qwen3-32b-standalone` | none: llm-d-inference-sim replicas behind the guide's router, for kind or CI | not a shipped overlay | yes |

That is 18 groups (A′ and C′ are window variants of A and C); 7 combinations ship for the validated groups, plus the simulator. Groups that share a model part differ in engine, so they keep separate combinations: an engine's request extensions and error shapes are its own. Within a group, provider overlays (`gke`, `amd-ci`, `ocp`) and the two v7 slice topologies change nothing a client sees, so they share one combination and are told apart by `--label`.

**What has been measured.** Only the router layer, standalone mode, on kind with a simulator engine (2026-11-16): the EPP's error envelopes, header handling, path handling and pass-through, recorded in `parts/topology/standalone.yaml`. No combination has run against a real engine, and nothing has run in gateway mode. Every engine and model line is written from the shipped config and the API definitions; the first run of each combination is its measurement, and it is expected to find errors.

## Known gaps

- **Prefix affinity** (`routing.prefix_affinity`, #27): the guide's central claim, that shared-prefix requests stay on one pod, needs a case family that reads per-pod counters. Kind showed it holding for sequential requests (ten of ten on one replica), but llmdv cannot check it yet. The cache suite cannot see it here either: no overlay asks the engine to report `cached_tokens`, so `cache_reuse` is left undeclared.
- **Gateway role** (#24): llmdv has no role for a Gateway in front of the EPP yet. Gateway mode ships when the role exists, as a `parts/topology/gateway.yaml` part and one combination per group.
- **Surfaces**: the EPP also routes `/v1/embeddings` and `/inference/v1/generate` (both answered on kind), which llmdv has no surface for.
- **Engine kinds**: no combination declares an engine ingress, because a committed profile names no pod and per-replica URLs are a lane's to provide. So the question of a kind for SGLang, TensorRT-LLM and Atom does not arise yet; when an engine layer is added, `vllm` is the only engine kind and would misdescribe their error envelopes, health paths and metric names.
- **Non-vLLM extension vocabularies**: llmdv's extension vocabularies (`vllm_options` and the extension entries of `chat_fields` and `completions_fields`) are vLLM's. `parts/engines/sglang.yaml` and `atom.yaml` declare none of them until a run shows which each engine serves.
- **Group P's model id**: the `mthreads/sglang` overlay serves a DeepSeek-V4 build under its local path, while `guide.yaml` gives that accelerator `Qwen/Qwen3-32B`. The model part declares what the overlay serves (inferred from SGLang's behaviour without `--served-model-name`); the first run's `GET /v1/models` settles it.
- **`ocp` provider**: `gpu/vllm/ocp` exists but `ocp` is not in `guide.yaml`'s `INFRA_PROVIDER` values; it is covered by group A all the same.
- **Unknown context windows**: E, F, L and P have no window from any flag or established default, so their context cases skip as undeclared until a run supplies one.
- **Model-name rewrite header**: the EPP overrides the body's model with `x-llm-d-model-name-rewrite`, but `transport.model_rewrite_header` requires the answer to echo the client's name, which the EPP does not promise.
- **Per-pod metrics and fleet rules**: engine metrics, replica counts and router settings need cluster access, which a committed profile does not carry; a lane adds an `environment` block in its own local file.
- **Load mode** (#23): the objective and fairness headers, the dropped-reason values and load-aware scoring show only under contention, so they skip as `requires_load`.
