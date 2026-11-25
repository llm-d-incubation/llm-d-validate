# Writing a profile

A profile tells `llmdv` what your stack is and how a correct one behaves: the model it serves, each
layer you can reach (an *ingress*), the APIs each layer exposes, and what each should answer.
`llmdv` reads it, decides which cases apply, and checks the stack against it. This page walks
through writing one; every field is in [REFERENCE.md](../profiles/REFERENCE.md).

## The steps

1. **Copy the minimal starter or closest example** to a gitignored `profiles/<system>/<model>.local.yaml`
   ([start from the closest example](#start-from-the-closest-example)).
2. **Name the model**: `model.public_id` is the id the ingress advertises on `GET /v1/models`, and
   `model.max_model_len` its context window ([the minimum](#the-minimum)).
3. **Describe each layer you can reach** as an ingress: its `kind` is where it sits (`gateway`,
   `router` or `engine`), and `base_url_env` names the variable that holds its URL.
4. **Name the credential by variable** with `auth: {type: bearer, key_env: <VAR>}`, or
   `auth: {type: none}` for a layer that authenticates nothing. The key never enters the file.
5. **Declare what the stack serves**: each ingress's `surfaces`, its `error_status`, and the
   `capabilities` it supports, as a correct stack behaves
   ([declare intended behaviour](#declare-intended-behaviour)).
6. **Check it offline** with `llmdv profile validate`, inspect overrides with
   `llmdv profile explain`, and preview what runs with `llmdv plan`.
7. **Run the declarations first** with `llmdv verify --full-suite -m contract`, then the full run
   ([validate, plan, verify](#validate-plan-verify)).

## Start from the closest example

The examples are templates with placeholders. Each validates offline; none runs as-is.

| Example | Shape |
| --- | --- |
| [example-minimal.yaml](../profiles/examples/example-minimal.yaml) | One endpoint, model id, URL/key variables and expected error statuses. Start here and add the features your stack promises. |
| [example-engine-only.yaml](../profiles/examples/example-engine-only.yaml) | One vLLM `engine` reached directly, such as `vllm serve`, with nothing in front and no cluster access. |
| [example-gateway-only.yaml](../profiles/examples/example-gateway-only.yaml) | One LiteLLM `gateway` and no cluster access: the shape you get when a URL and a key are all you have. |
| [example-two-ingress.yaml](../profiles/examples/example-two-ingress.yaml) | An llm-d fleet without LiteLLM: a vLLM `engine` and the llm-d `router`. Measured on a live stack; environment and Service names are placeholders. |
| [example-three-ingress.yaml](../profiles/examples/example-three-ingress.yaml) | LiteLLM in front of the router and engine, including header dispositions. Replace deployment facts with yours; choose behavior from the intended API contract. |
| [example-two-ingress-staging.yaml](../profiles/examples/example-two-ingress-staging.yaml) | The two-ingress stack in a second namespace, written with [`extends`](#reuse-a-profile-with-extends) as only its changes. |
| [custom-llm-d/](../profiles/examples/custom-llm-d/) | A deployment that follows none of llm-d's guides (LiteLLM, the llm-d router, vLLM), for two models, built from parts. |

In `custom-llm-d/`, run [glm-5.3.yaml](../profiles/examples/custom-llm-d/glm-5.3.yaml) (a text-only reasoning
model on prefill/decode vLLM) or [deepseek-v4.1-flash.yaml](../profiles/examples/custom-llm-d/deepseek-v4.1-flash.yaml)
(a multimodal model, with the engine's render routes served at the router). `parts/` holds what
both list: what the APIs promise (`base.yaml`), the LiteLLM layer, the router layer and one file
per model; parts never run on their own. Its values were measured on one deployment and say so
beside each line; compare yours and triage differences before adopting those expectations. Put
your URLs, keys and the model ids your gateway serves in a gitignored `*.local.yaml` that extends one of the two files.

For a first profile, copy the minimal starter under a `.local.yaml` name so git ignores it.
Use a fuller example when you need its setup, routing or model-specific declarations:

```sh
mkdir -p profiles/<system>
cp profiles/examples/example-minimal.yaml profiles/<system>/<model>.local.yaml
```

Then replace every `<...>` placeholder. The comments in the example explain each choice.

```
profiles/
  <system>/                 one folder per system you test
    <model>.yaml            a committed profile: URL and key named by variable
    *.local.yaml            your variants, anywhere under profiles/; gitignored
```

One file covers one fleet from one place. An external profile and an in-cluster profile for the
same fleet differ only in `base_url`, `vantage` and `setup`.

To commit a real profile, drop the `.local` suffix, name the URL and the key by variable
(`base_url_env`, `auth.key_env`) so no hostname or key enters the repository, and add a short
guide beside it on how to run it.

## Reuse a profile with `extends`

To change a few declarations of a profile without copying it, write a file that names it and holds
only the changes. This keeps real cluster values out of a committed profile: the committed file
names URLs and keys by variable, and a gitignored file beside it adds the namespace and
port-forwards.

```yaml
# profiles/<system>/<model>.local.yaml
extends: <model>.yaml           # relative to this file
vantage: in-cluster
environment:
  namespace: my-namespace
ingresses:
  - id: engine                  # keyed by id: only setup changes on this ingress
    setup:
      requires:
        - {kind: port_forward, namespace: my-namespace, selector: app=engine, local_port: 18100, remote_port: 8000}
```

To combine independent parts, list them. Each is merged over the ones before it, and the file's
own keys last, so one file per engine and model replaces one file per combination. Parts live in
a `parts/` folder; every other file is a profile you can run:

```yaml
# profiles/<system>/<group>/vllm-<model>.yaml
extends: [../parts/base.yaml, ../parts/engines/vllm.yaml, ../parts/models/<model>.yaml, parts/common.yaml]
```

Mappings merge, lists and other values replace, `null` clears a value, and ingresses are keyed by
`id`, so they merge like mappings; [REFERENCE.md](../profiles/REFERENCE.md#extends) has the rules. A `name` is
not inherited: a file that sets none is named after itself. `llmdv profile show` and
`llmdv profile validate` check the composed result, and a run's `profile.used.yaml` holds it
whole, with a comment naming the file that set each key.

## Inspect before running

These commands are offline and do not need URL or credential variables to be set:

```sh
llmdv profile validate profiles/<system>/<model>.local.yaml
llmdv profile explain profiles/<system>/<model>.local.yaml
llmdv profile explain profiles/<system>/<model>.local.yaml --field 'ingresses[gateway].model'
llmdv profile show profiles/<system>/<model>.local.yaml --yaml > /tmp/profile-expanded.yaml
llmdv plan --profile profiles/<system>/<model>.local.yaml
```

`explain` shows effective values and the source assignments that led to them. The default view
focuses on declared fields; `--field` also shows omitted fields and their library defaults.
Model and capability defaults apply before ingress overrides, even when a later file changes a
global default. For example, an ingress's `model.max_model_len: 8192` still wins over a later
parent's top-level `model.max_model_len: 4096`.

Mappings merge, but ordinary lists replace: changing `surfaces: [chat_completions, messages]`
to `[chat_completions]` removes `messages`; it does not append to the old list. Ingress lists
merge by `id`, so `ingresses: []` does not remove inherited ingresses. `null` clears a value
where the schema permits it; `{}` leaves inherited mapping entries intact.

`show --yaml` exports the composed declarations without `extends`. It preserves URL and key
variable names, makes client executable paths absolute, and keeps the profile's name when saved
elsewhere. It does not fill in every library default or preserve original comments. Keep the
source files for editing. Do not redirect this output over a source file: the shell truncates it
before the command reads it. These authoring views do not display values read from environment
variables; literals already written into a profile remain visible.

## The minimum

A profile needs a model, one ingress, a URL and a key for it, the surfaces it serves, and the
error statuses a correct stack returns:

```yaml
schema: llmdv.profile/1
name: my-model             # optional; defaults to the file name

model:
  public_id: my-org/my-model   # the id GET /v1/models advertises at this ingress
  max_model_len: 32768         # optional; enables the context-overflow case

ingresses:
  - id: gateway                # becomes the column name; `-k gateway` selects it
    kind: gateway              # where it sits: gateway, router or engine (required)
    base_url_env: MY_BASE_URL  # the URL comes from $MY_BASE_URL, never the file
    auth:
      type: bearer
      key_env: MY_TOKEN        # the key comes from $MY_TOKEN
    surfaces: [chat_completions, messages]
    error_status:              # what a correct stack answers; absent means any 4xx
      unknown_model: [404]
      unknown_path: [404]
      wrong_method: [405]
      bad_credentials: [401]
```

Add `test_sets.default` to make plain `plan`, `verify` and `job` commands work. For example:

```yaml
test_sets:
  default:
    - contract.*
    - discovery.models_lists_declared_model
    - openai_chat.basic_non_streaming
    - openai_chat.streaming_sse
```

Without a default, choose `--test-set smoke`, another named set, `--all-test-sets`, or
`--full-suite`; a plain run exits 2. Other declarations are optional. A capability you leave out
makes its selected cases skip as `undeclared`, so
a short profile runs fewer cases rather than failing more. Add declarations as you learn what the
fleet should do: reasoning and tool parsers under `model`, optional features under
`capabilities`, per-layer differences under each ingress.

If other people's traffic or dashboards depend on the fleet, set `fleet_policy: shared`.
Disruptive cases then skip as `excluded_by_policy`. The default, `dedicated`, permits those
cases within the chosen test sets or full suite; it does not expand the scope.

## Choose how much to run

Use `llmdv verify --profile <file> --test-set smoke` for a small check of model discovery,
generation and streaming. Preview it with the same flags on `llmdv plan`.

Keep expected behavior in the profile while choosing coverage separately: `test_sets.default` is the
default subset (required for a plain run); other entries in `test_sets` are named alternatives that run only when requested with
`--test-set NAME`. An explicit name replaces the default. This lets you run a quick check
without weakening capability declarations or copying the whole profile. See
[smoke and custom test sets](cli.md#test-sets-and-coverage) for examples.

## Declarations are verified, not trusted

The `contract` suite checks the declared surfaces and header dispositions against the live fleet,
and surfaces fail in **both** directions:

- Declared present, actually absent: the surface regressed.
- Declared absent, actually present: the profile is stale and that surface is going untested.

A declaration `llmdv` cannot check from outside skips with the missing instrument named, rather
than passing green.

## Declare intended behaviour

A profile states how a **correct** stack behaves, not what this fleet happens to do today. If the
gateway answers an unknown model with a 400 where the API calls for a 404, declare `[404]` and let
the case fail. The failure is a finding for the team to triage as a false positive, intended
behaviour or a defect. Declaring the observed 400 would hide it.

Keep four kinds of information distinct:

- **Deployment facts:** model id, endpoint position, URL variable and configured parsers.
- **Expected behavior:** the APIs and capabilities the deployment is meant to support.
- **Observations:** probe results and run evidence, including failures against that expectation.
- **Accepted deviations:** intentional exceptions with a rationale and an issue or tracking note.

Do not disable a promised capability because its test fails. `known_failures` can annotate a
tracked failure without changing its verdict. Use `unstructured_errors` only for an explicitly
accepted envelope deviation: it reverses the assertion and requires the envelope to be absent.
Optional fields omitted from the starter are coverage gaps to review in `plan`, not evidence of
a working feature.

Probe the fleet while you write, and record what you saw in a YAML comment beside the declaration:

```yaml
    error_status:
      # Probed: a nonexistent model id returns 400 with LiteLLM's own error body.
      unknown_model: [404]
```

Declare every case family the fleet is eligible for, so a run exercises everything it can. The
[`writing-an-infra-profile`](../.agents/skills/writing-an-infra-profile/SKILL.md) skill covers the
procedure: how to probe safely, what to measure for a gateway, how to fill pins and baselines, and
how to describe the layers behind an ingress without guessing.

## Validate, plan, verify

```sh
llmdv profile validate profiles/<system>/<model>.local.yaml           # offline: schema and consistency
llmdv plan --profile profiles/<system>/<model>.local.yaml             # offline: what runs, what skips
llmdv verify --profile profiles/<system>/<model>.local.yaml --full-suite -m contract   # the declarations, live
llmdv verify --profile profiles/<system>/<model>.local.yaml --full-suite # the full run
```

`llmdv profile validate` is offline by design: no cluster, no credentials. That keeps "your
profile is wrong" (exit 2) apart from "your vantage point is wrong" (exit 3) and "the fleet is
broken" (exit 1). `llmdv plan` lists the cases; `llmdv profile show <profile> --matrix` prints
the surface matrix the profile implies, including what will skip.

`--full-suite -m contract` runs only the surface and header checks, in about a minute: the quickest way to find
a stale `surfaces` or `headers` line. Other declarations are judged by the cases that use them.
Once it is clean, run the full suite. The [CLI reference](cli.md) covers exit codes,
narrowing a run and what a run writes.

## Further reading

- [REFERENCE.md](../profiles/REFERENCE.md): every field, its type, default and meaning, and the
  `fleet` rules.
- [profiles/llm-d/README.md](../profiles/llm-d/README.md): the llm-d guide profiles in detail.
- [`writing-an-infra-profile`](../.agents/skills/writing-an-infra-profile/SKILL.md): the agent
  procedure for probing a stack and choosing declarations.
- [Writing tests](writing-tests.md): adding a case or a suite.
