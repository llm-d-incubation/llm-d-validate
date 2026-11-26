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

**All suites**

- `*`


</details>

Counted offline by `llmdv plan`, one case per ingress and variant. Planned runs can still skip at runtime. Expand a profile for its scope counts and separate skip classes.

The default is also `--all-test-sets` for these profiles.

<details>
<summary>deepseek-v4.1-flash-1017.yaml</summary>

[Open the profile](deepseek-v4.1-flash-1017.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 1076 | 1044 | 32 |
| Smoke | 9 | 9 | 0 |
| Full library | 1076 | 1044 | 32 |

Planned skips by class:

- **Default:** 32 `undeclared`.
- **Full library:** 32 `undeclared`.

</details>

<details>
<summary>glm-5.3.yaml</summary>

[Open the profile](glm-5.3.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 2142 | 2032 | 110 |
| Smoke | 18 | 18 | 0 |
| Full library | 2142 | 2032 | 110 |

Planned skips by class:

- **Default:** 110 `undeclared`.
- **Full library:** 110 `undeclared`.

</details>

<details>
<summary>muse-spark-open-816b-nvfp4.yaml</summary>

[Open the profile](muse-spark-open-816b-nvfp4.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 2142 | 2107 | 35 |
| Smoke | 18 | 18 | 0 |
| Full library | 2142 | 2107 | 35 |

Planned skips by class:

- **Default:** 35 `undeclared`.
- **Full library:** 35 `undeclared`.

</details>

Preview cases and reasons with `llmdv plan --profile <profile>`.
