# A custom llm-d deployment

Complete profiles for three models served by a deployment that follows none of llm-d's guides:
a LiteLLM gateway in front of the llm-d router in front of vLLM. Each was measured on real
deployments of each layer, with every identifying detail removed, and declares what each layer
served (surfaces, error statuses, capabilities, headers and change sentinels), so a plain run
selects every case in the library.

| Profile | Model | Layers |
| --- | --- | --- |
| [`glm-5.3.yaml`](glm-5.3.yaml) | `zai-org/GLM-5.3` | `litellm` (a LiteLLM gateway), `router` (the llm-d router, reached directly) |
| [`deepseek-v4.1-flash-1017.yaml`](deepseek-v4.1-flash-1017.yaml) | `deepseek-ai/DeepSeek-V4.1-Flash-1017` | `litellm` |
| [`muse-spark-open-816b-nvfp4.yaml`](muse-spark-open-816b-nvfp4.yaml) | `meta-models/Muse-Spark-Open-816B-A42B-NVFP4` | `litellm`, `router` |

## Run one

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
error status, another path) is a short gitignored `*.local.yaml` that `extends` the profile and
changes only that.

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

## How the files fit together

Each profile you run is a short combination that lists the parts it is built from with
`extends`, then sets only what is that stack's own: its cache settings and what each layer serves
differently. No setting is declared in two files, so the order of the list changes nothing.
Parts never run on their own.

| Part | Holds |
| --- | --- |
| [`parts/base.yaml`](parts/base.yaml) | what all three stacks share: the API contracts every layer met, the clients, the test sets |
| [`parts/proxy-litellm.yaml`](parts/proxy-litellm.yaml) | the LiteLLM gateway layer (`litellm`) |
| [`parts/router-llm-d.yaml`](parts/router-llm-d.yaml) | the llm-d router layer (`router`), for the two stacks measured at it |
| `parts/models/<model>.yaml` | the model as a client sees it, and the capabilities that differ between the stacks |
| `parts/known-failures/<model>.yaml` | that stack's known failures, each citing the public issue it waits on |
| `parts/sentinels/<model>.yaml` | that stack's change sentinels: prompt and render pins, and the contract baseline |

## What `llmdv verify` selects

A plain `llmdv verify` runs the `default` test set, defined once in [`parts/base.yaml`](parts/base.yaml) and used unchanged by every profile here. `--test-set smoke` runs the [built-in smoke checks](../../README.md#what-a-verification-command-selects), and `--full-suite` runs every case in the library. There are no other sets, so `--all-test-sets` is the same as the default. Filters such as `-k` only narrow the run, and `--identity` never changes it.

The default is `*`: every case in the library, as `--full-suite` selects.

<details>
<summary>How many cases each scope selects, per profile</summary>

Counted offline by `llmdv plan`, one per ingress and variant. *Run* cases can still skip during a run (for example `throttled`); *skip* cases are ruled out by the profile's declarations, with their skip class.

| Profile | Default (= `--all-test-sets`) | `smoke` | `--full-suite` |
| --- | --- | --- | --- |
| `deepseek-v4.1-flash-1017.yaml` | 1076 selected, 1044 run, 32 skip: 32 undeclared | 9 selected, 9 run | 1076 selected, 1044 run, 32 skip: 32 undeclared |
| `glm-5.3.yaml` | 2142 selected, 2032 run, 110 skip: 110 undeclared | 18 selected, 18 run | 2142 selected, 2032 run, 110 skip: 110 undeclared |
| `muse-spark-open-816b-nvfp4.yaml` | 2142 selected, 2107 run, 35 skip: 35 undeclared | 18 selected, 18 run | 2142 selected, 2107 run, 35 skip: 35 undeclared |

</details>

To see every case and why one skips: `llmdv plan --profile <profile>` (add `--test-set smoke` or `--full-suite` for the other scopes), and `llmdv profile show <profile> --test-sets` for the definitions.
