# flow-control

Profiles for llm-d's [flow-control guide](https://github.com/llm-d/llm-d/tree/main/guides/flow-control) at llm-d `9ca8ff0`: the llm-d router with its `flowControl` feature gate on, priority bands 100, 0 and -10, round-robin fairness and FCFS ordering, in front of optimized-baseline's vLLM model servers.

The profiles test the router (EPP) each lane exposes, in standalone mode, through `../parts/topology/standalone.yaml`. The helm release is `flow-control`, so the Service is `<GUIDE_NAME>-epp` on port 80 (the lanes' `gateway_host`).

## Lanes

Both lanes run `guides/flow-control/scripts/nightly-deploy-gke.sh` as a `custom_deploy_script`, which renders optimized-baseline's model-server overlay, relabels it for this guide and cuts it to one replica. They then run `.github/scripts/e2e/e2e-validate-flow-control.sh`.

| CI lane (`nightly-e2e-flow-control-<lane>`) | Reusable workflow | Overlay it renders | Profile | Identity flags |
| --- | --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `reusable-nightly-e2e-gke` | `optimized-baseline/modelserver/gpu/vllm/gke` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `amd-ci-acc-rocm-vllm-x` | `reusable-nightly-e2e-amd` | `optimized-baseline/modelserver/amd/vllm/base` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=base` |

The provider value is the `INFRA_PROVIDER` the lane passes to the deploy script.

```sh
export LLMDV_EPP_URL=<the EPP's URL>
llmdv verify --profile profiles/llm-d/flow-control/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

## What `llmdv verify` selects

This applies to every runnable profile in this folder: `vllm-qwen3-32b-inline-standalone.yaml`, `vllm-qwen3-32b-parsers-standalone.yaml`.

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
| `vllm-qwen3-32b-parsers-standalone.yaml` | 95 selected, 89 run, 6 skip (6 undeclared) | 9 selected, 9 run | 1084 selected, 913 run, 171 skip (171 undeclared) |

Other filters (`-k`, `-m`, `--surface`, `--client`, `--ingress`) only narrow the chosen scope, and a filter that leaves no case exits 2. `--identity` flags label the run and never change what it selects.

To inspect the definitions, the cases and every skip reason, offline:

```sh
P=profiles/llm-d/flow-control/vllm-qwen3-32b-inline-standalone.yaml
llmdv profile show $P --test-sets        # every set and its patterns
llmdv plan --profile $P                  # the default: each case, and why one skips
llmdv plan --profile $P --test-set smoke
llmdv plan --profile $P --full-suite
llmdv plan --profile $P --json           # the same, as JSON
```
## What `llmdv` sees of flow control

Flow control is work-conserving: a request that finds a free slot dispatches at once. `llmdv` sends one request at a time, so on a correct stack a client sees this router as it would see optimized-baseline's, but for one response header: flow control adds `x-llm-d-flow-queue-duration-ms` (the time the request waited) to every request it admits, before the first byte, and the router sends no such header without it (llm-d-router `pkg/epp/handlers/response.go`, at the `main` build `guides/env.sh` pins). `parts/common.yaml` declares it `promised`, so `contract.header_disposition` fails a lane whose router runs without flow control. The guide's other claims are about contention, and none of them is covered yet:

- **NOT COVERED: queueing, backpressure, priority, fairness and shedding** (`requires_load`, #23). Holding requests past `maxConcurrency` × ready replicas, best-effort waiting longer than premium, round-robin between tenants in a band, and rejection with 429, 503 or 500 carrying `x-llm-d-request-dropped-reason`. The CI lane forces these with `maxConcurrency: 4` and one replica; `llmdv` has no load mode to produce them.
- **NOT COVERED: classification.** The objective and fairness headers (`x-llm-d-inference-objective`, `x-llm-d-inference-fairness-id`, and their deprecated `x-gateway-inference-*` aliases) are declared `consumed` in the topology part, which skips as `requires_load`. That declaration still holds with flow control on: the EPP reads them and their effect shows only under contention.
- **NOT COVERED: the feature gate's log line, `maxConcurrency` and the EPP's `llm_d_epp_flow_control_*` series.** They need EPP logs, config reads and the metrics port, which a committed profile does not carry. The header above is what a client sees of the gate.

One case sends concurrent requests: `scenarios.cancel_burst` opens eight streams at once. Under the CI setting, four dispatch and four wait in the EPP until a cancelled stream frees a slot, well inside `defaultRequestTTL` (60s) and the standard band's 200-request limit. A rejection there is a finding.

`parts/common.yaml` maps each of the guide's claims to a case or a gap.

## Upstream notes

- The guide's `guide.yaml` has no `support:` table, so no configuration is marked `validated` there; these profiles follow the two lanes. Its `INFRA_PROVIDER` values are `base` and `gke`, and its `deploy.modelserver` step names only the `gpu` overlay; the AMD path is documented in the README and run by the `amd-ci` lane.
- The README's "Default Configuration" table says 2 replicas; both lanes run one (`nightly-deploy-gke.sh`, CI-only deviation 3 of 3).
- The AMD lane runs the script named `nightly-deploy-gke.sh` through `reusable-nightly-e2e-amd`.
