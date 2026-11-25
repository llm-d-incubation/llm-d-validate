# Example profiles

Starting points for your own stack, one per shape of stack. [Writing a profile](../../docs/writing-profiles.md#start-from-the-closest-example)
says which to start from; copy it to a gitignored `profiles/<system>/<model>.local.yaml` before
filling it in. [`custom-llm-d/`](custom-llm-d/README.md) is a complete profile for a custom llm-d
deployment, built from parts.

## What `llmdv verify` selects

This applies to every runnable profile in this folder: `example-engine-only.yaml`, `example-gateway-only.yaml`, `example-minimal.yaml`, `example-three-ingress.yaml`, `example-two-ingress-staging.yaml`, `example-two-ingress.yaml`.

**Default.** A `plan`, `verify` or `job` with no scope flag runs the `default` test set. Each of `example-engine-only.yaml`, `example-gateway-only.yaml`, `example-minimal.yaml`, `example-three-ingress.yaml`, `example-two-ingress.yaml` defines its own copy in its `test_sets:`, and the copies are identical; `example-two-ingress-staging.yaml` inherits it from `example-two-ingress.yaml` through `extends`. There is no other profile-defined set.

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

**Named sets:** `default` (above) and the built-in `smoke`, which every profile has ([its cases](../README.md#what-a-verification-command-selects)). `--all-test-sets` combines the profile-defined sets only, so here it selects exactly `default`; `smoke` is never included in it.

**What each scope selects**, as `llmdv plan` counts it offline. Counts are case instances (one per ingress and variant). *Run* means the profile's declarations, capabilities and fleet policy let the case run; a case can still skip during the run (throttled, `requires_instrumentation`, or a declaration it reads). *Skip* names the skip classes:

| Profile | no flag, or `--test-set default`, or `--all-test-sets` | `--test-set smoke` | `--full-suite` (every case in the library) |
| --- | --- | --- | --- |
| `example-engine-only.yaml` | 13 selected, 13 run | 9 selected, 9 run | 1066 selected, 537 run, 529 skip (529 undeclared) |
| `example-gateway-only.yaml` | 13 selected, 7 run, 6 skip (6 undeclared) | 9 selected, 3 run, 6 skip (6 undeclared) | 1066 selected, 217 run, 849 skip (849 undeclared) |
| `example-minimal.yaml` | 13 selected, 5 run, 8 skip (2 unclassified, 6 undeclared) | 9 selected, 3 run, 6 skip (6 undeclared) | 1066 selected, 215 run, 851 skip (2 unclassified, 849 undeclared) |
| `example-three-ingress.yaml` | 53 selected, 43 run, 10 skip (10 undeclared) | 27 selected, 17 run, 10 skip (10 undeclared) | 3192 selected, 1486 run, 1706 skip (1706 undeclared) |
| `example-two-ingress-staging.yaml` | 34 selected, 34 run | 18 selected, 18 run | 2130 selected, 1120 run, 1010 skip (1010 undeclared) |
| `example-two-ingress.yaml` | 34 selected, 34 run | 18 selected, 18 run | 2130 selected, 1120 run, 1010 skip (1010 undeclared) |

Other filters (`-k`, `-m`, `--surface`, `--client`, `--ingress`) only narrow the chosen scope, and a filter that leaves no case exits 2. `--identity` flags label the run and never change what it selects.

To inspect the definitions, the cases and every skip reason, offline:

```sh
P=profiles/examples/example-engine-only.yaml
llmdv profile show $P --test-sets        # every set and its patterns
llmdv plan --profile $P                  # the default: each case, and why one skips
llmdv plan --profile $P --test-set smoke
llmdv plan --profile $P --full-suite
llmdv plan --profile $P --json           # the same, as JSON
```
