---
name: testing-a-model-deployment
description: Use when asked to test, validate or check a model deployment with `llmdv` and handed a model plus one or more endpoints (a LiteLLM gateway URL and key, an llm-d router or EPP route, or both), or a profile to run against them; when someone says "use this profile against this endpoint", "test model X on staging", or "does this deployment work".
---

# Testing a model deployment

Turn "test model X at these endpoints" into a run whose results can be trusted and compared. The order matters: find and identify what you are calling, point a profile at it, prove the basics by hand, then run the suite.

Never change the deployment while testing it. A fix to a deployment is someone else's rollout unless the user explicitly approves it, and then only on the cluster and objects they named.

## 1. Pin down the inputs

| Input | Ask for it if missing |
|---|---|
| The model | its id **at each endpoint**: a gateway's id is usually not the engine's (`<provider>/…` prefixes, `-staging` suffixes) |
| Each endpoint | the URL, and what it is: a LiteLLM gateway (needs a key) or the router (EPP) route (no key) |
| The environment | prod, staging, test or canary, and whether others use it while you run |
| The profile | a committed one, or "find or make one" |

Then check what the person has ([`.agents/ENVIRONMENT.md`](../../ENVIRONMENT.md)): cluster access, the bucket, and where they keep each gateway's URL and key. Never ask for a key in the conversation; they load it into the shell. A router (EPP) route reached directly needs no key. The snippets below call a loaded gateway's values `GATEWAY_URL` and `GATEWAY_KEY`: `export GATEWAY_URL="$<their url variable>" GATEWAY_KEY="$<their key variable>"`.

## 2. Find the endpoints if you were not given them

Not every model is behind LiteLLM. Look in this order, read-only, and stop at the first that answers:

1. **The LiteLLM gateways** the user has access to. For each, with its URL and key loaded:
   ```sh
   curl -s -m 20 -H "Authorization: Bearer $GATEWAY_KEY" "$GATEWAY_URL/v1/models" \
     | jq -r '[.data[].id | select(test("<model>"; "i"))] | join(" ")' 2>/dev/null || echo "(gateway error)"
   ```
   An empty line means that gateway does not serve it; an error means the gateway is down, not that the model is absent. Ask which gateways (prod, staging, test, a canary) the user can reach.
2. **The deployment manifests**, if the user can point you to them. The router's Route host and the namespace are set there.
3. **The cluster**, if the person has access to the one serving it: `kubectl get route -A | grep -i <model>` (or `oc`), or `kubectl -n <namespace> get route`. If no URL for the router turns up, ask the deployment owner which address to test (step 4); a profile can also declare local setup in `setup.requires` (see the `example-two-ingress.yaml` engine ingress).
4. **A person.** If none of these finds it, ask the user or the deployment owner: "Where is <model> served, and which URL should I test (a gateway, the router route, or both)?" Do not guess a URL from a naming pattern.

Record where each endpoint came from; a URL found in a manifest is a hypothesis until a request to it answers with the right model.

## 3. Identify what each endpoint is (read-only)

Before naming a layer, follow writing-an-infra-profile Rule 2. One request each, keys never echoed:

```sh
curl -s -H "Authorization: Bearer $GATEWAY_KEY" "$GATEWAY_URL/v1/models" | jq -r '.data[].id' | grep -i <model>
curl -s -D - -o /dev/null -H "Authorization: Bearer $GATEWAY_KEY" -H 'content-type: application/json' \
  "$GATEWAY_URL/v1/chat/completions" -d '{"model":"<gateway id>","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}' \
  | grep -i -E '^x-litellm-(version|model-name|model-api-base)'
curl -s -H "Authorization: Bearer $GATEWAY_KEY" "$GATEWAY_URL/v1/model/info" \
  | jq -r '.data[] | select(.model_name=="<gateway id>") | .litellm_params.model'   # the provider and engine-side name
curl -s "$ROUTER_URL/v1/models" | jq -r '.data[].id'   # router: the engine's model id
# The router has no version endpoint of its own: it forwards the path (it restricts none;
# profiles/llm-d/parts/topology/standalone.yaml) and one engine replica answers, only on vLLM.
curl -s "$ROUTER_URL/version"
```

Record: the gateway version, the engine build (`system_fingerprint`), and which backend the gateway forwards to (`x-litellm-model-api-base` or `/v1/model/info`; an internal URL, so describe it, never commit it). An HTML 503 from a gateway is its route being down, not the model: stop and report it.

## 4. Choose the profile

