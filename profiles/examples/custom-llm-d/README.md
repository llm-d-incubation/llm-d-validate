# Custom llm-d profiles for three models

Broad profiles for three models served by llm-d, based on measurements from real deployments,
with every identifying detail removed. Each describes a LiteLLM gateway (`litellm`) and a
directly reachable llm-d router (`router`), with API expectations, model capabilities and
layer-specific change sentinels. The default selects the existing library before applicability
gates; begin with smoke and review broader coverage below. The profiles are composed from shared
parts.

| Profile | Model | Layers |
| --- | --- | --- |
| [`glm-5.3.yaml`](glm-5.3.yaml) | `zai-org/GLM-5.3` | `litellm`, `router` |
| [`deepseek-v4.1-flash-1017.yaml`](deepseek-v4.1-flash-1017.yaml) | `deepseek-ai/DeepSeek-V4.1-Flash-1017` | `litellm`, `router` |
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
so configure each one before doing so. Both ingresses are available in all three profiles.
To check both, keep the gateway variables above and add:

```sh
export LLMDV_ROUTER_URL='https://your-router.example'
# Optionally set LLMDV_ROUTER_MODEL to the model ID your router serves.
llmdv plan --profile "$P" --test-set smoke
llmdv verify --profile "$P" --test-set smoke --analyze
```

Smoke does not run the real-client cases.

`LLMDV_GATEWAY_MODEL` and `LLMDV_ROUTER_MODEL` default to the public model ID when unset. A gateway often serves the model under its own
name, such as a provider prefix or a staging suffix; preflight refuses a run whose model id the
endpoint does not serve, and says so. Use a local file with `extends` for intended configuration differences. Change behavior
expectations only after triage; do not copy a failing response into the expected contract.

## Before broader coverage

**Claude Code.** The client cases drive the Claude Code the runner image installs, 2.1.330 at
`/usr/local/bin/claude`, the same pin as every other committed profile. These layers were measured
with 2.1.289. The deployment operator has since confirmed a successful live run with 2.1.330.
Include the client version when investigating a failure. Outside the image, install 2.1.330 at that path. The
[Containerfile](../../../Containerfile) shows the download and release-manifest checksum
verification steps. Without it, leave the client cases out with
`-m "not real_clients"`.

**Seed layer-specific baselines before using drift checks.** GLM's baselines and DeepSeek's
gateway baselines retain their measured values. DeepSeek's router uses the shared router
contract with model-specific capabilities; its prompt pins are `1` placeholders and its
response-shape baselines use `llmdv_seed`. Muse's response-shape baselines also use `llmdv_seed`.
Placeholder prompt pins will fail until seeded, and placeholder response shapes do not establish
drift coverage. Seed them from each intended healthy ingress using
[the seeding procedure](../../../docs/profile-provenance.md#seed-change-sentinels).

After reviewing the declarations and client prerequisite, preview broader coverage:

```sh
llmdv plan --profile "$P" --ingress litellm
llmdv verify --profile "$P" --ingress litellm --analyze
```

These profiles' defaults select the whole library before applicability gates. Add
`-m "not real_clients"` to both commands if you have not installed the pinned client. Check
executed and skipped counts; a broad selection does not guarantee complete API coverage.
See [growing coverage toward production](../../../docs/getting-started.md#widen-coverage).

## Adapt a profile built from parts

Run a combination from this directory, never a file under `parts/`. Each combination lists
its shared base, layer declarations, model, known failures and change sentinels in `extends`.
It then supplies cache settings and any ingress-specific overrides.

| File | Owns |
| --- | --- |
| `parts/base.yaml` | Shared API expectations, client pin and test sets |
| `parts/proxy-litellm.yaml`, `parts/router-llm-d.yaml` | Layer declarations |
| `parts/models/<model>.yaml` | Model configuration and model-specific capabilities |
| `parts/known-failures/<model>.yaml` | Tracked findings |
| `parts/sentinels/<model>.yaml` | Prompt pins and response-shape baselines |
| `<model>.yaml` | The runnable combination and its remaining overrides |

Keep deployment-specific edits in a gitignored local overlay, such as
`profiles/examples/custom-llm-d/my-stack.local.yaml` with `extends: glm-5.3.yaml`.
Review expected behavior before adapting it; a different observed response alone does not
justify changing an expectation. Use `profile explain` to trace a setting to its source and
`profile validate` and `plan` to check the composed result.

The profile names used by `compare --profile` are `custom-llm-d-glm-5.3`,
`custom-llm-d-deepseek-v4.1-flash-1017`, and `custom-llm-d-muse-spark-open-816b-nvfp4`.
Verification takes the YAML path; comparison takes the profile's `name:`.

## What `llmdv verify` selects

The default is defined once in [`parts/base.yaml`](parts/base.yaml) and used unchanged by every profile here.
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
| Default | 2140 | 2086 | 54 |
| Smoke | 18 | 18 | 0 |
| Full library | 2140 | 2086 | 54 |

Planned skips by class:

- **Default:** 54 `undeclared`.
- **Full library:** 54 `undeclared`.

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
