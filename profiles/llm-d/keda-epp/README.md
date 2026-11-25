# keda-epp

Profiles for llm-d's [keda-epp guide](https://github.com/llm-d/llm-d/tree/main/guides/workload-autoscaling/keda-epp) (`guides/workload-autoscaling/keda-epp`) at llm-d `9ca8ff0`: optimized-baseline's model server, autoscaled by a KEDA `ScaledObject` on an EPP metric (flow-control queue size and running requests, or in-flight requests).

The profiles test the router (EPP) each lane exposes, in standalone mode, through `../parts/topology/standalone.yaml`. Each lane installs the router under its own helm release, so the Service differs per lane (each lane's `gateway_host`).

## Lanes

| CI lane (`nightly-e2e-workload-autoscaling-keda-epp-<lane>`) | Reusable workflow, deploy script (`guides/workload-autoscaling/scripts/`) | Overlay it renders | Router | Service | Profile | Identity flags |
| --- | --- | --- | --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `reusable-nightly-e2e-gke`, `nightly-deploy-gke-keda-epp.sh` | `optimized-baseline/modelserver/gpu/vllm/gke` | flow control on, queue `ScaledObject` | `<release>-gke-epp` | `vllm-qwen3-32b-parsers-queue-standalone` | `--identity accelerator=gpu --identity provider=gke --identity flavor=queue` |
| `ibm-acc-gpu-vllm-x` | `reusable-nightly-e2e-openshift`, `nightly-deploy-ocp-keda-epp.sh` | `optimized-baseline/modelserver/gpu/vllm/base` | optimized-baseline's, no flow control, inflight `ScaledObject` | `<release>-inflight-epp` | `vllm-qwen3-32b-parsers-inflight-standalone` | `--identity accelerator=gpu --identity provider=base --identity flavor=inflight` |
| `amd-acc-rocm-vllm-x` | `reusable-nightly-e2e-amd`, `nightly-deploy-amd-keda-epp.sh` | `optimized-baseline/modelserver/amd/vllm/base` | flow control on, queue `ScaledObject` | `<release>-rocm-epp` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=base --identity flavor=queue` |

Each lane's deploy script names its own router release; `<release>` stands for the prefix it sets, and the Service is in the lane's `gateway_host`. The provider value is the overlay's `INFRA_PROVIDER` folder, and the flavor is the guide's `SIGNAL` value, the `ScaledObject` overlay the lane applies. The `gke` and `ibm` lanes serve the same model configuration but do not share a profile: a router with flow control adds `x-llm-d-flow-queue-duration-ms` to every request it admits, even one at a time, and one without sends no such header. The `queue` combinations declare it `promised`, so `contract.header_disposition` checks that flow control is on.

```sh
export LLMDV_EPP_URL=<the EPP's URL>
llmdv verify --profile profiles/llm-d/keda-epp/vllm-qwen3-32b-parsers-queue-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke --identity flavor=queue
```

**Replicas change during a lane.** Each lane starts the model server at one replica, and its validator (`.github/scripts/e2e/e2e-validate-scale-event.sh`) drives the HPA to two and back. A run that overlaps a scale event may see a second replica join the pool, or stay Pending. No declaration here depends on the replica count.

## What `llmdv verify` selects

This applies to every runnable profile in this folder: `vllm-qwen3-32b-inline-standalone.yaml`, `vllm-qwen3-32b-parsers-inflight-standalone.yaml`, `vllm-qwen3-32b-parsers-queue-standalone.yaml`.

**Default.** A `plan`, `verify` or `job` with no scope flag runs the `default` test set. It is defined once, in [`parts/common.yaml`](parts/common.yaml), and every profile above inherits it unchanged; none redefines it. There is no other profile-defined set.

**The `default` set's case-ID patterns, by the suite they select from** (12 suites, each confirmed by `llmdv plan`). A pattern ending in `.*` takes every case of its suite; any other names one case, or one variant in brackets. The rest of each suite is not selected:

| Suite | Patterns |
| --- | --- |
| `contract` | `contract.*` |
| `discovery` | `discovery.models_lists_declared_model`, `discovery.unknown_model_no_fallback`, `discovery.models_parses_with_openai_sdk` |
| `openai_chat` | `openai_chat.basic_non_streaming`, `openai_chat.streaming_sse`, `openai_chat.stop_reason`, `openai_chat.configured_reasoning`, `openai_chat.disconnect_recovery` |
| `openai_completions` | `openai_completions.basic_non_streaming`, `openai_completions.streaming_matches_json`, `openai_completions.stop_reason` |
| `anthropic_messages` | `anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse`, `anthropic_messages.disconnect_recovery` |
| `openai_responses` | `openai_responses.basic_non_streaming`, `openai_responses.streaming_sse`, `openai_responses.disconnect_recovery` |
| `streaming` | `streaming.sse_framing`, `streaming.cancel_then_next`, `streaming.disconnect_before_output` |
| `unhappy` | `unhappy.unknown_path`, `unhappy.malformed_json`, `unhappy.missing_model`, `unhappy.wrong_method`, `unhappy.error_envelope_fields`, `unhappy.context_overflow`, `unhappy.token_limit_over_context` |
| `transport` | `transport.connection`, `transport.health_status` |
| `tools` | `tools.auto_call`, `tools.auto_stream` |
| `long_context` | `long_context.*` |
| `scenarios` | `scenarios.cancel_burst` |

**Named sets:** `default` (above) and the built-in `smoke`, which every profile has ([its cases](../../README.md#what-a-verification-command-selects)). `--all-test-sets` combines the profile-defined sets only, so here it selects exactly `default`; `smoke` is never included in it.

**What each scope selects**, as `llmdv plan` counts it offline. Counts are case instances (one per ingress and variant). *Run* means the profile's declarations, capabilities and fleet policy let the case run; a case can still skip during the run (throttled, `requires_instrumentation`, or a declaration it reads). *Skip* names the skip classes:

| Profile | no flag, or `--test-set default`, or `--all-test-sets` | `--test-set smoke` | `--full-suite` (every case in the library) |
| --- | --- | --- | --- |
| `vllm-qwen3-32b-inline-standalone.yaml` | 95 selected, 83 run, 12 skip (12 undeclared) | 9 selected, 9 run | 1084 selected, 679 run, 405 skip (405 undeclared) |
| `vllm-qwen3-32b-parsers-inflight-standalone.yaml` | 93 selected, 87 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1082 selected, 911 run, 171 skip (171 undeclared) |
| `vllm-qwen3-32b-parsers-queue-standalone.yaml` | 95 selected, 89 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1084 selected, 913 run, 171 skip (171 undeclared) |

Other filters (`-k`, `-m`, `--surface`, `--client`, `--ingress`) only narrow the chosen scope, and a filter that leaves no case exits 2. `--identity` flags label the run and never change what it selects.

To inspect the definitions, the cases and every skip reason, offline:

```sh
P=profiles/llm-d/keda-epp/vllm-qwen3-32b-inline-standalone.yaml
llmdv profile show $P --test-sets        # every set and its patterns
llmdv plan --profile $P                  # the default: each case, and why one skips
llmdv plan --profile $P --test-set smoke
llmdv plan --profile $P --full-suite
llmdv plan --profile $P --json           # the same, as JSON
```
## Not covered

- **Every scaling claim** (`requires_load`, #23, plus reads of cluster objects): that the EPP's metrics reach Prometheus or Thanos, that the `ScaledObject` is Ready and its HPA exists, that a burst raises `desiredReplicas` and a drain lowers it, and overshoot while pods start.
- **Flow control** on the `gke` and `amd` lanes, beyond its response header: queueing, priority and shedding need load, as in [flow-control](../flow-control/README.md). The objective and fairness headers skip as `requires_load`.
- **The `saturation` signal**: no lane runs it.

`parts/common.yaml` maps each of the guide's claims.

## Upstream notes

- The workflows pass `guide_name: workload-autoscaling`, not the `guide.yaml` name `keda-epp`.
- `guide.yaml` has no `support:` table, so no configuration is marked `validated`; these profiles follow the three lanes. The guide's README says the `inflight` and `queue` signals have nightly coverage.
- The `ibm` lane passes `image_override: docker.io/vllm/vllm-openai:nightly` to `reusable-nightly-e2e-openshift`, while the overlay it renders names `vllm-openai:v0.31.0`. If the override applies to the model server, that lane runs a different vLLM build from the `gke` lane, which serves the same model configuration; `llmdv compare` across their runs shows any difference.
- `guide.yaml`'s `EPP_SERVICE` and `INFERENCE_POOL` defaults assume optimized-baseline's router release; every lane uses its own release name instead, to avoid colliding with other InferencePools on a shared cluster.
