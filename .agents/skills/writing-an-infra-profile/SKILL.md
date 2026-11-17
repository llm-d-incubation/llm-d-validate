---
name: writing-an-infra-profile
description: Use when writing, updating or reviewing an llmdv infra-profile; when choosing error_status, unstructured_errors, header dispositions, efforts, capabilities or change sentinels; when deciding which case families to leave out; when tempted to declare what a probe returned; before naming LiteLLM, an llm-d router or EPP, or vLLM behind an ingress; when one stack needs several profiles (models, engines, an llm-d guide's CI lanes); or when publishing a profile that was written against a private stack.
---

# Writing an infra-profile

A profile states what a **correct** stack does at each ingress you can reach. Every line is checked against the live fleet by the cases that use it (the contract suite checks declared surfaces in both directions, and header dispositions), so each refusal or deviation shows up as a failure the team triages: false positive, intended, or a real defect. A profile fitted to today's behaviour reports green by not looking.

Field reference: [`profiles/REFERENCE.md`](../../../profiles/REFERENCE.md). Guide: [`profiles/README.md`](../../../profiles/README.md).

## Procedure

1. **Copy the closest profile.** A committed real profile for the same layer is the best start; the examples cover the shapes:

   | Start from | Ingress kinds | Use for |
   |---|---|---|
   | `examples/example-engine-only.yaml` | `vllm` | a single model server reached directly, with nothing in front |
   | `examples/example-gateway-only.yaml` | `litellm` | a model behind a LiteLLM gateway: a URL and a key are all you have |
   | `examples/example-two-ingress.yaml` | `vllm` + `llmd_router` | engine and router, with port-forwards |
   | `examples/example-three-ingress.yaml` | `litellm` + `llmd_router` + `vllm` | all three layers; a template to measure against |
   | `examples/custom-llm-d/glm-5.3.yaml` | `litellm` + `llmd_router` | a full profile for a custom llm-d stack, built from shared parts with `extends` |

   A variant of an existing profile (another namespace, an in-cluster copy) is a short file with `extends: <parent>.yaml` that names only what differs; see `examples/example-two-ingress-staging.yaml`. Several configurations of one stack (models, engines, an llm-d guide's CI lanes) share parts; see [More than one configuration](#more-than-one-configuration-parts-and-combinations).

   Declare only the layers you can reach. One ingress is a complete profile; a declared but unreachable layer fails preflight.

2. **Put it at `profiles/<system>/<model>.yaml`.** A copy with real cluster detail is `profiles/<system>/<model>.local.yaml`, which is gitignored. A committed profile names its URL and key by variable (`base_url_env`, `auth.key_env`) and never stores either; a `.local.yaml` may write `base_url` inline, but declaring both is an error. An unset variable passes `llmdv profile validate` and fails preflight (exit 3). No namespace, cluster name, context, hostname, pod or Service name in a committed file. A committed profile gets a short run guide next to it (`<model>.md`).

3. **Fill what exists by measuring it.** Model id and context length from the gateway's `GET /v1/models` (a gateway's ids are usually not the engine's). If you have cluster access, the `environment` block (`cluster`, `context`, `namespace`, `kubeconfig`, `metrics`; optional, and no gateway-only profile has one); check the kubeconfig holds the context, or `profile show --setup` prints `-l <selector>` instead of a pod. Selectors, never pod names, in `setup.requires`. The reasoning shape from one request: a populated reasoning field means declare `parser` and the `field` you saw (`reasoning` on recent vLLM; `reasoning_content` on older vLLM, on SGLang and through LiteLLM, which renames it); an inline `<think>` block means `parser: null`. Surfaces by sending a **shape-valid** body to each path at each layer (an empty `{}` answers 400 on an absent route).

   Per layer:
   - **`litellm`:** `rate_limited: true` (a 429 then skips as `throttled`), `health_path: /health/liveliness`, `capabilities.model_info` from its `GET /v1/model/info`. Its model id is the gateway's (often prefixed, e.g. `<provider>/…`, and suffixed per gateway, e.g. `-staging`), not the engine's.
   - **`llmd_router`:** no auth (`{type: none}`), the engine's model id, and no `/v1/model/info`. Older EPP builds answer some malformed bodies with plain text (`error extracting request body: …`) and newer ones with a JSON envelope, so measure yours and see [`.agents/GROUND-TRUTH.md`](../../GROUND-TRUTH.md) before declaring `unstructured_errors`.

   Declare `fleet_policy: dedicated` (the default): it runs the disruptive families too, and they have not disturbed the deployments they ran against. Use `shared`, which skips them as `excluded_by_policy`, only when the deployment's owner asks for it, and say so in a comment.

4. **Declare behaviour as a correct API defines it** (Rule 1). Every effort, field, feature, option, check and scenario the surfaces define (Rule 3). Clients last: each client's surfaces must be exposed by some ingress.

5. **Probe each behaviour line and record the result in a comment.** The probe never sets the line. Probe a gateway at the gateway: its own validation runs before any engine sees the request.

   | Line | Send | Note in the comment |
   |---|---|---|
   | `error_status.unknown_model` | a model id that does not exist | a gateway may answer 400 where an engine answers 404 |
   | `error_status.unknown_path` | a path that does not exist | status and body |
   | `error_status.wrong_method` | `GET` on a POST-only path | status and body |
   | `error_status.bad_credentials` | a bogus credential | usually only the gateway authenticates |
   | `unstructured_errors` | a truncated body | parseable envelope or prose |
   | `headers` | a request carrying each header | only status and response headers are client-observable |

   Bodies go in a file; see probing-a-fleet-before-asserting.

6. **Seed the change sentinels.** `prompt_pins`, `render_pins` and `contract_baseline` take values only a run supplies. Declare the placeholders `llmdv profile validate` accepts:

   | Sentinel | Placeholder |
   |---|---|
   | `prompt_pins` | `1` per pin under each surface: `{chat_completions: {conversation: 1, tools: 1}}` (no `tools` pin under `completions`) |
   | `render_pins` | 64 zeros under each declared render surface and pin: `{chat_render: {conversation: "000…0", tools: "000…0"}}` |
   | `contract_baseline` part | `{llmdv_seed: [absent]}` |

   Run `llmdv verify --profile <profile> --evidence all` against a known-good build. Each pin fails with the observed value in its `canonical_input_tokens` or `canonical_render_digest` check; each contract part passes with its exchange in `exchanges.jsonl`, which `llmdv.assertions.contract` (`shape`, `stream_shape`, `baseline`) turns into a baseline. Replace the placeholders.

   **Render.** The render cases run only on the `*_render` surfaces an ingress declares: `render.stability` and `render.unknown_model` on each, `render.synthetic_prefix` only with `prefix_stability`, and a render pin only under a declared render surface. `contract` checks surfaces both ways, so declare a render surface only where the stack is meant to serve it; otherwise leave it out with a comment saying so, and those cases skip as `undeclared`. The cache keys that depend on rendering (`render_equivalent`, `stripped_system_prefix`, `equivalent_settings`, `canonical_tool_keys`) are in [REFERENCE.md](../../../profiles/REFERENCE.md#cache_reuse).

7. **Validate and run the contract suite:**
   ```sh
   llmdv profile validate <profile>          # offline: schema and consistency
   llmdv profile show <profile> --matrix     # what runs, what skips and why
   llmdv profile show <profile> --setup      # resolved port-forwards
   llmdv verify --profile <profile> -m contract
   ```
   Expect the first contract run to find errors. That is the suite working.

8. **List every empty capability field.** Each must be an exclusion named in the profile header with its reason.

## More than one configuration: parts and combinations

When one stack needs several profiles, write each fact once in a **part** and list parts in a **combination**, the file you run. Worked examples: [`profiles/llm-d/`](../../../profiles/llm-d/README.md) and [`profiles/examples/custom-llm-d/`](../../../profiles/examples/custom-llm-d/).

- A part holds one kind of fact (what the APIs promise, a layer, an engine, a model configuration) and lives under `parts/`. It is not a profile, and it never extends anything: a file reached by two routes is an error.
- Anything inside a `parts/` folder is a building block; every other file is a profile you can run. What only one group of profiles shares goes in that group's own `parts/common.yaml` (`profiles/llm-d/<guide>/parts/common.yaml`), so the group's name appears once, as its folder.
- A combination is `extends: [parts/…, …]`, a `name`, and only what belongs to that one stack: the engine's own names (parser names, the reasoning field), block size, per-layer differences and the change sentinels.
- Name a model part by what a client sees (model, context window, parsers), never by accelerator or cluster. Configurations that give a client the same model, window, parsers and engine share one combination; tell their runs apart with `--label`.
- Tag every line with where its value came from: `FROM values: <path>` (shipped configuration), `FROM docs: <path>`, `INTENT` (Rule 1), `DERIVED`, or `MEASURED on <where>`. Nothing unmeasured is tagged measured; a profile no run has exercised says `WRITTEN, NOT RUN` in its header.
- Validate and plan a combination, never a part: `llmdv profile validate <combination>` and `llmdv plan --profile <combination> -q`.

### Profiles for an llm-d guide

Write a profile for each configuration one of the guide's CI lanes deploys, and none for a configuration no lane runs. Say "guide", the word llm-d's own files use (`guides/`, `guide.yaml`, `GUIDE_NAME`), and name the folder exactly as llm-d names the guide; do not write "well-lit path" or invent a second term.

1. List the guide's nightly workflows in llm-d. For each lane read its accelerator, engine, provider and gateway class.
2. Render the overlay that lane applies, locally (`kubectl kustomize <path>`, never against a cluster). A lane that sets no provider applies the accelerator and engine folder's own kustomization. Read the model, context window, parsers and engine build, for each role (prefill and decode can differ).
3. Group lanes that look the same to a client. Each group is one combination; its header names its lanes, and the directory's README maps every lane to its profile and lists what no lane runs.

### Publishing a profile written against a private stack

1. **Scrub the comments, keep the values.** Remove hostnames and URLs, namespaces, cluster and context names, pod and Service names, company, team and people's names, dates, ticket and issue references, run ids and internal model aliases. Rewrite a measurement as `MEASURED on one deployment`. A measured value is a fact of the stack: replacing it with a generic default makes a different profile.
2. **Replace** addresses and keys with `base_url_env` and `auth.key_env`, internal model aliases with the public model id, and pins and baselines with the placeholders of step 6. Drop `known_failures` entries that cite private tickets.
3. **Keep a private overlay.** A gitignored `<name>.local.yaml` that `extends` the public file restores the model ids your layers serve, your variable names, and your pins and `known_failures`.
4. **Prove it before the PR.** `PYTHONPATH=src python .agents/skills/writing-an-infra-profile/compare_profiles.py <private profile> <name>.local.yaml <ingress id>` prints every effective setting that differs, for each ingress; nothing should. Then search the diff, the commit message and the PR text for each kind of identifier in step 1.

## Rule 1: declare intent; probes go in comments

| The line says | Declare | Example |
|---|---|---|
| whether something **exists** here | what you measured | surfaces served, model id, context window, health path, KV block size |
| how something should **behave** | what a correct stack does, per the API's own definition | `invalid_api_version: [400]`, every feature `accepted`, `unstructured_errors: []` |

```yaml
# INTENT: a Messages endpoint refuses a nonsense anthropic-version, as Anthropic's API does.
# MEASURED <date>: 200, normal generation. Fails until the team triages it.
invalid_api_version: [400]
```

Change a behaviour line only after the team records a decision, cited in the comment (`TRIAGED <date>: false positive, text-only model`). Two deviations exist, both declared only after triage, and both verified in both directions so a layer that changes fails its declaration:
- `unstructured_errors: [<class>]`: the layer answers the class without a parseable envelope; the assertion inverts.
- a success status in `error_status` (e.g. `unsupported_option: [200]`): the layer does not enforce the class.

Header dispositions: declare the truth. `stripped`, `forwarded` and `rewritten` skip as `requires_instrumentation`, and `consumed` as `requires_load`; never downgrade one to an observable disposition for a green cell.

## Rule 2: no stack claim without read-only evidence

Before naming LiteLLM, an llm-d router or EPP, or vLLM in a profile, comment, page or answer, gather read-only evidence and state a confidence:

| Status | Requires |
|---|---|
| **confirmed** | the deployment owner says so; a read-only cluster object (Service, Route, Deployment); or a probe that reaches the component and gets its own signature. The ingress you call is reached directly, so its own identity header confirms **that** layer. |
| **likely, not confirmed** | indirect signals only, each listed with its weight |
| **unknown** | nothing observed |

Signals:
- LiteLLM: `x-litellm-version`; `x-litellm-model-name` (`hosted_vllm/…`); `x-litellm-model-api-base` and `GET /v1/model/info`, which name the next hop (an internal URL: describe it, never quote it).
- The llm-d router: an EPP error body `inference error: <Kind> - …`; `x-llm-d-*` response headers (e.g. `x-llm-d-flow-queue-duration-ms`); backend headers (`x-gateway-destination-endpoint`, `x-envoy-upstream-host`), which carry internal hostnames: cite them, never quote them.
- vLLM: `system_fingerprint: vllm-…` and `GET /version`. The fingerprint passes through every layer, so it identifies the engine behind an ingress, not the ingress.
- Weak on their own: `server: uvicorn` (vLLM and LiteLLM both send it) and `x-went-into-resp-headers` (seen through engine and router). Record the signals you looked for and did not find. Hostnames, sibling profiles, config flags and docs are hypotheses, never confirmation.

## Rule 3: everything eligible runs

Declare every value llmdv's vocabularies define (in `profile/schema.py`, `profile/transport.py` and `surfaces/base.py` under `src/llmdv/`) for the target surfaces, even one an upstream API does not document. Honour schema constraints (surface names from `SURFACE_PATHS`; disjoint lists such as `chat_reasoning_efforts` and `chat_quiet_efforts`). "Not probed yet" is never a reason to leave something out: the run is the measurement.

Leave a case out only when the thing it needs does not exist here (a 404 route, no cluster or metrics access, no instrument, no second ingress), or it executes model-written code on the operator's machine.

## Rule 4: failures are left for the team

A failing case stays. Never edit the profile to remove a failure or pre-excuse one.

A failure the team has filed and traced to a fix that has not reached the fleet goes in
`known_failures` (REFERENCE.md), which names the check, the issue and the build the fix ships in.
The case still fails and the run still exits `1`; the label only tells the reader it is known.
An entry takes `check`, `cases`, `issue`, `fix` (e.g. `"upstream issue: <ref> (state) | fix PR: <ref>"`),
optionally `ingress` (singular) and `until` (quoted version strings); an entry that matches no
failure is reported as unmatched, which says the fix landed.
A decision that a behaviour is intended is not a known failure: record it as `TRIAGED <date>` in the
comment beside the line and, if the declaration changes, cite the decision.

## Red flags

| Thought | Reality |
|---|---|
| "[200] was measured, so it's honest" | The measurement is the comment. The declaration is the correct API. |
| "A text-only model obviously refuses images" | Declare `accepted`; say so in the comment; the team marks it. |
| "The manifest says the parser is enabled" | Manifests are per-container; prefill and decode differ. |
| "The sentinels need baselines first" | Declare the placeholders; the first run supplies the values. |
| "They need green for sign-off" | A sign-off lists failures and their triage. |
| "It's a private repo, the namespace is fine" | It goes in `*.local.yaml`. |
| "To publish it, I'll swap our values for sensible defaults" | Scrub comments, keep values, and prove it with `compare_profiles.py`. |
| "The guide's table lists it, so it needs a profile" | Only what a CI lane deploys gets one. |
| "One file per accelerator" | Name by what a client sees; lanes that look the same share a profile. |