| You have | Profile |
|---|---|
| A committed profile for this model and layer (under `profiles/<system>/`) | use it |
| A deployment of an llm-d guide | the guide's combination under `profiles/llm-d/<guide>/`; its `README.md` maps each deployment configuration to a profile, the URL variable to export (`LLMDV_ROUTER_URL`) and the exact `--identity` flags. It runs the guide's `test_sets.default`; add `--full-suite` for every case |
| GLM-5.3, DeepSeek-V4.1-Flash-1017 or Muse matching the composed examples | choose a combination in [custom-llm-d](../../../profiles/examples/custom-llm-d/README.md), check its declarations, and begin with `--test-set smoke`; parts are not runnable profiles |
| One for the model through another layer | copy it to `<system>/<model>.local.yaml` and change what differs: `kind`, `auth`, `model.public_id`, `health_path`, `rate_limited`, the surfaces |
| A gateway and the router for the same model | a two-ingress `.local.yaml`: the gateway profile plus a `kind: router` ingress (below); `llmdv` then reports each layer as a column and attributes differences |
| Nothing for this model | [writing-an-infra-profile](../writing-an-infra-profile/SKILL.md); start with `examples/example-minimal.yaml` when no fuller profile matches |

Every `.local.yaml` is gitignored and may write `base_url` inline. A committed profile names its URL and key by variable (`base_url_env`, `auth.key_env`) and never stores either. A profile argument is always a file path, not the profile's `name:`.

**A copy changes its `name:`** (e.g. add `-router`), or its runs mix with the committed profile's in the results folders.

**Adding the router to a gateway profile.** The new ingress inherits the top-level `model`, `capabilities` and `known_failures`, so override what differs at the router:
- `kind: router`, `auth: {type: none}`, `rate_limited: false`, `health_path: /health`;
- `model: {public_id: <the id from the router's /v1/models>}` (the engine's served name, not the gateway's);
- `error_status` and `unstructured_errors`: review the router example and [`.agents/GROUND-TRUTH.md`](../../GROUND-TRUTH.md), then declare the intended contract. Adopt an envelope deviation only after triage, with the decision recorded; an observed plain-text response alone is not acceptance;
- `surfaces`: what the router serves (often the render routes too); the contract run checks them;
- `transport` and `setup`: `setup` only for local setup the profile declares (`setup.requires`); none otherwise.

## 5. Point the profile at the endpoints

Each profile chooses its own variable names, per ingress. Inspect its declarations without displaying values loaded from the environment:

```sh
llmdv profile explain <profile> --field ingresses
# Narrow to an ingress's URL variable or auth block when needed; use its actual id:
llmdv profile explain <profile> --field 'ingresses[gateway].base_url_env'
llmdv profile explain <profile> --field 'ingresses[gateway].auth'
```

These commands work before the variables are set and show inherited assignments too. Literal URLs already written in a profile remain visible.

Then export each named variable: a gateway ingress's URL and key from wherever the user keeps them (step 1), a router ingress's URL from step 2.

```sh
export <url variable>="$GATEWAY_URL" <key variable>="$GATEWAY_KEY"   # a gateway ingress
export <url variable>='https://<router route host>'                 # a router ingress: no key
```

For example, `examples/example-minimal.yaml` reads its URL from `LLMDV_BASE_URL` and key from `LLMDV_API_KEY`.

An unset variable fails preflight (exit 3) and names the variable, never a value. A `.local.yaml` with `base_url` written inline needs only the key variable. Then check it offline:

```sh
llmdv profile validate <profile>
llmdv profile explain <profile>           # confirm effective contracts and their sources
llmdv profile show <profile> --matrix      # which surfaces run at which ingress
llmdv plan --profile <profile> -q          # cases that would run and skip, by skip class (policy skips included)
```

Use `fleet_policy: dedicated`: it permits selected cases from the disruptive families (malformed input, token abuse, disconnects, long context), and in practice these have not disturbed the deployments they ran against. Switch to `shared` (which skips them as `excluded_by_policy`) only when the deployment's owner asks for it.

## 6. Prove the basics by hand

A few direct requests at each endpoint catch an unusable deployment before an hour-long run, and give the run a baseline. Send them with the profile's model id for that endpoint (add the `Authorization` header for a gateway):

