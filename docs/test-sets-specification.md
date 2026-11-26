# Test sets specification

Implementation/design record for the migration to `test_sets`. For current user instructions,
read [Selection and coverage](cli.md#test-sets-and-coverage); field semantics live in
[the profile reference](../profiles/REFERENCE.md#test_sets). The migration and acceptance sections
below describe the implementation work, not additional steps for a new user.

## Core model

A profile describes the deployment and expected behavior. A test set chooses coverage without
changing assertions, capabilities, or policy gates. An omitted flag never implicitly requests
the full library: a plain run requires a deliberately configured `default` set.

```yaml
test_sets:
  default:
    - contract.*
    - discovery.models_lists_declared_model
    - openai_chat.basic_non_streaming
  quick-api:
    - openai_chat.basic_non_streaming
    - openai_chat.streaming_sse
  tools:
    - tools.*
  complete:
    - "*"
```

## Configuration

- A name begins with a lowercase letter and contains lowercase letters, digits, hyphens or
  underscores. Built-in names, initially `smoke`, are reserved; redefining one is exit 2.
- Every set is a non-empty list of case IDs or case-ID patterns. Patterns retain existing
  wildcard and literal `id[variant]` matching semantics. Sets cannot reference other sets.
- `default` is an ordinary profile-defined set with automatic-selection behavior.
- A missing or empty `test_sets` mapping is valid. Such a profile can run a built-in set or the
  full suite explicitly, but a plain run has no default and exits 2.
- Duplicate patterns and overlaps across sets never execute a case twice. Identity includes
  the ingress and variant, so separate legitimate parametrizations still run.
- `"*"` within YAML selects every library verification case. Bare `[*]` is not valid YAML for
  this purpose. If any set contains `"*"`, combining all sets selects the entire library.

## CLI and resolution

The same coverage flags work on `plan`, `verify`, and `job`:

```sh
llmdv verify --profile P
llmdv verify --profile P --test-set smoke
llmdv verify --profile P --test-set quick-api --test-set tools
llmdv verify --profile P --all-test-sets
llmdv verify --profile P --full-suite
```

| Invocation | Scope |
| --- | --- |
| No coverage flag, `default` exists | Use `default` |
| No coverage flag, no `default` | Exit 2 with available choices |
| `--test-set NAME` | Use that exact named set |
| Repeated `--test-set` | Union of the requested sets |
| `--all-test-sets` | Union of every profile-defined set, including `default` |
| `--full-suite` | Every library verification case |

Explicit modes replace the default. Include it explicitly with `--test-set default` when
combining it with other named sets. `--test-set` consumes exactly one argument and is repeatable,
matching `--ingress`, `--client`, and `--surface`; it never consumes a list of positional values.
The three explicit modes are mutually exclusive.

There is no `--all` flag and no CLI name-pattern syntax. `--test-set '*'` and names containing
`*` or `?` exit 2 with guidance to use `--all-test-sets` and `profile show --test-sets`.

`--full-suite` selects functional verification cases shipped in `llmdv.suites`, not developer
unit tests under `tests/`. Additional filters and normal applicability gates still apply.

## Built-in smoke

`smoke` checks model discovery, basic non-streaming generation and streaming on supported Chat
Completions, Completions, Responses and Messages APIs. It reuses existing assertions and gates.
It excludes exhaustive contract, tool, reasoning-specific, quality, load, long-context, cache
and fleet checks. Undeclared surfaces skip normally; preflight still runs.

Built-ins are available explicitly even without any profile-defined sets. They are never
implicitly included by `--all-test-sets`. Profiles cannot override built-ins; create a different
name for a customized subset. There is no built-in default.

## Inheritance

Through `extends`:

- Set names merge.
- Redefining a set replaces its entire list rather than appending.
- A name set to `null` removes the inherited definition.
- `test_sets: null` clears all inherited profile-defined sets.

The resolved default controls a plain run, including when inherited. Removing it does not cause
a fallback to smoke or to the full library.

## Collection, filters and gates

1. Resolve the requested coverage mode and names.
2. Collect the inventory for the requested ingresses.
3. Check the chosen sets' patterns against that inventory.
4. Combine their matching cases, deduplicating them.
5. Apply client, surface, keyword, marker and other pytest filters.
6. Apply normal capability, surface, policy and runtime gates.
7. Execute runnable cases.

Only chosen sets' patterns are matched. A stale pattern in an unused set does not fail a run.
`--all-test-sets` chooses every profile-defined set, so it checks all their patterns. Structural
profile errors, including empty lists and reserved names, are always invalid.

Defaults should cover contract checks. Planning warns when the effective default omits them,
based on resolved case coverage rather than the literal presence of `contract.*`. The warning
is advisory and does not reject deliberately narrow defaults or warn for explicit narrow sets.

## Errors and empty runs

| Condition | Result |
| --- | --- |
| Plain run without a default | Exit 2; list choices |
| Explicitly requested name absent | Exit 2; list available names |
| Any name in repeated flags is absent | Reject the whole request |
| `--test-set default` without default | Exit 2 |
| `--all-test-sets` without profile-defined sets | Exit 2 |
| `--test-set` without an argument | Argument error, exit 2 |
| Conflicting modes or CLI name patterns | Argument error, exit 2 |
| Empty list, malformed definition or reserved name | Invalid profile, exit 2 |
| Chosen pattern matches no collected case | Exit 2 naming the set and pattern |
| Additional filters leave zero cases | Exit 2; never report success |

Scope resolution and collection validation complete before deployment requests. A missing or
invalid choice never falls back to a larger run.

Selected-but-inapplicable cases are skipped with their reasons. They are distinct from cases
excluded by coverage. An all-skipped run reports **NO CHECKS EXECUTED**, rather than a passing
headline; it provides no evidence that the deployment works. Normal exit-code policy for skips
is unchanged.

## Discovery, planning and output

```sh
llmdv profile show P --test-sets
llmdv plan --profile P --test-set quick-api --test-set tools
llmdv plan --profile P --all-test-sets
llmdv plan --profile P --full-suite
```

Discovery lists profile and built-in names, patterns and whether a default exists. Planning
and execution identify the mode, resolved names and sources, exclusions from test sets,
exclusions from other filters, runnable cases and skipped cases with reasons. A subset's result
applies only to its executed checks.

## Results, comparison and reproduction

Plan documents and `outcome.json` record `coverage`:

- `mode`: `default`, `named`, `all-test-sets`, or `full-suite`.
- `sets`: resolved names, sources (`profile` or `built-in`), and case-ID patterns.
- `cases`: deduplicated union of patterns; empty in full-suite mode.
- `filters`: ingress, client, surface, keyword, marker and forwarded pytest arguments.
- `excluded_by_test_sets` and `excluded_by_filters`: counts from collection.
- `excluded`: identities of collected cases omitted by the resolved scope or other filters.

A run refused before planning completes may have only the resolved definition, or no coverage
record if resolution failed. Such a run has no verification evidence.

Comparisons consume coverage instead of the old selection field, distinguish omitted checks
from suite churn, and report scope changes. No compatibility shim reads old selection metadata.

For test-set runs, reproduction freezes the chosen patterns into `profile.replay.yaml` as a
`recorded-run` set and uses that file, retaining other CLI filters. `profile.used.yaml` retains
the source profile snapshot. This prevents changes to a built-in definition from changing the
replay's chosen patterns. Reproduction continues to require the recorded library version.

Test-set definitions are excluded from the deployment contract digest because they change
coverage, not expected behavior.

## Breaking repository migration

- Move existing `selection` lists to `test_sets.default`, preserving their patterns and comments.
- Move the experimental `selections` mapping into `test_sets`.
- Replace `--selection` with repeated `--test-set` flags.
- Replace `profile show --selections` with `profile show --test-sets`.
- Replace old `--all` library-suite calls with `--full-suite`.
- Review plain invocations: preserve deliberate full runs with `--full-suite`, or configure a
  deliberate default. Give all complete shipped examples small defaults, including the minimal
  example. Their plain `plan` invocation must succeed.
- Migrate all profiles, CI and deployment callers, result readers, tests, docs and skills in the
  same change. Old keys and flags fail with replacement hints; no aliases are retained.
- Migrate guide defaults in their shared parts so child profiles inherit unchanged coverage.

## Acceptance checks

- Before/after guide plans have identical case IDs, variants, ingresses, planned outcomes and
  skip classes.
- Every complete shipped example and guide profile plans successfully without coverage flags.
- Plan, verify and job resolve the same scope in all four modes.
- Missing defaults and invalid names/patterns fail before any deployment request.
- Repeated flags preserve following `-k`/`-m` and pytest arguments; overlapping cases run once.
- Reserved names, old spellings, CLI wildcards, conflicting modes and empty selections fail
  clearly. Unchosen stale patterns do not affect a run.
- Inheritance, null removal, YAML wildcard, built-in smoke, coverage recording, comparison and
  frozen replay behavior have regression coverage.
- Lint, formatting and offline tests pass where the environment allows them; socket restrictions
  must be reported separately from verification success.
