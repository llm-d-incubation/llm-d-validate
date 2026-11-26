# Example profiles

Starting points for your own stack, one per shape of stack. [Writing a profile](../../docs/writing-profiles.md#start-from-the-closest-example)
says which to start from; copy it to a gitignored `profiles/<system>/<model>.local.yaml` before
filling it in. [`custom-llm-d/`](custom-llm-d/README.md) holds complete, measured profiles for three models
on a custom llm-d deployment, built from parts.

## What `llmdv verify` selects

The default is defined in each profile's own `test_sets:` (the copies are identical); `example-two-ingress-staging.yaml` inherits it from `example-two-ingress.yaml`.
See [selection rules](../../docs/cli.md#test-sets-and-coverage) for smoke, full-library coverage and filters.

<details>
<summary>The default's case-ID patterns</summary>

`*` selects the whole library; `suite.*` selects a suite. Other patterns select matching cases.

**contract**

- `contract.*`

**discovery**

- `discovery.models_lists_declared_model`

**openai_chat**

- `openai_chat.basic_non_streaming`
- `openai_chat.streaming_sse`

**openai_completions**

- `openai_completions.basic_non_streaming`

**streaming**

- `streaming.completions_lifecycle[True]`

**openai_responses**

- `openai_responses.basic_non_streaming`
- `openai_responses.streaming_sse`

**anthropic_messages**

- `anthropic_messages.basic_non_streaming`
- `anthropic_messages.streaming_sse`


</details>

Counted offline by `llmdv plan`, one case per ingress and variant. Planned runs can still skip at runtime. Expand a profile for its scope counts and separate skip classes.

The default is also `--all-test-sets` for these profiles.

<details>
<summary>example-engine-only.yaml</summary>

[Open the profile](example-engine-only.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 13 | 13 | 0 |
| Smoke | 9 | 9 | 0 |
| Full library | 1076 | 538 | 538 |

Planned skips by class:

- **Full library:** 538 `undeclared`.

</details>

<details>
<summary>example-gateway-only.yaml</summary>

[Open the profile](example-gateway-only.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 13 | 7 | 6 |
| Smoke | 9 | 3 | 6 |
| Full library | 1076 | 217 | 859 |

Planned skips by class:

- **Default:** 6 `undeclared`.
- **Smoke:** 6 `undeclared`.
- **Full library:** 859 `undeclared`.

</details>

<details>
<summary>example-minimal.yaml</summary>

[Open the profile](example-minimal.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 13 | 5 | 8 |
| Smoke | 9 | 3 | 6 |
| Full library | 1076 | 215 | 861 |

Planned skips by class:

- **Default:** 2 `unclassified`, 6 `undeclared`.
- **Smoke:** 6 `undeclared`.
- **Full library:** 2 `unclassified`, 859 `undeclared`.

</details>

<details>
<summary>example-three-ingress.yaml</summary>

[Open the profile](example-three-ingress.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 53 | 43 | 10 |
| Smoke | 27 | 17 | 10 |
| Full library | 3222 | 1502 | 1720 |

Planned skips by class:

- **Default:** 10 `undeclared`.
- **Smoke:** 10 `undeclared`.
- **Full library:** 1720 `undeclared`.

</details>

<details>
<summary>example-two-ingress-staging.yaml</summary>

[Open the profile](example-two-ingress-staging.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 34 | 34 | 0 |
| Smoke | 18 | 18 | 0 |
| Full library | 2150 | 1122 | 1028 |

Planned skips by class:

- **Full library:** 1028 `undeclared`.

</details>

<details>
<summary>example-two-ingress.yaml</summary>

[Open the profile](example-two-ingress.yaml).

| Scope | Selected | Planned run | Planned skip |
| --- | ---: | ---: | ---: |
| Default | 34 | 34 | 0 |
| Smoke | 18 | 18 | 0 |
| Full library | 2150 | 1122 | 1028 |

Planned skips by class:

- **Full library:** 1028 `undeclared`.

</details>

Preview cases and reasons with `llmdv plan --profile <profile>`.
