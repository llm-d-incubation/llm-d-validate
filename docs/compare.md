# Comparing runs

A run says whether the fleet holds now. `llmdv compare` says whether it got worse: it reads runs of
one profile back from the archive `--remote-output` wrote, matches cases, and reports which ones
changed their answer.

```sh
llmdv compare --from s3://bucket/llmdv --profile qwen-router --list                # runs for this profile, oldest first
llmdv compare --from s3://bucket/llmdv --profile qwen-router                       # latest run against the preceding matching run
llmdv compare --from s3://bucket/llmdv --profile qwen-router --baseline <run-id>   # against a specific run, e.g. before a rollout
llmdv compare --from s3://bucket/llmdv --profile qwen-router --window 5            # a change counts only if the last 5 runs agreed
llmdv compare --from file:///var/llmdv-runs --profile qwen-router --no-fail        # a local archive; report but exit 0
```

`--profile` is the profile's `name:`, not its path. Output reads the same in a terminal and in
Slack:

```
*qwen-router* — 1 newly failing
`2026-10-07T21-02-50Z-30a7` → `2026-10-07T21-30-00Z-dr1f`

*Newly failing as of* `2026-10-07T21-30-00Z-dr1f` (1)
  • `contract.declared_surfaces_exist@router`

*Existing passing*: 2
```

## How a baseline is chosen

A run is listed or used only when both `results.jsonl` and `MANIFEST.json` exist. Run ids begin
with a UTC timestamp, so runs order by start time.

**Current run.** Standalone `llmdv compare` judges the latest complete run of the profile under
`--from`. It has no selector for the current run's trigger or flavor. `llmdv job --compare`
judges the job's own finishing run, even when another run was published more recently.

