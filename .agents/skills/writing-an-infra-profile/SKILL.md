---
name: writing-an-infra-profile
description: Use when writing, updating or reviewing an `llmdv` infra-profile; when choosing error_status, unstructured_errors, header dispositions, efforts, capabilities or change sentinels; when deciding which case families to leave out; when tempted to declare what a probe returned; before naming LiteLLM, an llm-d router or EPP, or vLLM behind an ingress; when one stack needs several profiles (models, engines, an llm-d guide's CI lanes); or when publishing a profile that was written against a private stack.
---

# Writing an infra-profile

A profile states what a **correct** stack does at each ingress you can reach. Every line is checked against the live fleet by the cases that use it (the contract suite checks declared surfaces in both directions, and header dispositions), so each refusal or deviation shows up as a failure the team triages: false positive, intended, or a real defect. A profile fitted to today's behaviour reports green by not looking.

Field reference: [`profiles/REFERENCE.md`](../../../profiles/REFERENCE.md). How to start: [`profiles/README.md`](../../../profiles/README.md).

## Procedure

1. **Choose a starting point.** Reuse a committed profile when it describes the intended deployment. For a new stack without a matching profile, start with `examples/example-minimal.yaml` and add the contracts it promises; fuller examples cover setup and model-specific declarations:

   | Start from | Ingress kinds | Use for |
   |---|---|---|
   | `examples/example-minimal.yaml` | one endpoint; choose its position | a first custom profile with model id, URL/key variables and expected error statuses |
   | `examples/example-engine-only.yaml` | `engine` (vLLM) | a single model server reached directly, with nothing in front |
   | `examples/example-gateway-only.yaml` | `gateway` (LiteLLM) | a model behind a LiteLLM gateway: a URL and a key are all you have |
   | `examples/example-two-ingress.yaml` | `engine` + `router` | engine and router, with port-forwards |
   | `examples/example-three-ingress.yaml` | `gateway` + `router` + `engine` | all three layers; a template to measure against |
   | `examples/custom-llm-d/glm-5.3.yaml` | `gateway` + `router` | a full profile for a custom llm-d stack, built from shared parts with `extends` |

   A variant of an existing profile (another namespace, an in-cluster copy) is a short file with `extends: <parent>.yaml` that names only what differs; see `examples/example-two-ingress-staging.yaml`. Several configurations of one stack (models, engines, an llm-d guide's CI lanes) share parts; see [More than one configuration](#more-than-one-configuration-parts-and-combinations).

   The minimal starter is an authoring scaffold, not complete coverage. Use `plan` to review its gaps and declare eligible features before treating it as a finished validation profile; a failed probe is not a reason to disable a promised feature.

   Declare only the layers you can reach. One ingress is a complete profile; a declared but unreachable layer fails preflight.

2. **Put it at `profiles/<system>/<model>.yaml`.** A copy with real cluster detail is `profiles/<system>/<model>.local.yaml`, which is gitignored. A committed profile names its URL and key by variable (`base_url_env`, `auth.key_env`) and never stores either; a `.local.yaml` may write `base_url` inline, but declaring both is an error. An unset variable passes `llmdv profile validate` and fails preflight (exit 3). No namespace, cluster name, context, hostname, pod or Service name in a committed file. A committed profile gets a short note on how to run it beside it: a `README.md` in its folder, one per folder for a set of combinations, as each llm-d guide has.

3. **Fill what exists by measuring it.** Model id and context length from the gateway's `GET /v1/models` (a gateway's ids are usually not the engine's). If you have cluster access, the `environment` block (`cluster`, `context`, `namespace`, `kubeconfig`, `metrics`; optional, and no gateway-only profile has one); check the kubeconfig holds the context, or `profile show --setup` prints `-l <selector>` instead of a pod. Selectors, never pod names, in `setup.requires`. The reasoning shape from one request: a populated reasoning field means declare `parser` and the `field` you saw (`reasoning` on recent vLLM; `reasoning_content` on older vLLM, on SGLang and through LiteLLM, which renames it); an inline `<think>` block means `parser: null`. Probe surfaces with a **shape-valid** body at each layer (an empty `{}` answers 400 on an absent route). Compare observations with the intended surfaces; a promised route returning 404 is a finding, not a reason to remove its declaration.

   Per layer:
   - **LiteLLM (`kind: gateway`):** `rate_limited: true` (a 429 then skips as `throttled`), `health_path: /health/liveliness`, `capabilities.model_info` from its `GET /v1/model/info`. Its model id is the gateway's (often prefixed, e.g. `<provider>/…`, and suffixed per gateway, e.g. `-staging`), not the engine's.
   - **The llm-d router (`kind: router`):** no auth (`{type: none}`), the engine's model id, and no `/v1/model/info`. Older EPP builds answer some malformed bodies with plain text (`error extracting request body: …`) and newer ones with a JSON envelope, so measure yours and see [`.agents/GROUND-TRUTH.md`](../../GROUND-TRUTH.md) before declaring `unstructured_errors`.

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

   Run `llmdv verify --profile <profile> --evidence all --full-suite` against a known-good build (`--full-suite`, because a profile's `test_sets.default` may leave the pin cases out). Each pin fails with the observed value in its `canonical_input_tokens` or `canonical_render_digest` check; each contract part passes with its exchange in `exchanges.jsonl`, which `llmdv.assertions.contract` (`shape`, `stream_shape`, `baseline`) turns into a baseline. Replace the placeholders.

   **Render.** The render cases run only on the `*_render` surfaces an ingress declares: `render.stability` and `render.unknown_model` on each, `render.synthetic_prefix` only with `prefix_stability`, and a render pin only under a declared render surface. `contract` checks surfaces both ways, so declare a render surface only where the stack is meant to serve it; otherwise leave it out with a comment saying so, and those cases skip as `undeclared`. The cache keys that depend on rendering (`render_equivalent`, `stripped_system_prefix`, `equivalent_settings`, `canonical_tool_keys`) are in [REFERENCE.md](../../../profiles/REFERENCE.md#cache_reuse).

7. **Validate and run the contract suite:**
   ```sh
   llmdv profile validate <profile>          # offline: schema and consistency
   llmdv profile explain <profile>           # effective values and source assignments
   llmdv profile explain <profile> --field 'ingresses[gateway].model' # use your ingress id
   llmdv profile show <profile> --matrix     # surfaces per ingress: what runs, what skips
   llmdv plan --profile <profile> -q         # offline: cases that would run and skip, by skip class
   llmdv profile show <profile> --setup      # resolved port-forwards
   llmdv verify --profile <profile> --full-suite -m contract
   ```
   `explain` focuses on declared fields; `--field` also shows omitted fields and library defaults. Source assignments follow precedence order: global model/capability defaults apply before ingress overrides, even if a later file changes the global value. This identifies the source file, not the evidence or rationale behind a declaration; keep provenance comments.

   For a standalone copy, use `llmdv profile show <profile> --yaml > /tmp/profile-expanded.yaml`. It removes `extends`, preserves the profile name and environment-variable references, and anchors relative client executables. It does not expand all library defaults or preserve original comments. Keep the sources for editing and write the export to a different path. Both authoring views hide environment-derived values; literals in the source remain visible, so export is not a publishing scrub.

   Expect the first contract run to find errors. That is the suite working.

8. **List every empty capability field.** Each must be an exclusion named in the profile header with its reason.

## Test sets for shorter runs

Keep the deployment contract intact when a user wants less coverage. `test_sets.default` remains the
profile's default list of case-id globs; `test_sets: {quick-api: [<case-id>, ...]}` defines
optional alternatives. `--test-set NAME` replaces the default; `--full-suite` is mutually exclusive
and bypasses it. Repeat `--test-set` to combine named sets; `--all-test-sets` combines all
profile-defined sets. These modes are mutually exclusive. A plain run without `default` exits 2;
never silently fall back to the full suite. CLI name wildcards are invalid. Other selectors narrow scope.

Use `profile show <profile> --test-sets` to list built-in and custom subsets, then
`plan --profile <profile> --test-set smoke` to preview the built-in quick check. The same flag
works on `verify` and `job`. Respect a user's request for a subset; do not silently run the full
suite. Report the chosen test sets and run/skip counts as partial coverage. Full validation of
an llm-d guide configuration still follows the lane coverage requirements below.

Share custom names through `extends`; their lists replace, and `null` removes a name. Built-in
names are reserved. See [test sets](../../../profiles/REFERENCE.md#test_sets)
for the schema and the exact scope of smoke.

## More than one configuration: parts and combinations

When one stack needs several profiles, write each fact once in a **part** and list parts in a **combination**, the file you run. Worked examples: [`profiles/llm-d/`](../../../profiles/llm-d/README.md) and [`profiles/examples/custom-llm-d/`](../../../profiles/examples/custom-llm-d/).

- A part holds one kind of fact (what the APIs promise, a layer, an engine, a model configuration). It is not a profile, and it never extends anything: a file reached by two routes is an error.
- Anything inside a `parts/` folder is a building block; every other file is a profile you can run. What only one group of profiles shares goes in that group's own `parts/common.yaml` (`profiles/llm-d/<guide>/parts/common.yaml`), so the group's name appears once, as its folder.
- A combination is `extends: [parts/…, …]`, a `name`, and only what belongs to that one stack: the engine's own names (parser names, the reasoning field), block size, per-layer differences and the change sentinels.
- Name a model part by what a client sees (model, context window, parsers), never by accelerator or cluster. Configurations that give a client the same model, window, parsers and engine share one combination; tell their runs apart with `--identity accelerator=<value> --identity provider=<value>` (and `--identity llm-d-ref=<value>` when applicable), not `--label`. For an llm-d guide, follow llm-d's own lane inputs: `accelerator` is the guide's `ACCELERATOR_TYPE`, `provider` the `INFRA_PROVIDER` folder the lane renders (with any connector the guide folds into it, such as `nixl/amd-ci`; `base` when the lane sets none), and every other variable the guide's lanes vary (connector, variant, topology, scaler) goes into one `--identity flavor=`, llm-d's values joined with `/` in overlay-path order (`flavor=lmcache-connector/cpu`), passed on every lane of that guide, rather than a new profile or a label.
- Tag every line with where its value came from: `FROM values: <path>` (shipped configuration), `FROM docs: <path>`, `INTENT` (Rule 1), `DERIVED`, or `MEASURED on <where>`. Nothing unmeasured is tagged measured; a profile no run has exercised says `WRITTEN, NOT RUN` in its header.
- Mappings merge; ordinary lists replace; `null` clears where allowed. Ingress lists merge by `id`, so `ingresses: []` does not remove inherited ingresses. Inspect unexpected overrides with `profile explain <combination> --field <field>`.
- Validate and plan a combination, never a part: `llmdv profile validate <combination>` and `llmdv plan --profile <combination> -q`.

### Profiles for an llm-d guide

To find which guides and lanes need this after llm-d moves, use [tracking-llm-d-guide-drift](../tracking-llm-d-guide-drift/SKILL.md).

Decide which configurations get a profile from the guide itself, and read all of its sources, checking each against the others:

- the nightly lanes, `.github/workflows/nightly-e2e-<guide>-*.yaml`, and the deploy scripts they call;
- the rendered manifests of the overlay each lane applies (`kubectl kustomize <path>`, locally, never against a cluster), for the model, context window, parsers, engine build and flags;
- the guide's `guide.yaml`, including any `support:` table;
- the guide's README, including any support or supported-accelerators section.

Write a profile for each configuration a nightly lane deploys, as the rendered manifests show it, and none for any other. `guide.yaml` and the README are statements to check against the lanes and manifests, not the source of truth. Wherever they disagree (a lane deploying something the table or README does not list, a table cell no lane runs, a README naming a different model or flag than the manifest), profile what the lane deploys and record the disagreement under "Upstream notes" in the guide folder's README, citing paths. Say "guide", the word llm-d's own files use (`guides/`, `guide.yaml`, `GUIDE_NAME`), and name the folder exactly as llm-d names the guide; do not write "well-lit path" or any other term for it.

1. List the guide's nightly workflows in llm-d and the deploy scripts they call. For each lane read its accelerator, engine, provider and gateway class.
2. Render the overlay that lane applies, locally (`kubectl kustomize <path>`, never against a cluster). A lane that sets no provider applies the accelerator and engine folder's own kustomization. Read the model, context window, parsers, engine build and flags, for each role (prefill and decode can differ). Check what you read against `guide.yaml` and the README.
3. Group lanes that look the same to a client. Each group is one combination; its header names its lanes, and the directory's README maps every lane to its profile with the exact `--identity` flags that keep lanes sharing a combination apart, lists what no lane runs, and records under "Upstream notes" every disagreement between the guide's sources.
4. Give the guide's `parts/common.yaml` a `test_sets.default` of the cases the guide's claims touch, keeping `contract.*` (see [REFERENCE.md](../../../profiles/REFERENCE.md#test_sets)), and check every glob with `llmdv plan --profile <combination>`. Rule 3 still holds for the configuration: one lane per configuration runs `--full-suite`, recorded in `profiles/llm-d/README.md` under "Which lane runs everything", as does a lane whose engine build differs from the rest of its configuration.
5. A layer the kinds cannot order (a routing sidecar between the router and the engines, two routers in series, a coordinator in front of the router) is placed with `fronts`; see [Declared layer edges](../../../profiles/REFERENCE.md#declared-layer-edges). An ingress may front only ingresses at its own position or deeper.

#### How profiles/llm-d is built

**A sum, not a product.** A guide ships one router configuration, several engines, many model configurations, and one topology today: the standalone router (`parts/topology/standalone.yaml`). A gateway topology is added when a lane runs gateway mode. One file per combination would repeat every fact in dozens of files, so each fact lives in one part and a combination lists the parts:

```
profiles/llm-d/
  parts/                               shared by every guide; never run on their own
    base.yaml                          what every guide's stack shares: API intent, engine-independent
    topology/standalone.yaml           the EPP's own listener: the ingress, error statuses, header dispositions
    engines/<engine>.yaml              an engine's own request extensions (vllm, sglang, atom)
    models/<model-config>.yaml         model id, context window, reasoning and tool-call shape, vision
    sentinels/unmeasured*.yaml         placeholder change sentinels until a run measures them
  <guide>/                             one folder per guide, named as llm-d names it
    parts/common.yaml                  what every profile of the guide shares: its claims, cache decisions, block sizes
    <engine>-<model-config>[-<variant>]-<topology>.yaml   a combination: what you run
```

A combination is `extends: [../parts/base.yaml, ../parts/topology/<t>.yaml, ../parts/engines/<e>.yaml, ../parts/models/<m>.yaml, ../parts/sentinels/<s>.yaml, parts/common.yaml]`, then its `name` and the parser names (the engine's own flag values, where the model part has parsers). The sentinel part is `unmeasured-with-tools.yaml` when the model has a tool-call parser, `unmeasured.yaml` otherwise. Once a run of that exact stack measures them, the combination sets its own `prompt_pins` and `contract_baseline`, which override the part.

- **No key is set by two parts,** so the order of the list changes nothing. The exceptions, each on purpose: the simulator's model part overrides five `base.yaml` keys, and the simulator, the precise-prefix-cache-routing GPU, AMD and XPU and the fast-model-actuation combinations override the standalone surfaces, and each says so in a comment. A run's `profile.used.yaml` names the file that set each key.
- **`tests/test_example_profiles.py` checks every part** through the combinations that list it.
- **Ingress-keyed facts live in the topology part.** `llmdv` keys surfaces, error statuses and header dispositions by ingress id (`epp` in the standalone topology), so ingress-keyed facts such as the SLO headers, which the guides leave without effect, are declared in the topology part, not in a guide's `parts/common.yaml`.
- **Model parts are named by the configuration a client sees** (`qwen3-32b-ctx16k-parsers`), never by accelerator; their headers list the overlays that use them.

**Which source owns which fact:**

| Source | Owns |
| --- | --- |
| the guide's nightly lanes and the deploy scripts they call | which configurations get a profile |
| the guide's `guide.yaml` and README | what the guide says exists and is supported, checked against the lanes and manifests (a disagreement goes under "Upstream notes"); how a client reaches the stack |
| router values and model-server overlays | the deployed configuration: model id, window, parsers, router plugins, block sizes |
| the APIs (OpenAI, Anthropic, the EPP reference) | behaviour: statuses, envelopes, fields, header handling |
| a run | existence (surfaces served, the window an engine resolves) and the truth of every line above |

**Provenance tags**, in the comment above every declaration:

| Tag | Means |
| --- | --- |
| `FROM values: <path>` | read from the guide's shipped configuration at llm-d `9ca8ff0`, overlays rendered with `kubectl kustomize` |
| `FROM docs: <path>` | read from the llm-d documentation at that commit, or from this repository's profile reference |
| `INTENT` | how a correct stack behaves, as the API itself defines it |
| `DERIVED` | computed from the model's own config, not stated by the guide (a default context window) |
| `MEASURED 2026-11-16 on kind (real EPP, simulator engine)` | observed through a real endpoint picker in front of llm-d-inference-sim; a fact about the router layer only |
| inferred | a reading of the config no source states outright; the comment says so |

Behaviour lines change only after the team triages a failure, and the comment then cites the decision.

### Publishing a profile written against a private stack

1. **Scrub the comments, keep the values.** Remove hostnames and URLs, namespaces, cluster and context names, pod and Service names, company, team and people's names, dates, ticket and issue references, run ids and internal model aliases. Rewrite a measurement as `MEASURED on one deployment`. A measured value is a fact of the stack: replacing it with a generic default makes a different profile.
2. **Replace** addresses and keys with `base_url_env` and `auth.key_env`, internal model aliases with the public model id, and pins and baselines with the placeholders of step 6. Drop `known_failures` entries that cite private tickets.
3. **Keep a private overlay.** A gitignored `<name>.local.yaml` that `extends` the public file restores the model ids your layers serve, your variable names, and your pins and `known_failures`.
4. **Prove it before the PR.** `PYTHONPATH=src python .agents/skills/writing-an-infra-profile/compare_profiles.py <private profile> <name>.local.yaml <ingress id>` prints every effective setting that differs at that ingress; run it once per ingress, and nothing should differ. Then search the diff, the commit message and the PR text for each kind of identifier in step 1.

## Rule 1: declare intent; probes go in comments

| The line says | Declare | Example |
|---|---|---|
| deployment facts | configuration and measurements, with their sources recorded | model id, context window, health path, KV block size |
| what the deployment **promises to serve** | the intended surfaces and capabilities; record probes separately | a promised route returning 404 remains declared and fails |
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

The full list of what each header, body field and marker reveals is the evidence table, `src/llmdv/ingress/signals.py`; a row marked `backend` (including `x-litellm-model-id` and `x-litellm-deployment`) may name a pod, host or Service and is never quoted. A product that names its build in a header the table does not know is declared per ingress with `version_headers` ([REFERENCE.md](../../../profiles/REFERENCE.md#ingresses)).

## Rule 3: everything eligible runs

Declare every value `llmdv`'s vocabularies define (in `profile/schema.py`, `profile/transport.py` and `surfaces/base.py` under `src/llmdv/`) for the target surfaces, even one an upstream API does not document. Honour schema constraints (surface names from `SURFACE_PATHS`; disjoint lists such as `chat_reasoning_efforts` and `chat_quiet_efforts`). "Not probed yet" is never a reason to leave something out: the run is the measurement.

Leave a case out when its feature is outside the intended contract, its required access or instrument is unavailable (no cluster or metrics access, no instrument, no second ingress), or it executes model-written code on the operator's machine. A promised route returning 404 is a failure to investigate, not an exclusion.

A profile's `test_sets.default` is not a way to leave a case out of a configuration: it narrows what one lane runs, and only when another lane runs the same configuration with `--full-suite` (see [Profiles for an llm-d guide](#profiles-for-an-llm-d-guide)). For a configuration whose canonical lane should exercise the library, invoke that lane with
`--full-suite`; a profile without `test_sets.default` requires an explicit coverage choice.

## Rule 4: failures are left for the team

A failing case stays. Never edit the profile to remove a failure or pre-excuse one.

A failure the team has filed and traced to a fix that has not reached the fleet goes in
`known_failures` ([REFERENCE.md](../../../profiles/REFERENCE.md#known_failures)), which names the check, the issue and the build the fix ships in.
The case still fails and the run still exits `1`; the label only tells the reader it is known.
An entry takes `check`, `cases`, `issue`, `fix` (e.g. `"upstream issue: <ref> (state) | fix PR: <ref>"`),
optionally `ingress` (one id or a list) and `until` (quoted version strings); an entry that matches no
failure although a case it covers ran is reported as unmatched, which says the fix may have landed.
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
| "The table or README lists it, so it needs a profile" | Only what a nightly lane deploys gets one; record the difference under "Upstream notes". |
| "The guide has no support table, so nothing qualifies" | The lanes decide: profile what each one deploys. |
| "One file per accelerator" | Name by what a client sees; lanes that look the same share a profile. |
