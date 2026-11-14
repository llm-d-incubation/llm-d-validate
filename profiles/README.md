# Profiles

A profile is a YAML file that describes **one fleet, from one vantage point**: the model it serves, the layers you can reach (each one an *ingress*), the API surfaces each layer exposes, and how a correct stack behaves on each. llmdv reads it, decides which cases apply, and checks the fleet against it.

A profile is hand-written and carries no secrets. URLs and keys are named by environment variable, never stored.

This page is the guide to writing one. Every field is in [REFERENCE.md](REFERENCE.md). Rules read from a cluster's configuration, metrics or logs are in [FLEET.md](FLEET.md).

## Layout

Profiles are grouped by the system they test, one folder per system.

```
profiles/
  README.md                 this guide
  REFERENCE.md              every field
  FLEET.md                  configuration, metrics and log rules
  examples/                 templates with placeholders; they do not run as-is
  <system>/                 one folder per system you test
    <model>.yaml            a committed profile: URL and key named by variable
    *.local.yaml            your variants, gitignored
```

| File | What it is |
| --- | --- |
| `examples/example-gateway-only.yaml` | One `litellm` ingress and no cluster access. The shape you get when a key is all you have. |
| `examples/example-two-ingress.yaml` | An llm-d fleet without LiteLLM: a `vllm` engine and an `llmd_router` gateway. Captured from a live stack; the environment and service names are placeholders. |
| `examples/example-three-ingress.yaml` | The full shape with LiteLLM in front of the router and engine, including header dispositions. Derived from deployment manifests and never run. |
| `**/*.local.yaml` | Your own profiles, anywhere under `profiles/`. Gitignored. |

## Start from the closest example

1. Pick the example whose shape matches what you can reach. With only a gateway URL and a key, that is `example-gateway-only.yaml`.
2. Copy it into a folder named for your system, with a `.local.yaml` name so git ignores it:

   ```sh
   mkdir -p profiles/<system>
   cp profiles/examples/example-gateway-only.yaml profiles/<system>/<model>.local.yaml
   ```

3. Replace every `<...>` placeholder. Read the comments in the example as you go: they explain each choice.

One file covers one fleet from one place. An external profile and an in-cluster profile for the same fleet differ only in `base_url`, `vantage` and `setup`.

To commit a real profile, drop the `.local` suffix, name the URL and the key by variable (`base_url_env`, `auth.key_env`) so no hostname or key enters the repository, and add a short guide beside it on how to run it.

## The minimum

A profile needs a model, one ingress, a URL and a key for it, the surfaces it serves, and the error statuses a correct stack returns:

```yaml
schema: llmdv.profile/1
name: my-model             # optional; defaults to the file name

model:
  public_id: my-org/my-model   # the id GET /v1/models advertises at this ingress
  max_model_len: 32768         # optional; enables the context-overflow case

ingresses:
  - id: gateway                # becomes the column name; `-k gateway` selects it
    kind: litellm              # vllm (default), llmd_router or litellm
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

Everything else is optional. A capability you leave out makes its cases skip as `undeclared`, so a short profile runs fewer cases rather than failing more. Add declarations as you learn what the fleet should do: reasoning and tool parsers under `model`, optional features under `capabilities`, per-layer differences under each ingress. [REFERENCE.md](REFERENCE.md) lists them all.

If other people's traffic or dashboards depend on the fleet, set `fleet_policy: shared`. Disruptive cases then skip as `excluded_by_policy`. The default, `dedicated`, runs everything.

## Declarations are verified, not trusted

The surfaces and header dispositions a profile declares are checked against the live fleet by the `contract` suite, and surfaces fail in **both** directions:

- Declared present, actually absent → the surface regressed.
- Declared absent, actually present → the profile is stale and that surface is going untested.

A declaration llmdv cannot check from outside skips with the missing instrument named, rather than passing green.

## Declare intended behaviour

A profile states how a **correct** stack behaves, not what this fleet happens to do today. If the gateway answers an unknown model with a 400 where the API calls for a 404, declare `[404]` and let the case fail. The failure is a finding for the team to triage as a false positive, intended behaviour or a defect. Declaring the observed 400 would hide it.

Probe the fleet while you write, and record what you saw in a YAML comment beside the declaration:

```yaml
    error_status:
      # Probed: a nonexistent model id returns 400 with LiteLLM's own error body.
      unknown_model: [404]
```

Every case family the fleet is eligible for should be declared, so a run exercises everything it can. Probe each behaviour before declaring it, and record what you measured in a comment beside the line, as above.

## Check it, then run it

```sh
llmdv profile validate profiles/<system>/<model>.local.yaml          # offline: schema and consistency
llmdv profile show     profiles/<system>/<model>.local.yaml --matrix # what will run, and what skips
llmdv verify --profile profiles/<system>/<model>.local.yaml -m contract   # the declarations, live
```

`llmdv profile validate` is offline by design: no cluster, no credentials. That keeps "your profile is wrong" (exit 2) apart from "your vantage point is wrong" (exit 3) and "the fleet is broken" (exit 1). See [exit codes](../MANUAL.md#exit-codes).

`-m contract` runs only the surface and header checks, the quickest way to find a stale `surfaces` or `headers` line. Other declarations are judged by the cases that use them. Once it is clean, run the full suite; [selecting what runs](../MANUAL.md#selecting-what-runs) covers narrowing it, and [output](../MANUAL.md#output) covers what a run writes.

## Further reading

- [REFERENCE.md](REFERENCE.md): every field, its type, default and meaning.
- [FLEET.md](FLEET.md): configuration, metrics and log rules for a profile that can read the cluster.
- [Extending llmdv](../ARCHITECTURE.md#extending-llmdv): adding a case or a capability.
