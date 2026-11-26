# keda-epp

Profiles for testing a deployment of llm-d's [keda-epp](https://github.com/llm-d/llm-d/tree/main/guides/workload-autoscaling/keda-epp) guide (`guides/workload-autoscaling/keda-epp`), in which optimized-baseline's model server is autoscaled by a KEDA `ScaledObject` on an EPP metric (flow-control queue size and running requests, or in-flight requests).

[What it tests](#what-it-tests) · [Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## What it tests

**Validation focus: serving through an autoscaled pool.** Check APIs, streaming and cancellation; queue profiles also require the flow-control header. These checks detect serving regressions while the pool changes.

**What a pass establishes:** the checks that actually executed held at the reachable ingress
during this run. Skipped checks remain coverage gaps. **Coverage boundary:** Scaling decisions and replica transitions need metrics and cluster observations.

These focused defaults are a starting point for validating a production deployment. Add coverage
for the APIs, clients and operating conditions you rely on; see
[Growing coverage toward production](../../../docs/getting-started.md#widen-coverage).

<details>
<summary>Why these checks relate to the guide</summary>

The base tests every guide gets (each API, error handling, streaming and cancellation, the parsers and the context window), through the autoscaled pool. The `queue` profiles also check that flow control is on: a router with flow control adds `x-llm-d-flow-queue-duration-ms` to every request it admits, even one at a time, and `contract.header_disposition` checks for it. No declaration depends on the replica count, so a run that overlaps a scale event (a second replica joining the pool, or staying Pending) is still valid; the scaling itself is not tested: see [Not covered](#not-covered).

The declaration rationale and claim-to-case mapping live in
[`parts/common.yaml`](parts/common.yaml). [Not covered](#not-covered) explains the remaining
access, instrumentation and load requirements.

</details>

## Pick your profile

Pick the profile for the optimized-baseline overlay you deployed and the router it sits behind: a router with flow control (the `queue` signal) looks different to a client from one without (the `inflight` signal), so they have separate profiles.

| Model a client sees | Router and scaling signal | Profile |
| --- | --- | --- | --- |
| Qwen3-32B on vLLM, with reasoning and tool-call parsers | flow control on, queue `ScaledObject` | [vllm-qwen3-32b-parsers-queue-standalone](vllm-qwen3-32b-parsers-queue-standalone.yaml) |
| Qwen3-32B on vLLM, with reasoning and tool-call parsers | optimized-baseline's, no flow control, inflight `ScaledObject` | [vllm-qwen3-32b-parsers-inflight-standalone](vllm-qwen3-32b-parsers-inflight-standalone.yaml) |
| Qwen3-32B on vLLM, reasoning inline, no tool parser | flow control on, queue `ScaledObject` | [vllm-qwen3-32b-inline-standalone](vllm-qwen3-32b-inline-standalone.yaml) |

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/keda-epp/<profile>.yaml
```

To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional for verification. Set them when comparing deployments that share a profile, so their recorded configuration dimensions remain distinct; for example `--identity accelerator=gpu --identity provider=gke --identity flavor=queue`, where `provider` is your overlay's `INFRA_PROVIDER` folder and `flavor` the guide's `SIGNAL` value.


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
- `openai_chat.disconnect_recovery`

**openai_completions**

- `openai_completions.basic_non_streaming`
- `openai_completions.streaming_matches_json`
- `openai_completions.stop_reason`

**anthropic_messages**

- `anthropic_messages.basic_non_streaming`
- `anthropic_messages.streaming_sse`
- `anthropic_messages.disconnect_recovery`

**openai_responses**

- `openai_responses.basic_non_streaming`
- `openai_responses.streaming_sse`
- `openai_responses.disconnect_recovery`

**streaming**

- `streaming.sse_framing`
- `streaming.cancel_then_next`
- `streaming.disconnect_before_output`

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

**scenarios**

- `scenarios.cancel_burst`


</details>

Counted offline by `llmdv plan`, one case per ingress and variant. Planned runs can still skip at runtime. Expand a profile for its scope counts and separate skip classes.

The default is also `--all-test-sets` for these profiles.

<details>
<summary>vllm-qwen3-32b-inline-standalone.yaml</summary>

[Open the profile](vllm-qwen3-32b-inline-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 95 | 83 | 12 |
| Smoke | 9 | 9 | 0 |
| Full library | 1094 | 680 | 414 |

Planned skips by class:

- **Default:** 12 `undeclared`.
- **Full library:** 414 `undeclared`.

</details>

<details>
<summary>vllm-qwen3-32b-parsers-inflight-standalone.yaml</summary>

[Open the profile](vllm-qwen3-32b-parsers-inflight-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 93 | 87 | 6 |
| Smoke | 9 | 9 | 0 |
| Full library | 1092 | 920 | 172 |

Planned skips by class:

- **Default:** 6 `undeclared`.
- **Full library:** 172 `undeclared`.

</details>

<details>
<summary>vllm-qwen3-32b-parsers-queue-standalone.yaml</summary>

[Open the profile](vllm-qwen3-32b-parsers-queue-standalone.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 95 | 89 | 6 |
| Smoke | 9 | 9 | 0 |
| Full library | 1094 | 922 | 172 |

Planned skips by class:

- **Default:** 6 `undeclared`.
- **Full library:** 172 `undeclared`.

</details>

Preview cases and reasons with `llmdv plan --profile <profile>`.

## Not covered

- **Every scaling claim** (`requires_load`, #23, plus reads of cluster objects): that the EPP's metrics reach Prometheus or Thanos, that the `ScaledObject` is Ready and its HPA exists, that a burst raises `desiredReplicas` and a drain lowers it, and overshoot while pods start.
- **Flow control** on the `queue` profiles' deployments, beyond its response header: queueing, priority and shedding need load, as in [flow-control](../flow-control/README.md). The objective and fairness headers skip as `requires_load`.
- **The `saturation` signal**: llm-d does not test it nightly, and no profile here covers it.

`parts/common.yaml` maps each of the guide's claims.

## For maintainers

<details>
<summary>llm-d's nightly lanes, and how each maps to a profile</summary>

The profiles test the router (EPP) each lane exposes, in standalone mode, through `../parts/topology/standalone.yaml`. Each lane installs the router under its own helm release, so the Service differs per lane (each lane's `gateway_host`).

| CI lane (`nightly-e2e-workload-autoscaling-keda-epp-<lane>`) | Reusable workflow, deploy script (`guides/workload-autoscaling/scripts/`) | Overlay it renders | Router | Service | Profile | Identity flags |
| --- | --- | --- | --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `reusable-nightly-e2e-gke`, `nightly-deploy-gke-keda-epp.sh` | `optimized-baseline/modelserver/gpu/vllm/gke` | flow control on, queue `ScaledObject` | `<release>-gke-epp` | `vllm-qwen3-32b-parsers-queue-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=queue` |
| `ibm-acc-gpu-vllm-x` | `reusable-nightly-e2e-openshift`, `nightly-deploy-ocp-keda-epp.sh` | `optimized-baseline/modelserver/gpu/vllm/base` | optimized-baseline's, no flow control, inflight `ScaledObject` | `<release>-inflight-epp` | `vllm-qwen3-32b-parsers-inflight-standalone` | `--identity accelerator=gpu --identity provider=base --identity flavor=inflight` |
| `amd-acc-rocm-vllm-x` | `reusable-nightly-e2e-amd`, `nightly-deploy-amd-keda-epp.sh` | `optimized-baseline/modelserver/amd/vllm/base` | flow control on, queue `ScaledObject` | `<release>-rocm-epp` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=base --identity flavor=queue` |

Each lane's deploy script names its own router release; `<release>` stands for the prefix it sets, and the Service is in the lane's `gateway_host`. The provider value is the overlay's `INFRA_PROVIDER` folder, and the flavor is the guide's `SIGNAL` value, the `ScaledObject` overlay the lane applies. The `gke` and `ibm` lanes serve the same model configuration but do not share a profile: a router with flow control adds `x-llm-d-flow-queue-duration-ms` to every request it admits, even one at a time, and one without sends no such header. The `queue` combinations declare it `promised`, so `contract.header_disposition` checks that flow control is on.

**Replicas change during a lane.** Each lane starts the model server at one replica, and its validator (`.github/scripts/e2e/e2e-validate-scale-event.sh`) drives the HPA to two and back. A run that overlaps a scale event may see a second replica join the pool, or stay Pending. No declaration here depends on the replica count.

</details>

<details>
<summary>Where llm-d's own sources disagree</summary>

- The workflows pass `guide_name: workload-autoscaling`, not the `guide.yaml` name `keda-epp`.
- `guide.yaml` has no `support:` table, so no configuration is marked `validated`; these profiles follow the three lanes. The guide's README says the `inflight` and `queue` signals have nightly coverage.
- The `ibm` lane passes `image_override: docker.io/vllm/vllm-openai:nightly` to `reusable-nightly-e2e-openshift`, while the overlay it renders names `vllm-openai:v0.31.0`. If the override applies to the model server, that lane runs a different vLLM build from the `gke` lane, which serves the same model configuration; `llmdv compare` across their runs shows any difference.
- `guide.yaml`'s `EPP_SERVICE` and `INFERENCE_POOL` defaults assume optimized-baseline's router release; every lane uses its own release name instead, to avoid colliding with other InferencePools on a shared cluster.

</details>
