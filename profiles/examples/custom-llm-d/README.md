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
llmdv verify --profile profiles/examples/custom-llm-d/glm-5.3.yaml --ingress litellm   # the gateway
llmdv verify --profile profiles/examples/custom-llm-d/glm-5.3.yaml --ingress router    # the router
llmdv verify --profile profiles/examples/custom-llm-d/glm-5.3.yaml                     # both
```

`LLMDV_ROUTER_MODEL` does the same for the router. A gateway often serves the model under its own
name, such as a provider prefix or a staging suffix; preflight refuses a run whose model id the
endpoint does not serve, and says so. A deployment that differs in a measured setting (another
error status, another path) is a short file that `extends` the profile and changes only that.

**Claude Code.** The client cases drive Claude Code 2.1.289, the version these layers were measured
with, and the profiles expect it at `/opt/claude-code/2.1.289/claude`. The runner image installs
a different version at `/usr/local/bin/claude`, so install this one beside it (Linux; use
`linux-arm64` on ARM):

```bash
sudo mkdir -p /opt/claude-code/2.1.289
sudo curl --fail --location -o /opt/claude-code/2.1.289/claude \
  https://downloads.claude.ai/claude-code-releases/2.1.289/linux-x64/claude
sudo chmod 755 /opt/claude-code/2.1.289/claude
```

Check it against the release's `manifest.json` checksum, as the `Containerfile` does. Without it,
leave the client cases out with `-m "not real_clients"`.

**Muse's contract baselines are not seeded.** GLM-5.3's and DeepSeek's `contract_baseline` hold
the response shapes measured on their deployments; Muse's holds the `llmdv_seed` placeholder in
each part, so its contract-drift cases compare against nothing. Seed it from a run of a healthy
deployment using [the seeding procedure](../../../docs/profile-provenance.md#seed-change-sentinels).

## What `llmdv verify` selects

The default is defined in each profile's own `test_sets:` (the copies are identical).
See [selection rules](../../../docs/cli.md#test-sets-and-coverage) for smoke, full-library coverage and filters.

<details>
<summary>The default's case-ID patterns</summary>

`*` selects the whole library; `suite.*` selects a suite. Other patterns select matching cases.

| Suite | Patterns |
| --- | --- |
| All suites | `*` |

</details>

<details>
<summary>How many cases each scope selects, per profile</summary>

Counted offline by `llmdv plan`, one per ingress and variant. *Run* cases can still skip during a run (for example `throttled`); *skip* cases are ruled out by the profile's declarations, with their skip class.

| Profile | Default (= `--all-test-sets`) | `smoke` | `--full-suite` |
| --- | --- | --- | --- |
| `deepseek-v4.1-flash-1017.yaml` | 1076 selected, 1044 run, 32 skip: 32 undeclared | 9 selected, 9 run | 1076 selected, 1044 run, 32 skip: 32 undeclared |
| `glm-5.3.yaml` | 2142 selected, 2032 run, 110 skip: 110 undeclared | 18 selected, 18 run | 2142 selected, 2032 run, 110 skip: 110 undeclared |
| `muse-spark-open-816b-nvfp4.yaml` | 2142 selected, 2107 run, 35 skip: 35 undeclared | 18 selected, 18 run | 2142 selected, 2107 run, 35 skip: 35 undeclared |

</details>

Preview individual cases and skip reasons with `llmdv plan --profile <profile>`.
