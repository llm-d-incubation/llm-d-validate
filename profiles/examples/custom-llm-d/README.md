# A custom llm-d deployment

Two complete profiles for a deployment that follows none of llm-d's guides: LiteLLM in front of
the llm-d router in front of vLLM, for two models, built from the shared files in
[`parts/`](parts/) with `extends`. [Writing a profile](../../../docs/writing-profiles.md#start-from-the-closest-example)
describes them, and each file's header says how to run it.

## What `llmdv verify` selects

This applies to every runnable profile in this folder: `deepseek-v4.1-flash.yaml`, `glm-5.3.yaml`.

**Default.** A `plan`, `verify` or `job` with no scope flag runs the `default` test set. Each of `deepseek-v4.1-flash.yaml`, `glm-5.3.yaml` defines its own copy in its `test_sets:`, and the copies are identical. There is no other profile-defined set.

**The `default` set's case-ID patterns, by the suite they select from** (7 suites, each confirmed by `llmdv plan`). A pattern ending in `.*` takes every case of its suite; any other names one case, or one variant in brackets. The rest of each suite is not selected:

| Suite | Patterns |
| --- | --- |
| `contract` | `contract.*` |
| `discovery` | `discovery.models_lists_declared_model` |
| `openai_chat` | `openai_chat.basic_non_streaming`, `openai_chat.streaming_sse` |
| `openai_completions` | `openai_completions.basic_non_streaming` |
| `streaming` | `streaming.completions_lifecycle[True]` |
| `openai_responses` | `openai_responses.basic_non_streaming`, `openai_responses.streaming_sse` |
| `anthropic_messages` | `anthropic_messages.basic_non_streaming`, `anthropic_messages.streaming_sse` |

**Named sets:** `default` (above) and the built-in `smoke`, which every profile has ([its cases](../../README.md#what-a-verification-command-selects)). `--all-test-sets` combines the profile-defined sets only, so here it selects exactly `default`; `smoke` is never included in it.

**What each scope selects**, as `llmdv plan` counts it offline. Counts are case instances (one per ingress and variant). *Run* means the profile's declarations, capabilities and fleet policy let the case run; a case can still skip during the run (throttled, `requires_instrumentation`, or a declaration it reads). *Skip* names the skip classes:

| Profile | no flag, or `--test-set default`, or `--all-test-sets` | `--test-set smoke` | `--full-suite` (every case in the library) |
| --- | --- | --- | --- |
| `deepseek-v4.1-flash.yaml` | 26 selected, 26 run | 18 selected, 18 run | 2122 selected, 2087 run, 35 skip (35 undeclared) |
| `glm-5.3.yaml` | 26 selected, 26 run | 18 selected, 18 run | 2122 selected, 2012 run, 110 skip (110 undeclared) |

Other filters (`-k`, `-m`, `--surface`, `--client`, `--ingress`) only narrow the chosen scope, and a filter that leaves no case exits 2. `--identity` flags label the run and never change what it selects.

To inspect the definitions, the cases and every skip reason, offline:

```sh
P=profiles/examples/custom-llm-d/deepseek-v4.1-flash.yaml
llmdv profile show $P --test-sets        # every set and its patterns
llmdv plan --profile $P                  # the default: each case, and why one skips
llmdv plan --profile $P --test-set smoke
llmdv plan --profile $P --full-suite
llmdv plan --profile $P --json           # the same, as JSON
```
