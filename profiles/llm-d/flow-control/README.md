# flow-control

Profiles for testing a deployment of llm-d's [flow-control](https://github.com/llm-d/llm-d/tree/main/guides/flow-control) guide, in which the llm-d router runs with its `flowControl` feature gate on (priority bands 100, 0 and -10, round-robin fairness and FCFS ordering) in front of optimized-baseline's vLLM model servers.

[What it tests](#what-it-tests) · [Pick your profile](#pick-your-profile) · [Run it](#run-it) · [What `llmdv verify` selects](#what-llmdv-verify-selects) · [Not covered](#not-covered) · [For maintainers](#for-maintainers)

## What it tests

**Validation focus: admission signals and cancellation recovery.** Check the queue-duration header and cancel-burst behavior to detect a missing flow-control gate or requests that leave the path unable to serve.

**What a pass establishes:** the checks that actually executed held at the reachable ingress
during this run. Skipped checks remain coverage gaps. **Coverage boundary:** Priority, fairness, sustained queueing and shedding need a bounded load experiment.

These focused defaults are a starting point for validating a production deployment. Add coverage
for the APIs, clients and operating conditions you rely on; see
[Growing coverage toward production](../../../docs/getting-started.md#widen-coverage).

<details>
<summary>Why these checks relate to the guide</summary>

Flow control is work-conserving: a request that finds a free slot dispatches at once. `llmdv` runs cases sequentially, although individual cases can send concurrent requests. For an uncontended request, a client sees the same API behavior as optimized-baseline, with one additional response header: flow control adds `x-llm-d-flow-queue-duration-ms` (the time the request waited) to every request it admits, before the first byte, and the router sends no such header without it (llm-d-router `pkg/epp/handlers/response.go`, at the `main` build `guides/env.sh` pins). `parts/common.yaml` declares it `promised`, so `contract.header_disposition` fails a router running without flow control.

On top of the base tests every guide gets (each API, error handling, the parsers and the context window), the profiles send cancelled and disconnected requests, which must still be served and must free their slot. One case sends concurrent requests: `scenarios.cancel_burst` opens eight streams at once. Under the setting llm-d tests with (`maxConcurrency: 4`, one replica), four dispatch and four wait in the EPP until a cancelled stream frees a slot, well inside `defaultRequestTTL` (60s) and the standard band's 200-request limit. A rejection there is a finding.

The declaration rationale and claim-to-case mapping live in
[`parts/common.yaml`](parts/common.yaml). [Not covered](#not-covered) explains the remaining
access, instrumentation and load requirements.

</details>

## Pick your profile

Pick the profile for the optimized-baseline model-server overlay you deployed.

| Model a client sees | Overlay | Profile |
| --- | --- | --- |
| Qwen3-32B on vLLM, with reasoning and tool-call parsers | `optimized-baseline/modelserver/gpu/vllm/gke` | [vllm-qwen3-32b-parsers-standalone](vllm-qwen3-32b-parsers-standalone.yaml) |
| Qwen3-32B on vLLM, reasoning inline, no tool parser | `optimized-baseline/modelserver/amd/vllm/base` | [vllm-qwen3-32b-inline-standalone](vllm-qwen3-32b-inline-standalone.yaml) |

## Run it

The profiles test the guide's router (the EPP). Give `llmdv` its URL:

```sh
export LLMDV_ROUTER_URL='https://your-router.example' # replace with your router's URL
llmdv verify --profile 'profiles/llm-d/flow-control/<profile>.yaml'
```

To test a gateway or the engine as well, [add it as another ingress](../../README.md#include-a-gateway-or-engine). `--identity` flags are optional for verification. Set them when comparing deployments that share a profile, so their recorded configuration dimensions remain distinct; for example `--identity accelerator=gpu --identity provider=gke`, read off your overlay path.


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
<summary>vllm-qwen3-32b-parsers-standalone.yaml</summary>

[Open the profile](vllm-qwen3-32b-parsers-standalone.yaml).

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

The guide's other claims are about contention, and none of them is covered yet:

- **Queueing, backpressure, priority, fairness and shedding** (`requires_load`, #23). Holding requests past `maxConcurrency` × ready replicas, best-effort waiting longer than premium, round-robin between tenants in a band, and rejection with 429, 503 or 500 carrying `x-llm-d-request-dropped-reason`. llm-d's own test forces these with `maxConcurrency: 4` and one replica; `llmdv` has no load mode to produce them.
- **Classification.** The objective and fairness headers (`x-llm-d-inference-objective`, `x-llm-d-inference-fairness-id`, and their deprecated `x-gateway-inference-*` aliases) are declared `consumed` in the topology part, which skips as `requires_load`. That declaration still holds with flow control on: the EPP reads them and their effect shows only under contention.
- **The feature gate's log line, `maxConcurrency` and the EPP's `llm_d_epp_flow_control_*` series.** They need EPP logs, config reads and the metrics port, which a committed profile does not carry. The `x-llm-d-flow-queue-duration-ms` header is what a client sees of the gate.

`parts/common.yaml` maps each of the guide's claims to a case or a gap.

## For maintainers

<details>
<summary>llm-d's nightly lanes, and how each maps to a profile</summary>

Both lanes run `guides/flow-control/scripts/nightly-deploy-gke.sh` as a `custom_deploy_script`, which renders optimized-baseline's model-server overlay, relabels it for this guide and cuts it to one replica. They then run `.github/scripts/e2e/e2e-validate-flow-control.sh`.

| CI lane (`nightly-e2e-flow-control-<lane>`) | Reusable workflow | Overlay it renders | Profile | Identity flags |
| --- | --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `reusable-nightly-e2e-gke` | `optimized-baseline/modelserver/gpu/vllm/gke` | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |
| `amd-ci-acc-rocm-vllm-x` | `reusable-nightly-e2e-amd` | `optimized-baseline/modelserver/amd/vllm/base` | `vllm-qwen3-32b-inline-standalone` | `--identity accelerator=amd --identity provider=base` |

The provider value is the `INFRA_PROVIDER` the lane passes to the deploy script.

The helm release is `flow-control`, so the Service is `<GUIDE_NAME>-epp` on port 80 (the lanes' `gateway_host`).

</details>

<details>
<summary>Where llm-d's own sources disagree</summary>

- The guide's `guide.yaml` has no `support:` table, so no configuration is marked `validated` there; these profiles follow the two lanes. Its `INFRA_PROVIDER` values are `base` and `gke`, and its `deploy.modelserver` step names only the `gpu` overlay; the AMD path is documented in the README and run by the `amd-ci` lane.
- The README's "Default Configuration" table says 2 replicas; both lanes run one (`nightly-deploy-gke.sh`, CI-only deviation 3 of 3).
- The AMD lane runs the script named `nightly-deploy-gke.sh` through `reusable-nightly-e2e-amd`.

</details>