```sh
URL=<endpoint>; M=<model id there>
TOOL='{"type":"function","function":{"name":"get_weather","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}'
ask() { curl -s "$URL/v1/chat/completions" -H 'content-type: application/json' -d "$1"; }
ask "{\"model\":\"$M\",\"max_tokens\":400,\"messages\":[{\"role\":\"user\",\"content\":\"What is 17 times 23?\"}]}" | jq '{fp:.system_fingerprint, c:.choices[0].message.content, u:.usage}'
ask "{\"model\":\"$M\",\"max_tokens\":300,\"tool_choice\":\"required\",\"tools\":[$TOOL],\"messages\":[{\"role\":\"user\",\"content\":\"Weather in Paris?\"}]}" | jq '.choices[0].message.tool_calls // .'
```

| Check | Expect |
|---|---|
| a plain chat | 200, an answer in `content`, the `system_fingerprint` the profile header records |
| thinking off, with the switch the profile's `reasoning` comment names (`reasoning_effort: "none"`, or `chat_template_kwargs` such as `{"enable_thinking": false}`) | the answer in `content`, no reasoning |
| a forced tool call (above) | 200 and one call. A 500 with `KeyError: 'triggers'` in the vLLM log means its structured-output backend locked to guidance; the deployment needs `--structured-outputs-config '{"backend":"xgrammar"}'` |
| `tool_choice: auto` with the same tool | a call |

When a check fails on one deployment and passes on another with the same image and arguments, suspect state (what the pod served first, a stuck engine) before configuration.

## 7. Run

When the user asks for a smoke check or a named subset, preview and run that scope:

```sh
llmdv profile show <profile> --test-sets
llmdv plan --profile <profile> --test-set smoke
llmdv verify --profile <profile> --test-set smoke
```

`--test-set NAME` replaces the profile's default `test_sets.default`; it cannot be combined with
`--full-suite`. The built-in `smoke` checks discovery, basic generation and streaming. Report its
run/skip counts as partial coverage, and do not follow it with the full suite unless requested.
For custom names and full scope details, see [test sets](../../../docs/cli.md#test-sets-and-coverage).

One live run per gateway at a time; never `-n`. From the repository root:

```sh
llmdv verify --profile <profile> --full-suite -m contract                 # about a minute: the declarations, live
llmdv verify --profile <profile> --evidence all -k "<tests>"  # the cases you care about, if any; add --full-suite for a case outside the default test set
llmdv verify --profile <profile> --full-suite --analyze                    # the full suite, 15-60 min; runs/<timestamp>/
```

Restrict a multi-ingress profile with `--ingress <id>`, or several ids comma-separated (`--ingress litellm,router`); preflight checks only the layers selected. Keep the machine awake for the full run, or run it unattended with `llmdv job` or as a cluster job ([deploy/README.md](../../../deploy/README.md); [`.agents/ENVIRONMENT.md`](../../ENVIRONMENT.md), "Long runs"). Exit `1` is test failures, `3` your environment rather than the fleet, `5` results not delivered, `6` interrupted; see [docs/cli.md](../../../docs/cli.md#exit-codes).

## 8. Compare and report

- **Against an earlier run of the same profile:** running-and-comparing-live-runs.
- **Against the same model elsewhere** (prod vs staging, gateway vs router): `llmdv compare` only compares runs of one profile, so join two `results.jsonl` files on `nodeid` (with the ingress id taken out of the brackets; a multi-ingress run filtered to the matching column), and split the differences into pass → fail, fail → pass and skip changes. When the other deployment already has published runs, use its latest complete one (running-and-comparing-live-runs, "Pull a run back") rather than starting one; note when its `llmdv` version differs from yours. A group of new failures sharing one first-failing check is one cause.
- **Read the failures:** triaging-a-run (causes, tracking issues, layer, repro), then reporting-a-run for the dashboard. A failure shared with other models' profiles (vLLM API layers, fleet-wide flags) is not this deployment's defect.
- **File what is new:** filing-a-fleet-finding.

Hand back: what was tested (endpoints described, never their hosts), the build, the counts against the baseline, what changed and why, and what is left for whom.

## Red flags

| Thought | Reality |
|---|---|
| "The model id is the same everywhere" | Gateways rename models. Read `/v1/models` at each endpoint. |
| "It failed, so the last change broke it" | Check whether it fails on the other deployment too, and whether the pod's state differs before blaming a change. |
| "The run passed the contract, so the deployment works" | The contract checks declarations; forced tool calls, thinking off and streams are separate cases. |
| "I'll put the URL in the committed profile for convenience" | Hostnames go in `*.local.yaml` or environment variables only. |
| "I can fix the deployment quickly myself" | Only with the user's explicit approval, scoped to what they named. |
