# agentic-api

Profiles for llm-d's [agentic-api guide](https://github.com/llm-d/llm-d/tree/main/guides/agentic-api) at llm-d `9ca8ff0`. The guide puts vLLM Agentic API (`vllm/agentic-api:v0.9.0`, Service `agentic-api` on port 9000), backed by PostgreSQL, in front of an llm-d router and optimized-baseline's vLLM model server. Every profile here is WRITTEN, NOT RUN.

**These profiles cover only the router.** They test the guide's llm-d router (EPP), in standalone mode, through `../parts/topology/standalone.yaml`. The helm release is `agentic-api`, so the Service is `<GUIDE_NAME>-epp` on port 80 (the lane's `gateway_host`). Its router config is optimized-baseline's, unchanged, so the router behaves as optimized-baseline's does.

## Lanes

| CI lane (`nightly-e2e-agentic-api-<lane>`) | Reusable workflow | Overlay it renders | Profile | Identity flags |
| --- | --- | --- | --- | --- |
| `gke-acc-gpu-vllm-x` | `reusable-nightly-e2e-gke` | `agentic-api/modelserver/gpu/vllm/gke` (= `optimized-baseline/modelserver/gpu/vllm/gke`) | `vllm-qwen3-32b-parsers-standalone` | `--identity accelerator=gpu --identity provider=gke` |

The lane's deploy script is `guides/agentic-api/scripts/nightly-deploy-gke.sh` (one model-server replica), and its validator is `.github/scripts/e2e/e2e-validate-agentic-api.sh`, which runs the guide's `verify.py` against agentic-api.

```sh
export LLMDV_EPP_URL=<the EPP's URL>
llmdv verify --profile profiles/llm-d/agentic-api/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

## Not covered: agentic-api itself

The guide's own subject is not covered: health and `/v1/models` on agentic-api, stateful `/v1/responses` continued by `previous_response_id` from PostgreSQL, the webhook tool loop (`function_call`, then `function_call_output` against `previous_response_id`), and WebSocket `/v1/responses`. `parts/common.yaml` maps each claim.

### Follow-up

To cover agentic-api, a profile declares a second ingress in the gateway position (`kind: gateway`) for the `agentic-api` Service on port 9000, in front of the router, with `fronts: [epp]` naming the router it sends to (#24, A5). That ingress then needs:

- its own surfaces and error statuses, measured: which paths agentic-api serves (at least `responses`, and whether it passes chat completions and completions through), what it answers for an unknown model, path or method, and whether its errors carry the OpenAI envelope;
- stateful Responses cases (`store`, `previous_response_id`, a tool loop continued by `function_call_output`) that llmdv's Responses suite can run against a stateful ingress;
- a WebSocket transport for `ws://…/v1/responses`, which llmdv does not have.

Until a run measures agentic-api's behaviour, nothing about it is declared.

## Upstream notes

- `guide.yaml` marks only `gpu`/`vllm` `validated`, with providers `base` and `gke`; the one lane runs `gke`.
- The guide's model server keeps optimized-baseline's labels (`llm-d.ai/guide: optimized-baseline`), and the router selects on that label.
- `guide.yaml`'s support notes describe 2 replicas; the lane runs one (`nightly-deploy-gke.sh`, CI-only deviation 1).
