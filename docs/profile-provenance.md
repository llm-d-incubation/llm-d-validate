# Profile evidence and provenance

Use this guide when a profile needs measured baselines, several configurations, or preparation
for public use. [Writing a profile](writing-profiles.md) covers the basic workflow and intended
behavior; [the profile reference](../profiles/REFERENCE.md) owns field semantics.

## Measure deployment facts

Read the advertised model ID and any reported context length from `GET /v1/models` at each
reachable ingress. A gateway may use a different model ID from the engine. Record missing
information as unknown; do not infer it from a sibling deployment.

If you have read-only cluster access, keep its `environment` settings in a local profile.
Use selectors in `setup.requires`, never fixed pod names. Confirm the kubeconfig context before
using `profile show --setup` to resolve declared pods.

Probe one reasoning request to identify the parser's output field. Recent vLLM can expose
`reasoning`; older vLLM, SGLang and LiteLLM can expose `reasoning_content`. Inline `<think>` text
uses `parser: null`. Keep the evidence for the specific build; product names alone do not
establish the wire shape.

Probe surfaces with a shape-valid body. An empty `{}` can produce a validation error instead
of proving whether a route exists. A promised route returning 404 remains a finding.

For LiteLLM, check model aliases, `GET /v1/model/info`, health routing and rate limiting at the
gateway. For a directly reached llm-d router, confirm its authentication and model ID rather
than copying the gateway's. Router builds differ in whether malformed bodies produce JSON or
plain text; consult [measured ground truth](../.agents/GROUND-TRUTH.md) before declaring an
accepted envelope deviation.

Review fleet policy with the deployment owner. `dedicated` (the default) permits disruptive
cases within the selected scope. Use `shared` when required for a shared deployment; those cases
skip as `excluded_by_policy`. Record the reason in the profile.

## Record probes separately from expectations

Probe each behavior line and record the result in a comment. The probe never sets the line.
Probe a gateway at the gateway: its own validation runs before any engine sees the request.

| Line | Send | Note in the comment |
|---|---|---|
| `error_status.unknown_model` | a model id that does not exist | a gateway may answer 400 where an engine answers 404 |
| `error_status.unknown_path` | a path that does not exist | status and body |
| `error_status.wrong_method` | `GET` on a POST-only path | status and body |
| `error_status.bad_credentials` | a bogus credential | usually only the gateway authenticates |
| `unstructured_errors` | a truncated body | parseable envelope or prose |
| `headers` | a request carrying each header | only status and response headers are client-observable |

Keep probe bodies in files so shell quoting cannot change the request. Preserve redacted request
and response evidence, including the build and vantage point.

## Seed change sentinels

`prompt_pins`, `render_pins` and `contract_baseline` take values only a run supplies. Declare
the placeholders `llmdv profile validate` accepts:

| Sentinel | Placeholder |
|---|---|
| `prompt_pins` | `1` per pin under each surface: `{chat_completions: {conversation: 1, tools: 1}}` (no `tools` pin under `completions`) |
| `render_pins` | 64 zeros under each declared render surface and pin: `{chat_render: {conversation: "000…0", tools: "000…0"}}` |
| `contract_baseline` part | `{llmdv_seed: [absent]}` |