**Identity.** Without an explicit baseline, both commands pick earlier runs whose guide,
accelerator, engine, provider, gateway, model, trigger and flavor match
([run identity](architecture.md#run-identity)). A run without an identity is never picked, but
can be named. `--identity trigger=nightly` marks a scheduled lane (default `manual`);
`--identity flavor=<value>` separates lanes that share a profile, such as `lmcache-connector/cpu`
and `native/cpu` offload. A run without a flavor matches only runs without one. Labels do not affect matching.
An explicit baseline can select legacy runs without identity; when both identities are present,
a configuration mismatch is rejected. A different
llm-d ref, `llmdv` version or profile digest is a note.

**Digest.** Every result carries its `profile_digest`, a hash of the parsed declarations:
comments, `known_failures` and the entire `test_sets` mapping do not move it. Runs under another digest are dropped
from the window and counted. When the two runs differ, the report first says **the profile
changed** and names the declarations that moved. Runs recorded before this rule hashed the file
bytes, so the first comparison after it reports one change with nothing listed; so does a
profile whose older digest included test-set declarations, once.

**Window.** `--window N` (`--compare-window N` on `llmdv job`) is how many earlier runs must
agree; the default, `1`, is the preceding matching run. A case is newly failing only when every run of the
window passed it; a mixed history is flaky. A skip is not a verdict, so a throttled night does
not dilute a window. Each case is read against the newest run of the window that holds it.

## What the sections mean

Cases are matched on `(ingress, case_id, variant)`. Only sections with something in them are
printed, except the *Existing passing* count.

| Section | Meaning |
| --- | --- |
| **Newly failing** | passed before, fails now: the regression |
| **…got no HTTP answer** | a note under *Newly failing*: a timeout or dropped connection (`transient` in `comparison.json`). Still a regression; rerun before triage. A 5xx is not transient |
| **A regression is never known** | a note under *Newly failing*: passed in every run of the window, now fails a check its `known_failures` entry declares. It stays a regression: the entry excuses a new failure or outlived its fix |
| **Existed failing** | failing in both runs |
| **Newly passing** | failed before, passes now |
| **Existing passing** | a count |
| **Flaky across the window** | the window disagrees; not a regression |
| **Stopped running** | had a verdict, skipped now: lost evidence, not a failure |
| **Now running and failing** | skipped before, fails now: new evidence, not new breakage |
| **Started running and passing** | skipped before, passes now; a count |
| **Failing new cases** | absent from the window, failing now; not regressions |
| **Suite changed** | cases appeared or disappeared: a change to `llmdv`, not the fleet |
| **Outside the coverage** | cases a run of the window held and the chosen scope left out of this run, or that this run held and every run of the window left out, so there is nothing to compare them with. Never regressions or new failures; a known failure among them is listed under known failures instead. `coverage.outside` in `comparison.json` |
| **Known failures, fix pending** | failing in the window too and declared in `known_failures` with its issue. Transition `known`, under `known.failing` in `comparison.json` |
| **Fix landed** | labelled a known failure by the newest run of the window that held it, passes now: remove the entry |
| **Fix expected, still failing** | a known failure whose `until` build has answered and still fails; named, kept in its bucket |
| **No longer covered** | a known failure in the newest run of the window that held it, a plain failure now (entry removed, or a different check fails); named with the old issue and both checks, kept in its bucket |

## Coverage changes

A run under [test sets](../profiles/REFERENCE.md#test_sets) holds only its chosen cases; the
rest are excluded, not skipped. Coverage changes which cases run, never what they assert, so
subset and full-suite runs can share a comparison window. `outcome.json` records `coverage`:
mode, resolved sets and patterns, filters, and collected cases excluded by the scope or filters.
When scope changes, the report identifies added/removed patterns and changed modes under
`coverage.changes`. Excluded checks are listed under `coverage.outside`, not suite churn.

Because each case is read against the newest run that holds it, a case a selected run left out
is judged against, labelled from and seen to stop running by the full run before it. A known
failure that passes now is a fix landed, given a `--window` that reaches the run that selected
it.

## Stack changes

`outcome.json` records what each ingress's traffic revealed, read from one table
(`src/llmdv/ingress/signals.py`) plus the profile's `version_headers`, with how many responses
carried each value. When a signal that names a build (the engine's `system_fingerprint`,
`x-litellm-version`, a declared version header) differs, the report first says **the stack
changed** and names it (`litellm: system_fingerprint …g5b2a143 → …g2107750`). Other signals,
such as `server` or the router's per-request `x-llm-d-*` headers, vary freely and stay on the
dashboard. A run that saw two builds behind one ingress straddled a rollout, and the report says
so. Backend-naming headers never reach `outcome.json`. `comparison.json` has this under `stack`.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | no regression, or `--no-fail` |
| `1` | a case that passed now fails |
| `2` | nothing to compare: no usable baseline, a single run, empty or malformed latest results, a profile name or run id that is not one path segment, or a `--from` that can never work |
| `3` | the archive cannot be read: no credentials, the store refuses or cannot be reached, or results are unreadable |

A first run or a mistyped profile name exits `2`, never `0`. All codes:
[cli.md](cli.md#exit-codes).

## In CI

Publish each kind of run under its own prefix, so the latest run is compared with runs of the
same kind. `--window 3` keeps one flaky case from reading as a regression; `--no-fail` reports
without failing the job.

## In a cluster job

```sh
llmdv job --profile P --remote-output s3://bucket/llmdv --compare --compare-window 3
llmdv job --profile P --remote-output s3://bucket/llmdv --compare --compare-baseline <run-id>
```

`llmdv job --compare` judges the finishing run against published runs and writes
`comparison.md` and `comparison.json`, which are published with the run and posted with the Slack
notification. **A regression makes `llmdv job` exit `1` even when every case passed.** A
comparison that cannot be made is advisory: `comparison.md` says
`Comparison unavailable: <reason>` and nothing else changes.

## Feeding an alerting system

Every `llmdv job` run leaves `alerts.json` beside `outcome.json`, one entry per case, ingress and
variant, and prints it as the last line of its log, so a reader needs no access to the results
store. If it cannot be built, the job logs a warning and keeps its exit code.

| Field | Meaning |
| --- | --- |
| `fleet_observed` | true only for verification class `ok` or `fleet` |
| `results[].transition` | `regressed`, `failing`, `recovered`, `passing`, `flaky`, `new`, `known`, or `unbaselined` with no comparison |
| `results[].points_at` | the layer the failure points at, from the layer graph in `outcome.json` (derived from recorded positions when none was recorded); `basis` says why |
| `results[].evidence` | the failure bundle, relative to `publication.location` |
| `comparison` | `made`, and the baseline and window, or why none was made |
| `publication`, `job_exit_code` | as in `outcome.json` |

Alert on `regressed`, not on failure, with `--compare-window 3` or more so a flaky case reads as
`flaky`. Leave `LLMDV_NOTIFY_WEBHOOK` unset when the consumer posts to the channel itself.
