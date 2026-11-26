# Measured profiles for three models

Complete profiles for three models served by llm-d, measured on real deployments of each layer,
with every identifying detail removed. Each declares what each layer served (surfaces, error
statuses, capabilities, headers and change sentinels) and how a correct stack
behaves, so a plain run selects every case in the library.

| Profile | Model | Layers |
| --- | --- | --- |
| [`glm-5.3.yaml`](glm-5.3.yaml) | `zai-org/GLM-5.3` | `litellm` (a LiteLLM gateway), `router` (the llm-d router, reached directly) |
| [`deepseek-v4.1-flash-1017.yaml`](deepseek-v4.1-flash-1017.yaml) | `deepseek-ai/DeepSeek-V4.1-Flash-1017` | `litellm` |
| [`muse-spark-open-816b-nvfp4.yaml`](muse-spark-open-816b-nvfp4.yaml) | `meta-models/Muse-Spark-Open-816B-A42B-NVFP4` | `litellm`, `router` |

Point a profile at any deployment of its model, production or staging, with variables, and pick
the layers you can reach with `--ingress`:

```bash
export LLMDV_GATEWAY_URL=<the gateway's base URL> LLMDV_GATEWAY_KEY=<a key>
export LLMDV_GATEWAY_MODEL=<the id the gateway serves the model as>   # optional: the public id by default
export LLMDV_ROUTER_URL=<the router's base URL>
llmdv verify --profile profiles/examples/measured/glm-5.3.yaml --ingress litellm   # the gateway
llmdv verify --profile profiles/examples/measured/glm-5.3.yaml --ingress router    # the router
llmdv verify --profile profiles/examples/measured/glm-5.3.yaml                     # both
```

`LLMDV_ROUTER_MODEL` does the same for the router. A gateway often serves the model under its own
name, such as a provider prefix or a staging suffix; preflight refuses a run whose model id the
endpoint does not serve, and says so. A deployment that differs in a measured setting (another
error status, another path) is a short file that `extends` the profile and changes only that.

**Claude Code.** The client cases drive the Claude Code the runner image installs, 2.1.330 at
`/usr/local/bin/claude`, the same pin as every other committed profile. These layers were measured
with 2.1.289, so a client case that fails on a first run with 2.1.330 may be the version change
rather than the deployment. Outside the image, install 2.1.330 at that path as the
[deploy guide](../../../deploy/README.md) describes, checked against the release's `manifest.json`
checksum as the `Containerfile` does. Without it, leave the client cases out with
`-m "not real_clients"`.

**Muse's contract baselines are not seeded.** GLM-5.3's and DeepSeek's `contract_baseline` hold
the response shapes measured on their deployments; Muse's holds the `llmdv_seed` placeholder in
each part, so its contract-drift cases compare against nothing. Seed it from a run of a healthy
deployment, as the [profile skill](../../../.agents/skills/writing-an-infra-profile/SKILL.md)
describes (step 6).