Run `llmdv verify --profile <profile> --evidence all --full-suite` against a known-good build
(`--full-suite`, because a profile's `test_sets.default` may leave the pin cases out). Each pin
fails with the observed value in its `canonical_input_tokens` or `canonical_render_digest`
check; each contract part passes with its exchange in `exchanges.jsonl`, which
`llmdv.assertions.contract` (`shape`, `stream_shape`, `baseline`) turns into a baseline. Replace
the placeholders.

**Render.** The render cases run only on the `*_render` surfaces an ingress declares: `render.stability` and `render.unknown_model` on each, `render.synthetic_prefix` only with `prefix_stability`, and a render pin only under a declared render surface. `contract` checks surfaces both ways, so declare a render surface only where the stack is meant to serve it; otherwise leave it out with a comment saying so, and those cases skip as `undeclared`. The cache keys that depend on rendering (`render_equivalent`, `stripped_system_prefix`, `equivalent_settings`, `canonical_tool_keys`) are in [REFERENCE.md](../profiles/REFERENCE.md#cache_reuse).

## More than one configuration: parts and combinations

When one stack needs several profiles, write each fact once in a **part** and list parts in a
**combination**, the file you run. Worked examples:
[`profiles/llm-d/`](../profiles/llm-d/README.md) and
[`profiles/examples/custom-llm-d/`](../profiles/examples/custom-llm-d/).

- A part holds one kind of fact (what the APIs promise, a layer, an engine, a model configuration). It is not a profile, and it never extends anything: a file reached by two routes is an error.
- Anything inside a `parts/` folder is a building block; every other file is a profile you can run. What only one group of profiles shares goes in that group's own `parts/common.yaml` (`profiles/llm-d/<guide>/parts/common.yaml`), so the group's name appears once, as its folder.
- A combination is `extends: [parts/…, …]`, a `name`, and only what belongs to that one stack: the engine's own names (parser names, the reasoning field), block size, per-layer differences and the change sentinels.
- Name a model part by what a client sees (model, context window, parsers), never by accelerator or cluster. Configurations that give a client the same model, window, parsers and engine share one combination; tell their runs apart with `--identity accelerator=<value> --identity provider=<value>` (and `--identity llm-d-ref=<value>` when applicable), not `--label`. For an llm-d guide, follow llm-d's own lane inputs: `accelerator` is the guide's `ACCELERATOR_TYPE`, `provider` the `INFRA_PROVIDER` folder the lane renders (with any connector the guide folds into it, such as `nixl/amd-ci`; `base` when the lane sets none), and every other variable the guide's lanes vary (connector, variant, topology, scaler) goes into one `--identity flavor=`, llm-d's values joined with `/` in overlay-path order (`flavor=lmcache-connector/cpu`), passed on every lane of that guide, rather than a new profile or a label.
- Tag every line with where its value came from: `FROM values: <path>` (shipped configuration), `FROM docs: <path>`, `INTENT` ([intended behavior](writing-profiles.md#declare-intended-behaviour)), `DERIVED`, or `MEASURED on <where>`. Nothing unmeasured is tagged measured.
- Mappings merge; ordinary lists replace; `null` clears where allowed. Ingress lists merge by `id`, so `ingresses: []` does not remove inherited ingresses. Inspect unexpected overrides with `profile explain <combination> --field <field>`.
- Validate and plan a combination, never a part: `llmdv profile validate <combination>` and `llmdv plan --profile <combination> -q`.

### Profiles for an llm-d guide

To find which guides and lanes need this after llm-d moves, use
[tracking-llm-d-guide-drift](../.agents/skills/tracking-llm-d-guide-drift/SKILL.md).

Decide which configurations get a profile from the guide itself, and read all of its sources,
checking each against the others:

- the nightly lanes, `.github/workflows/nightly-e2e-<guide>-*.yaml`, and the deploy scripts they call;
- the rendered manifests of the overlay each lane applies (`kubectl kustomize <path>`, locally, never against a cluster), for the model, context window, parsers, engine build and flags;
- the guide's `guide.yaml`, including any `support:` table;
- the guide's README, including any support or supported-accelerators section.

Write a profile for each configuration a nightly lane deploys, as the rendered manifests show
it, and none for any other. `guide.yaml` and the README are statements to check against the
lanes and manifests, not the source of truth. Wherever they disagree (a lane deploying something
the table or README does not list, a table cell no lane runs, a README naming a different model
or flag than the manifest), profile what the lane deploys and record the disagreement under
"Where llm-d's own sources disagree" in the guide folder's README (in its "For maintainers"
section), citing paths. Say "guide", the word llm-d's own files use (`guides/`, `guide.yaml`,
`GUIDE_NAME`), and name the folder exactly as llm-d names the guide; do not write "well-lit
path" or any other term for it.

1. List the guide's nightly workflows in llm-d and the deploy scripts they call. For each lane read its accelerator, engine, provider and gateway class.
2. Render the overlay that lane applies, locally (`kubectl kustomize <path>`, never against a cluster). A lane that sets no provider applies the accelerator and engine folder's own kustomization. Read the model, context window, parsers, engine build and flags, for each role (prefill and decode can differ). Check what you read against `guide.yaml` and the README.
3. Group lanes that look the same to a client. Each group is one combination; its header names its lanes, and the directory's README maps every lane to its profile with the exact `--identity` flags that keep lanes sharing a combination apart, lists what no lane runs, and records under "Where llm-d's own sources disagree" every disagreement between the guide's sources. The README is for someone who deployed the guide, not for CI: it opens with "Pick your profile" (profile, the model a client sees, the overlays it covers), "Run it", "What it tests", the generated "What `llmdv verify` selects" and "Not covered", and keeps the lane table, the identity derivation and the disagreements between llm-d's sources in collapsed `<details>` blocks under "For maintainers". Keep lane and CI wording out of the sections above it, and keep each lane name in backticks: the drift skill's report matches them. [`tiered-prefix-cache/README.md`](../profiles/llm-d/tiered-prefix-cache/README.md) is the model.
4. Give the guide's `parts/common.yaml` a `test_sets.default` of the cases the guide's claims touch, keeping `contract.*` (see [REFERENCE.md](../profiles/REFERENCE.md#test_sets)), and check every glob with `llmdv plan --profile <combination>`. The [coverage rule](#coverage-and-known-failures) still holds for the configuration: one lane per configuration runs `--full-suite`, recorded in `profiles/llm-d/README.md` under "Which lane runs everything", as does a lane whose engine build differs from the rest of its configuration.
5. A layer the kinds cannot order (a routing sidecar between the router and the engines, two routers in series, a coordinator in front of the router) is placed with `fronts`; see [Declared layer edges](../profiles/REFERENCE.md#declared-layer-edges). An ingress may front only ingresses at its own position or deeper.

#### How profiles/llm-d is built

**A sum, not a product.** The guide profiles share router declarations, engine parts, model configurations, and a topology: the standalone router (`parts/topology/standalone.yaml`). A gateway topology is added when a lane runs gateway mode. One file per combination would repeat every fact in dozens of files, so each fact lives in one part and a combination lists the parts:

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

A combination is `extends: [../parts/base.yaml, ../parts/topology/<t>.yaml,
../parts/engines/<e>.yaml, ../parts/models/<m>.yaml, ../parts/sentinels/<s>.yaml,
parts/common.yaml]`, then its `name` and the parser names (the engine's own flag values, where
the model part has parsers). The sentinel part is `unmeasured-with-tools.yaml` when the model
has a tool-call parser, `unmeasured.yaml` otherwise. Once a run of that exact stack measures
them, the combination sets its own `prompt_pins` and `contract_baseline`, which override the
part.

- **No key is set by two parts,** so the order of the list changes nothing. The exceptions, each on purpose: the simulator's model part overrides five `base.yaml` keys, and the simulator, the precise-prefix-cache-routing GPU, AMD and XPU and the fast-model-actuation combinations override the standalone surfaces, and each says so in a comment. A run's `profile.used.yaml` names the file that set each key.
- **`tests/test_example_profiles.py` checks every part** through the combinations that list it.
- **Ingress-keyed facts live in the topology part.** `llmdv` keys surfaces, error statuses and header dispositions by ingress id (`router` in the standalone topology), so ingress-keyed facts such as the SLO headers, which the guides leave without effect, are declared in the topology part, not in a guide's `parts/common.yaml`.
- **Model parts are named by the configuration a client sees** (`qwen3-32b-ctx16k-parsers`), never by accelerator; their headers list the overlays that use them.

**Which source owns which fact:**

| Source | Owns |
| --- | --- |
| the guide's nightly lanes and the deploy scripts they call | which configurations get a profile |
| the guide's `guide.yaml` and README | what the guide says exists and is supported, checked against the lanes and manifests (a disagreement goes under "Where llm-d's own sources disagree"); how a client reaches the stack |
| router values and model-server overlays | the deployed configuration: model id, window, parsers, router plugins, block sizes |
| the APIs (OpenAI, Anthropic, the EPP reference) | behaviour: statuses, envelopes, fields, header handling |
| a run | existence (surfaces served, the window an engine resolves) and the truth of every line above |

**Provenance tags**, in the comment above every declaration:

| Tag | Means |
| --- | --- |
| `FROM values: <path>` | read from the guide's shipped configuration at the revision recorded in [guide coverage](../profiles/llm-d/README.md#for-maintainers), overlays rendered with `kubectl kustomize` |
| `FROM docs: <path>` | read from the llm-d documentation at that commit, or from this repository's profile reference |
| `INTENT` | how a correct stack behaves, as the API itself defines it |
| `DERIVED` | computed from the model's own config, not stated by the guide (a default context window) |
| `MEASURED on <vantage and stack>` | observed through the named vantage; state which layer the evidence establishes |
| inferred | a reading of the config no source states outright; the comment says so |

Behaviour lines change only after the team triages a failure, and the comment then cites the
decision.

### Publishing a profile written against a private stack

1. **Scrub the comments, keep the values.** Remove hostnames and URLs, namespaces, cluster and context names, pod and Service names, company, team and people's names, dates, ticket and issue references, run ids and internal model aliases. Rewrite a measurement as `MEASURED on one deployment`. A measured value is a fact of the stack: replacing it with a generic default makes a different profile.
2. **Replace** addresses and keys with `base_url_env` and `auth.key_env`, internal model aliases with the public model id, and pins and baselines with the [sentinel placeholders](#seed-change-sentinels). Drop `known_failures` entries that cite private tickets.
3. **Keep a private overlay.** A gitignored `<name>.local.yaml` that `extends` the public file restores the model ids your layers serve, your variable names, and your pins and `known_failures`.
4. **Prove it before the PR.** `PYTHONPATH=src python .agents/skills/writing-an-infra-profile/compare_profiles.py <private profile> <name>.local.yaml <ingress id>` prints every effective setting that differs at that ingress; run it once per ingress, and nothing should differ. Then search the diff, the commit message and the PR text for each kind of identifier in step 1.

## Identify layers with evidence

Before naming LiteLLM, an llm-d router or EPP, or vLLM in a profile, comment, page or answer,
gather read-only evidence and state a confidence:

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

The full list of what each header, body field and marker reveals is the evidence table,
`src/llmdv/ingress/signals.py`; a row marked `backend` (including `x-litellm-model-id` and
`x-litellm-deployment`) may name a pod, host or Service and is never quoted. A product that
names its build in a header the table does not know is declared per ingress with
`version_headers` ([REFERENCE.md](../profiles/REFERENCE.md#ingresses)).

## Coverage and known failures

Declare every value `llmdv`'s vocabularies define (in `profile/schema.py`,
`profile/transport.py` and `surfaces/base.py` under `src/llmdv/`) for the target surfaces, even
one an upstream API does not document. Honour schema constraints (surface names from
`SURFACE_PATHS`; disjoint lists such as `chat_reasoning_efforts` and `chat_quiet_efforts`). "Not
probed yet" is never a reason to leave something out: the run is the measurement.

Leave a case out when its feature is outside the intended contract, its required access or
instrument is unavailable (no cluster or metrics access, no instrument, no second ingress), or
it executes model-written code on the operator's machine. A promised route returning 404 is a
failure to investigate, not an exclusion.

For llm-d guide validation, a profile's `test_sets.default` narrows what one lane runs; another
lane runs the same configuration with `--full-suite` (see [Profiles for an llm-d
guide](#profiles-for-an-llm-d-guide)). For a configuration whose canonical lane should exercise
the library, invoke that lane with `--full-suite`; a profile without `test_sets.default`
requires an explicit coverage choice.

### Preserve failures for triage

A failing case stays. Never edit the profile to remove a failure or pre-excuse one.

A failure the team has filed and traced to a fix that has not reached the fleet goes in
`known_failures` ([REFERENCE.md](../profiles/REFERENCE.md#known_failures)), which names the
check, the issue and the build the fix ships in. The case still fails and the run still exits
`1`; the label only tells the reader it is known. An entry takes `check`, `cases`, `issue`,
`fix` (e.g. `"upstream issue: <ref> (state) | fix PR: <ref>"`), optionally `ingress` (one id or
a list) and `until` (quoted version strings); an entry that matches no failure although a case
it covers ran is reported as unmatched, which says the fix may have landed. A decision that a
behaviour is intended is not a known failure: record it as `TRIAGED <date>` in the comment
beside the line and, if the declaration changes, cite the decision.
