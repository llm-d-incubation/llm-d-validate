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
| `render_pins` | A 64-zero string per pin under each declared render surface; see the copyable example below |
| `contract_baseline` part | `{llmdv_seed: [absent]}` |

A copyable render placeholder (under an ingress's `capabilities`):

```yaml
render_pins:
  chat_render:
    conversation: "0000000000000000000000000000000000000000000000000000000000000000"
```

Declare it only if the ingress is intended to serve `chat_render`. Add a separate `tools` pin
when the corresponding tool capability is supported.

These are authoring placeholders, not measured expectations. An undeclared pin skips; an
ordinary run does not populate it. Use a known-good build and select only the sentinel cases
you need with `--full-suite -k <test_function>`: `--full-suite` makes them available even when
the profile's default subset omits them, and `-k` narrows the run again.

For a conversation prompt pin, select `test_canonical_conversation_keeps_its_size`; for a
render pin, select `test_canonical_conversation_keeps_its_tokens`. Set the placeholder first
(use 64 literal zeros for a render digest), then read the `canonical_input_tokens` or
`canonical_render_digest` check's `observed` value in `results.jsonl`. A placeholder pin
normally fails; review that observation before replacing it. Tool pins are separate cases
and require the corresponding tool capabilities.

<details>
<summary>Worked example: seed one Chat Completions response contract</summary>

Start with a configured, gitignored local profile and its reachable ingress. This example
uses ingress ID `gateway`; substitute your own ID in both commands and the extraction step.
Add this declaration to that ingress's existing capabilities, preserving its other settings:

```yaml
contract_baseline:
  chat_completions:
    response: {llmdv_seed: [absent]}
```

Validate and collect the selected case's evidence from a known-good deployment:

```sh
P=profiles/examples/my-stack.local.yaml
llmdv profile validate "$P"
llmdv plan --profile "$P" --ingress gateway --surface chat_completions \
  --full-suite -k test_response_keeps_its_contract
llmdv verify --profile "$P" --ingress gateway --surface chat_completions \
  --full-suite -k test_response_keeps_its_contract --evidence all
RUN='<paste the full run directory path printed by verify>'
```

The placeholder allows the contract case to record a healthy response, but establishes no
meaningful drift protection yet. With the same Python environment used to install `llmdv`,
extract a proposed baseline offline. This requires one passing case and one successful JSON
response for that case; it refuses ambiguous or failed evidence. It creates a new snippet
file inside the run directory and refuses to overwrite one:

```sh
python - "$RUN" gateway <<'PY'
import json
import sys
from pathlib import Path

import yaml
from llmdv.assertions.contract import baseline, shape

run = Path(sys.argv[1])
ingress = sys.argv[2]


def rows(name):
    return [json.loads(line) for line in (run / name).read_text().splitlines() if line.strip()]


def matches(row):
    return (
        row.get("case_id") == "evolution.response_contract"
        and row.get("ingress") == ingress
        and row.get("surface") == "chat_completions"
    )


results = [row for row in rows("results.jsonl") if matches(row)]
if len(results) != 1 or results[0].get("outcome") != "pass":
    raise SystemExit("Expected exactly one passing response-contract case; inspect the run.")
exchanges = [
    row for row in rows("exchanges.jsonl")
    if matches(row) and row.get("nodeid") == results[0]["nodeid"]
]
if len(exchanges) != 1:
    raise SystemExit("Expected exactly one exchange; inspect the run.")
exchange = exchanges[0]
status = exchange.get("status")
body = exchange.get("response_body")
if (
    not isinstance(status, int) or not 200 <= status < 300
    or exchange.get("transport_error") or not isinstance(body, dict) or not body
):
    raise SystemExit("Expected a successful JSON object response; inspect the run.")
snippet = {"contract_baseline": {"chat_completions": {"response": baseline(shape(body))}}}
with (run / "response-baseline.yaml").open("x") as output:
    yaml.safe_dump(snippet, output, sort_keys=False)
PY
```

Review `response-baseline.yaml`, including which paths or types may legitimately vary.
**Replace** the placeholder `response` mapping in the local profile with the generated
mapping; do not overwrite the entire profile or leave `llmdv_seed` alongside the real paths.
Keep the run and build provenance locally. Validate the profile and rerun the same selected
case to check the seeded expectation.

Repeat separately for each ingress, surface and contract part. A response-body shape cannot
seed a stream: stream parts need parsed frames and `stream_shape`, followed by `baseline`.
Do not mix evidence from different configurations or seed from an unexplained failure.

</details>

**Render.** The render cases run only on the `*_render` surfaces an ingress declares: `render.stability` and `render.unknown_model` on each, `render.synthetic_prefix` only with `prefix_stability`, and a render pin only under a declared render surface. `contract` checks surfaces both ways, so declare a render surface only where the stack is meant to serve it; otherwise leave it out with a comment saying so, and those cases skip as `undeclared`. The cache keys that depend on rendering (`render_equivalent`, `stripped_system_prefix`, `equivalent_settings`, `canonical_tool_keys`) are in [REFERENCE.md](../profiles/REFERENCE.md#cache_reuse).

## More than one configuration: parts and combinations

When one stack needs several profiles, write each fact once in a **part** and list parts in a
**combination**, the file you run. Worked examples:
[`profiles/llm-d/`](../profiles/llm-d/README.md) and
[`profiles/examples/custom-llm-d/`](../profiles/examples/custom-llm-d/).

- A part holds one kind of fact (what the APIs promise, a layer, an engine, a model configuration). It is not a profile, and it never extends anything: a file reached by two routes is an error.
- Anything inside a `parts/` folder is a building block; every other file is a profile you can run. What only one group of profiles shares goes in that group's own `parts/common.yaml` (`profiles/llm-d/<guide>/parts/common.yaml`), so the group's name appears once, as its folder.
- A combination is `extends: [parts/…, …]`, a `name`, and only what belongs to that one stack: the engine's own names (parser names, the reasoning field), block size, per-layer differences and the change sentinels.
- Name a model part by what a client sees (model, context window, parsers), never by accelerator or cluster. Configurations that give a client the same model, window, parsers and engine share one combination; tell their runs apart with `--identity accelerator=<value> --identity provider=<value>` (and `--identity llm-d-ref=<value>` when applicable), not `--label`. For an llm-d guide, follow llm-d's own workflow inputs: `accelerator` is the guide's `ACCELERATOR_TYPE`, `provider` the `INFRA_PROVIDER` folder the workflow renders (with any connector the guide folds into it, such as `nixl/amd-ci`; `base` when the workflow sets none), and every other variable the guide's workflows vary (connector, variant, topology, scaler) goes into one `--identity flavor=`, llm-d's values joined with `/` in overlay-path order (`flavor=lmcache-connector/cpu`), passed on every workflow of that guide, rather than a new profile or a label.
- Tag every line with where its value came from: `FROM values: <path>` (shipped configuration), `FROM docs: <path>`, `INTENT` ([intended behavior](writing-profiles.md#declare-intended-behaviour)), `DERIVED`, or `MEASURED on <where>`. Nothing unmeasured is tagged measured.
- Mappings merge; ordinary lists replace; `null` clears where allowed. Ingress lists merge by `id`, so `ingresses: []` does not remove inherited ingresses. Inspect unexpected overrides with `profile explain <combination> --field <field>`.
- Validate and plan a combination, never a part: `llmdv profile validate <combination>` and `llmdv plan --profile <combination> -q`.

### Profiles for an llm-d guide

To find which guides and workflows need this after llm-d moves, use
[tracking-llm-d-guide-drift](../.agents/skills/tracking-llm-d-guide-drift/SKILL.md).

Decide which configurations get a profile from the guide itself, and read all of its sources,
checking each against the others:

- the nightly workflows, `.github/workflows/nightly-e2e-<guide>-*.yaml`, and the deploy scripts they call;
- the rendered manifests of the overlay each workflow applies (`kubectl kustomize <path>`, locally, never against a cluster), for the model, context window, parsers, engine build and flags;
- the guide's `guide.yaml`, including any `support:` table;
- the guide's README, including any support or supported-accelerators section.

Write a profile for each configuration a nightly workflow deploys, as the rendered manifests show
it, and none for any other. `guide.yaml` and the README are statements to check against the
workflows and manifests, not the source of truth. Wherever they disagree (a workflow deploying something
the table or README does not list, a table cell no workflow runs, a README naming a different model
or flag than the manifest), profile what the workflow deploys and record the disagreement under
"Where llm-d's own sources disagree" in the guide folder's README (in its "For maintainers"
section), citing paths. Say "guide", the word llm-d's own files use (`guides/`, `guide.yaml`,
`GUIDE_NAME`), and name the folder exactly as llm-d names the guide; do not write "well-lit
path" or any other term for it.

1. List the guide's nightly workflows in llm-d and the deploy scripts they call. For each workflow read its accelerator, engine, provider and gateway class.
2. Render the overlay that workflow applies, locally (`kubectl kustomize <path>`, never against a cluster). A workflow that sets no provider applies the accelerator and engine folder's own kustomization. Read the model, context window, parsers, engine build and flags, for each role (prefill and decode can differ). Check what you read against `guide.yaml` and the README.
3. Group deployment configurations that look the same to a client. Each group is one combination; its header names its workflows, and the directory's README maps every workflow to its profile with the exact `--identity` flags that distinguish configurations sharing a profile, lists what no workflow runs, and records under "Where llm-d's own sources disagree" every disagreement between the guide's sources. Write the README for someone validating a deployment: it opens with "What it tests", "Pick your profile" (model/configuration, overlays, linked profile), "Run it", the generated "What `llmdv verify` selects" and "Not covered", and keeps the workflow table, the identity derivation and the disagreements between llm-d's sources in collapsed `<details>` blocks under "For maintainers". Keep automation provenance in those maintainer sections, and keep each workflow name in backticks: the drift skill's report matches them. [`tiered-prefix-cache/README.md`](../profiles/llm-d/tiered-prefix-cache/README.md) is the model.
4. Give the guide's `parts/common.yaml` a `test_sets.default` of the cases the guide's claims touch, keeping `contract.*` (see [REFERENCE.md](../profiles/REFERENCE.md#test_sets)), and check every glob with `llmdv plan --profile <combination>`. The [coverage rule](#coverage-and-known-failures) still holds for the configuration: assign a full-suite validation run to each configuration, recorded in `profiles/llm-d/README.md` under "Broader validation assignments", and separately cover each distinct engine build.
5. A layer the kinds cannot order (a routing sidecar between the router and the engines, two routers in series, a coordinator in front of the router) is placed with `fronts`; see [Declared layer edges](../profiles/REFERENCE.md#declared-layer-edges). An ingress may front only ingresses at its own position or deeper.

#### How profiles/llm-d is built

**A sum, not a product.** The guide profiles share router declarations, engine parts, model configurations, and a topology: the standalone router (`parts/topology/standalone.yaml`). A gateway topology is added when a workflow runs gateway mode. One file per combination would repeat every fact in dozens of files, so each fact lives in one part and a combination lists the parts:

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

- **No key is set by two parts,** so the order of the list changes nothing. The exceptions, each on purpose: the simulator's model part overrides five `base.yaml` keys, and the simulator, the precise-prefix-cache-routing GPU, AMD and XPU and the fast-model-actuation combinations override the standalone surfaces, and each says so in a comment. Use `profile explain` on the source profile to inspect which file set each key; archived profiles that pin environment-supplied model IDs omit those source comments.
- **`tests/test_example_profiles.py` checks every part** through the combinations that list it.
- **Ingress-keyed facts live in the topology part.** `llmdv` keys surfaces, error statuses and header dispositions by ingress id (`router` in the standalone topology), so ingress-keyed facts such as the SLO headers, which the guides leave without effect, are declared in the topology part, not in a guide's `parts/common.yaml`.
- **Model parts are named by the configuration a client sees** (`qwen3-32b-ctx16k-parsers`), never by accelerator; their headers list the overlays that use them.

**Which source owns which fact:**

| Source | Owns |
| --- | --- |
| the guide's nightly workflows and the deploy scripts they call | which configurations get a profile |
| the guide's `guide.yaml` and README | what the guide says exists and is supported, checked against the workflows and manifests (a disagreement goes under "Where llm-d's own sources disagree"); how a client reaches the stack |
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

For llm-d guide validation, `test_sets.default` selects focused checks for the deployment.
Maintainers also assign broader validation with `--full-suite` to each shared configuration
and distinct engine build (see [Profiles for an llm-d guide](#profiles-for-an-llm-d-guide)).
Users can expand coverage for their own requirements independently of those assignments.
A profile without `test_sets.default` requires an explicit coverage choice.

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
