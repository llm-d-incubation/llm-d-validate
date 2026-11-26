# Measured profiles for three models

Broad profiles for three models served by llm-d, based on measurements from real deployments,
with every identifying detail removed. Each declares what each layer served (surfaces, error
statuses, capabilities, headers and change sentinels) and how a correct stack
behaves, so a plain run selects every case in the library.

| Profile | Model | Layers |
| --- | --- | --- |
| [`glm-5.3.yaml`](glm-5.3.yaml) | `zai-org/GLM-5.3` | `litellm` (a LiteLLM gateway), `router` (the llm-d router, reached directly) |
| [`deepseek-v4.1-flash-1017.yaml`](deepseek-v4.1-flash-1017.yaml) | `deepseek-ai/DeepSeek-V4.1-Flash-1017` | `litellm` |
| [`muse-spark-open-816b-nvfp4.yaml`](muse-spark-open-816b-nvfp4.yaml) | `meta-models/Muse-Spark-Open-816B-A42B-NVFP4` | `litellm`, `router` |

## Start with smoke

Choose a profile whose declarations match your intended model configuration. After
[installation](../../../docs/getting-started.md#install), load the endpoint and credential
variables from your environment or credential store. For GLM at the gateway:

```sh
export P=profiles/examples/custom-llm-d/glm-5.3.yaml
export LLMDV_GATEWAY_URL='https://your-gateway.example'
# Load LLMDV_GATEWAY_KEY from your credential store.
# Optionally set LLMDV_GATEWAY_MODEL to the model ID your gateway serves.
llmdv plan --profile "$P" --ingress litellm --test-set smoke
llmdv verify --profile "$P" --ingress litellm --test-set smoke --analyze
```

For a direct router check, set `LLMDV_ROUTER_URL`, select `--ingress router`, and use
`LLMDV_ROUTER_MODEL` if its model ID differs. Omitting `--ingress` selects all declared ingresses,
so configure each one before doing so. Smoke does not run the real-client cases.

`LLMDV_GATEWAY_MODEL` and `LLMDV_ROUTER_MODEL` default to the public model ID when unset. A gateway often serves the model under its own
name, such as a provider prefix or a staging suffix; preflight refuses a run whose model id the
endpoint does not serve, and says so. Use a local file with `extends` for intended configuration differences. Change behavior
expectations only after triage; do not copy a failing response into the expected contract.

## Before broader coverage

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

After reviewing the declarations and client prerequisite, preview broader coverage:

```sh
llmdv plan --profile "$P" --ingress litellm
llmdv verify --profile "$P" --ingress litellm --analyze
```

These profiles' defaults select the whole library before applicability gates. Add
`-m "not real_clients"` to both commands if you have not installed the pinned client. Check
executed and skipped counts; a broad selection does not guarantee complete API coverage.
See [growing coverage toward production](../../../docs/getting-started.md#widen-coverage).

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
