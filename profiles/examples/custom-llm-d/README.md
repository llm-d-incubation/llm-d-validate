# A custom llm-d deployment

Two complete profiles for a deployment that follows none of llm-d's guides: LiteLLM in front of
the llm-d router in front of vLLM, for two models, built from the shared files in
[`parts/`](parts/) with `extends`. [Writing a profile](../../../docs/writing-profiles.md#start-from-the-closest-example)
describes them, and each file's header says how to run it.

## What `llmdv verify` selects

A plain `llmdv verify` runs the `default` test set, defined in each profile's own `test_sets:` (the copies are identical). `--test-set smoke` runs the [built-in smoke checks](../../README.md#what-a-verification-command-selects), and `--full-suite` runs every case in the library. There are no other sets, so `--all-test-sets` is the same as the default. Filters such as `-k` only narrow the run, and `--identity` never changes it.

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
| `deepseek-v4.1-flash.yaml` | 26 selected, 26 run | 18 selected, 18 run | 2122 selected, 2087 run, 35 skip: 35 undeclared |
| `glm-5.3.yaml` | 26 selected, 26 run | 18 selected, 18 run | 2122 selected, 2012 run, 110 skip: 110 undeclared |

</details>

To see every case and why one skips: `llmdv plan --profile <profile>` (add `--test-set smoke` or `--full-suite` for the other scopes), and `llmdv profile show <profile> --test-sets` for the definitions.
