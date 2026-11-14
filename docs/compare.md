# Comparing runs

A run says whether the fleet holds now. `llmdv compare` says whether it got worse: it reads runs of
the same profile back from the archive `--remote-output` wrote, matches cases, and reports which
ones changed their answer.

```sh
llmdv compare --from s3://bucket/llmdv --profile qwen-router --list                # runs for this profile, oldest first
llmdv compare --from s3://bucket/llmdv --profile qwen-router                       # the latest run against the one before it
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

## What the sections mean

Cases are matched on `(ingress, case_id, variant)`, so the same case at the gateway and at the
engine are different questions. Only sections with something in them are printed, except the
*Existing passing* count, which always is.

| Section | Meaning |
| --- | --- |
| **Newly failing** | passed before, fails now: the regression |
| **Existed failing** | failing in both runs: known, not new |
| **Newly passing** | failed before, passes now |
| **Existing passing** | a count, not a list |
| **Flaky across the window** | the runs in the window disagree; not a regression |
| **…got no HTTP answer** | a note under *Newly failing*: a timeout or a dropped connection, named in `comparison.json` as `transient`. Still a regression; rerun before triage, since a rollout looks the same. An HTTP 5xx is not transient: the fleet answered |
| **Stopped running** | had a verdict before, skipped now. Not a failure: lost evidence |
| **Now running and failing** | skipped before, fails now. New evidence, not new breakage |
| **Started running and passing** | skipped before, passes now; a count |
| **Failing new cases** | absent from the window, failing now; not regressions |
| **Suite changed** | cases appeared or disappeared: a change to llmdv, not the fleet |
| **Known failures, fix pending** | failing in the window too, and declared in the profile's `known_failures` with the issue that tracks them. Not a regression: the team knows. Named in `comparison.json` under `known.failing`, with the transition `known` |
| **A regression is never known** | a note under *Newly failing*: the case passed in every run of the window and now fails a declared check. It stays a regression; the entry either excuses a failure or outlived its fix |
| **Fix landed** | a case the previous run labelled as a known failure passes now: remove its entry |
| **Fix expected, still failing** | a known failure whose `until` build has answered and which still fails. Kept in its regression bucket, and named |
| **No longer covered** | was a known failure in the previous run and is a plain failure now: the entry was removed, or the case fails a different check. Kept in its bucket, and named with the old issue and both checks |

Every result carries the `profile_digest` it was produced under. When the two runs disagree, the
report says **the profile changed** before anything else and names the declarations that moved,
because a case can move with the contract rather than with the fleet.

A skip is not a verdict, so a throttled night does not dilute a window. Runs made under a
different profile are dropped from the window and the count is stated. The digest hashes the
parsed declarations, not the file: comments and the `known_failures` block do not move it. Runs
recorded before that rule carry a digest of the file bytes, so the first run after it reports
the profile as changed once, with nothing listed under what changed.

Every run records in `outcome.json` what its traffic said about each ingress: the engine's
`system_fingerprint`, `x-litellm-version`, `server`, the router's signature headers, each with
the number of responses that carried it. When the two runs saw different values, the report
says **the stack changed** before anything else and names the change (`litellm:
system_fingerprint …g5b2a143 → …g2107750`), because a case that moved with the build is a
different question from one that moved on the same build. When one run saw more than one
build behind an ingress, it says so: that run straddled a rollout and its verdicts mix two
fleets. Internal service names are never recorded. `comparison.json` carries the same under
`stack`.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | no regression, or `--no-fail` |
| `1` | a case that passed now fails |
| `2` | no usable baseline, a single run, or the latest run has empty or malformed results |

A comparison against nothing is never a clean bill of health, so a first run or a mistyped
profile name exits `2`, never `0`.

## In CI

A CI pipeline gets the most from `llmdv compare` by publishing each kind of run under its own
prefix, so the latest run is always compared with earlier runs of the same kind. A window of three
(`--window 3`) keeps one flaky case from reading as a regression: a case is newly failing only when
all three earlier runs passed it, and a mixed history is reported as flaky. Pass `--no-fail` to
report without failing the job.

## In a cluster job

```sh
llmdv job --profile P --remote-output s3://bucket/llmdv --compare
llmdv job --profile P --remote-output s3://bucket/llmdv --compare --compare-window 3
llmdv job --profile P --remote-output s3://bucket/llmdv --compare --compare-baseline <run-id>
```

`llmdv job --compare` judges the finishing run's local results against published runs. Without
`--compare-baseline`, its baselines are only the most recent earlier runs labelled
`trigger=nightly`; on-demand runs are never baselines. Standalone `llmdv compare` has no such
filter: it compares the latest run with the one before it, whatever its trigger.

The comparison is written to `comparison.md` and `comparison.json` in the run directory,
published with the run, and posted with the Slack notification. **A regression makes `llmdv job`
exit `1` even when every case passed.** A comparison that cannot be made (no baseline, an
unreachable archive) is advisory: `comparison.md` says `Comparison unavailable: <reason>`, and
publication, notification and the exit code are unaffected.

Both `results.jsonl` and `MANIFEST.json` must exist for an archived run to be listed or used as a
baseline. Run ids begin with a UTC timestamp, so ordering is by start time.

## Feeding an alerting system

Every `llmdv job` run leaves `alerts.json` beside `outcome.json`: the verdicts joined with the comparison,
one entry per case, ingress and variant. `llmdv job` prints it as the last line of its log, so a
reader of the finished Job's log needs no access to the results store. The feed is advisory:
when it cannot be built the job logs a warning, keeps its exit code, and prints no feed line.

| Field | Meaning |
| --- | --- |
| `fleet_observed` | true only for verification class `ok` or `fleet`; any other run says nothing about the fleet |
| `results[].transition` | `regressed`, `failing`, `recovered`, `passing`, `flaky`, `new` or `known`; `unbaselined` when no comparison was made |
| `results[].points_at` | the layer the failure points at, read off the declared chain; `basis` says why |
| `results[].evidence` | the failure bundle, relative to `publication.location` |
| `comparison` | `made`, and the baseline and window, or the reason none was made |
| `publication`, `job_exit_code` | as in `outcome.json` |

Alert on `regressed`, not on failure. Run with `--compare --compare-window 3` or more so a flaky
case reads as `flaky` rather than paging, and leave `LLMDV_NOTIFY_WEBHOOK` unset when the consumer
posts to the channel itself.
