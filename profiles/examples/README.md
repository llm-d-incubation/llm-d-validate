# Example profiles

Starting points for your own stack, one per shape of stack. [Writing a profile](../../docs/writing-profiles.md#start-from-the-closest-example)
says which to start from; copy it to a gitignored `profiles/<system>/<model>.local.yaml` before
filling it in. [`custom-llm-d/`](custom-llm-d/README.md) holds complete, measured profiles for three models
on a custom llm-d deployment, built from parts.

## What `llmdv verify` selects

A plain `llmdv verify` runs the `default` test set, defined in each profile's own `test_sets:` (the copies are identical); `example-two-ingress-staging.yaml` inherits it from `example-two-ingress.yaml`. `--test-set smoke` runs the [built-in smoke checks](../README.md#what-a-verification-command-selects), and `--full-suite` runs every case in the library. There are no other sets, so `--all-test-sets` is the same as the default. Filters such as `-k` only narrow the run, and `--identity` never changes it.

<details>
<summary>The default's case-ID patterns, in 7 suites</summary>

`suite.*` takes the whole suite; any other pattern names one case. The rest of each suite is not selected.

| Suite | Patterns |
| --- | --- |
| `contract` | `contract.*` |
| `discovery` | `discovery.models_lists_declared_model` |
| `openai_chat` | `openai_chat.basic_non_streaming`, `openai_chat.streaming_sse` |
| `openai_completions` | `openai_completions.basic_non_streaming` |
| `streaming` | `streaming.completions_lifecycle[True]` |
| `openai_responses` | `openai_responses.basic_non_streaming`, `openai_responses.streaming_sse` |
| `anthropic_messages` | `anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse` |

</details>

<details>
<summary>How many cases each scope selects, per profile</summary>

Counted offline by `llmdv plan`, one per ingress and variant. *Run* cases can still skip during a run (for example `throttled`); *skip* cases are ruled out by the profile's declarations, with their skip class.

| Profile | Default (= `--all-test-sets`) | `smoke` | `--full-suite` |
| --- | --- | --- | --- |
| `example-engine-only.yaml` | 13 selected, 13 run | 9 selected, 9 run | 1076 selected, 538 run, 538 skip: 538 undeclared |
| `example-gateway-only.yaml` | 13 selected, 7 run, 6 skip: 6 undeclared | 9 selected, 3 run, 6 skip: 6 undeclared | 1076 selected, 217 run, 859 skip: 859 undeclared |
| `example-minimal.yaml` | 13 selected, 5 run, 8 skip: 2 unclassified, 6 undeclared | 9 selected, 3 run, 6 skip: 6 undeclared | 1076 selected, 215 run, 861 skip: 2 unclassified, 859 undeclared |
| `example-three-ingress.yaml` | 53 selected, 43 run, 10 skip: 10 undeclared | 27 selected, 17 run, 10 skip: 10 undeclared | 3222 selected, 1502 run, 1720 skip: 1720 undeclared |
| `example-two-ingress-staging.yaml` | 34 selected, 34 run | 18 selected, 18 run | 2150 selected, 1122 run, 1028 skip: 1028 undeclared |
| `example-two-ingress.yaml` | 34 selected, 34 run | 18 selected, 18 run | 2150 selected, 1122 run, 1028 skip: 1028 undeclared |

</details>

To see every case and why one skips: `llmdv plan --profile <profile>` (add `--test-set smoke` or `--full-suite` for the other scopes), and `llmdv profile show <profile> --test-sets` for the definitions.
