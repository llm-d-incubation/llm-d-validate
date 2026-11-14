# Design decisions

The decisions that shape `llmdv`, and why each was made. They outlive any one layout or CLI, so
read this before proposing to change one. The original design, with its full argument, is
archived in [archive/llmdv-design-spec.md](archive/llmdv-design-spec.md); where the two disagree,
the code and [../docs/architecture.md](../docs/architecture.md) win.

## What the infrastructure is like

These facts about model serving stacks shaped everything below. They are general to the
engine, router and proxy layering, not to one fleet.

**Each ingress is a different code path.** An engine reached directly, a router with an
endpoint picker in front of many replicas, and a proxy with its own authentication are three
pieces of software, not three addresses for one. They validate, rewrite, authenticate and
time out differently. Some fleets have no proxy at all. So ingress presence is declared per
fleet, never assumed, and every case runs once per declared ingress.

**The header contract is asymmetric.** A proxy can strip a client-supplied header and
re-inject its own value, a router can act on it, and an engine can ignore it. The same header
has a different disposition depending on where it enters. That is why dispositions are
declared per ingress, and why some of them cannot be seen from the client.

**Surface support varies per fleet, and per container within one.** Reasoning and tool-call
parsers are commonly set on decode containers only. Two ingresses of one fleet can therefore
legitimately disagree, and a case that passes at one port-forwarded replica and fails through
the router may be a per-replica configuration difference, not a router defect.

**HTTP status is not a verdict.** A draining engine rejects work with `finish_reason: "abort"`
on an HTTP 200. Any assertion that checks status alone scores a dropped request as a success,
so the shared assertions check the finish reason on every surface.

**The vantage point is a confounder.** A VPN or HTTP proxy can put every request through one
hop, which then becomes what you measure. A profile records where it is driven from, and a
proxied or dead tunnel is an environment problem, never a fleet failure.

## Three orthogonal axes

A case receives an **ingress** (transport: URL, auth, timeouts, expected statuses) and a
**surface** (wire shape: paths, bodies, stream grammar, error envelope) and knows the internals
of neither. A **client** is a selector over cases, not a fourth abstraction.

*Why:* the deliverable question is "can client X complete its exchanges through ingress Y".
Keeping the axes independent is what makes a new surface inherit the existing cases, a new
ingress kind cost one module, and a difference between layers mean something. The moment a
case reaches into a layer, running it at another layer stops being a comparison.

## Declare, then verify

A profile declares what a fleet is and what it supports, and the `contract` suite checks every
declaration against the live fleet in **both** directions: declared present but absent fails
(the surface regressed), and declared absent but present fails too (the profile is stale and
the surface is going untested).

*Why:* a declaration nobody verifies is a comment. One-directional checking lets coverage drain
silently: a surface that appears after the profile was written is never tested, and nobody is
told.

The same rule makes deviations honest. A layer that answers an error in prose
(`unstructured_errors`) or does not enforce a class (a success status in `error_status`) is
declared per ingress and still asserted, inverted. Because the declaration fails when the layer
starts enforcing or stops deviating, a stale one cannot hide a fix. A deviation that would not
fail when it changed is a loosened assertion, not a declaration. Deviations are declared only
after the failure has been triaged; until then the profile states the correct behaviour (see
[skills/writing-an-infra-profile](skills/writing-an-infra-profile/SKILL.md)).

## A skip names what is missing

A declaration llmdv cannot check from the client side skips with the missing instrument named,
rather than passing. There are six skip classes (`SkipClass` in `profile/schema.py`), and they
are never collapsed: `undeclared` is a fleet gap, `not_implemented` a tool gap,
`requires_instrumentation` and `requires_load` a missing instrument, `excluded_by_policy` a
choice not to run on a shared fleet, `throttled` the tool's own quota.

*Why:* a matrix that merges them reads as coverage when it is not. A green cell for a header the
client cannot observe is a false claim; a skip that says "needs an instrumented backend" is a
true one and a to-do.

## Never mutate a cluster

`llmdv` tests deployed infrastructure; it does not deploy. Every cluster call goes through
`kube.py`, whose verb allowlist (`get`, `version`, `config`, `logs`) leaves no path to `apply`,
`delete`, `scale` or `exec`. It never starts, stops or supervises a port-forward either: it
prints the command and checks whether it is satisfied.

*Why:* the tool is pointed at shared fleets that other people run. A test tool that can change
what it measures cannot be trusted with a kubeconfig, and the guarantee is only worth having if
it is structural rather than a convention. Reading pod logs was added because reading a
container's output changes nothing; the allowlist exists to keep writes unreachable, not to
keep llmdv from reading. Managing tunnels was left out because a supervised background process
is one more thing to leak or die silently.

## Exit codes separate blame

`0` ok, `1` fleet failures, `2` usage or invalid profile, `3` preflight (the vantage point), `4`
an llmdv defect, `5` passed but not delivered, `6` interrupted. Preflight runs before collection
and checks every selected ingress, the credentials and the output destination.

*Why:* a connection refused against `localhost` is almost always a dead port-forward, not a
fleet bug. Reporting it as a test failure teaches people to distrust the suite. With separate
codes, CI can tell "the fleet is broken" from "the runner is broken" without parsing logs.
`5` never masks a verification failure: the verification code always wins.

## argparse, not Typer

The CLI uses the standard library's argparse, with `parse_known_args` (never
`argparse.REMAINDER`) to forward unknown arguments to pytest.

*Why:* Click and Typer write help and errors straight to stdout and stderr, which fights the
single-logger contract below; adopting them means disabling the parts one would adopt them for.
`llmdv verify` must pass unknown arguments to pytest, which `parse_known_args` does plainly.
`REMAINDER` swallows later llmdv flags into pytest's argv, so an llmdv usage error surfaces as a
pytest one. The CLI layer stays deliberately thin (parse, build a config, call a function), so a
swap would not reach the library.

## One logging system, redacted at the handler

No `print` (ruff `T20`). Diagnostics go to stderr under `--log-level`; primary output goes to
stdout through a logger the level never suppresses. A redaction filter sits on every handler.

*Why:* `llmdv profile show | yq` must keep working whatever the log level. And httpx logs request
headers at DEBUG, so without a filter on the handlers a debug run writes keys into `llmdv.log`,
which is then published with the run. Redaction in the evidence writer alone does not hold.

## Evidence for every verdict

Assertions record what they observed through the check journal **before** asserting, on passes
as well as failures. Every HTTP exchange of every case is written to `exchanges.jsonl`.
`--evidence all` writes a bundle for every case.

*Why:* evidence for failures only leaves a pass unfalsifiable: a pass is indistinguishable from a
case that checked nothing. Recording before asserting means a failure shows the value
that caused it. A failure whose recorded checks all passed is flagged, because a bundle that
contradicts its own verdict reads as a spurious failure.

## Local first, publish after

The run directory is always written locally. Publishing to `s3://` or `file://` is a separate
step over the finished directory, an unsupported destination fails in preflight, and
`MANIFEST.json` is published last, so a run is complete in storage exactly when the manifest is
there.

*Why:* a run must survive wrong upload credentials, and reproducibility must never depend on a
network hop. A destination discovered to be broken after an hour-long suite is an hour lost.
A manifest written last lets a reader tell a finished upload from a partial one.

The one exception is oversized evidence with explicit `--artifact-sink`: transcripts and pod
logs may be uploaded synchronously while the run is still collecting, but the capped local copy
is always retained and the reference records the whole copy's hash and upload status. This keeps
the default and scheduled runs unchanged while making an opt-in whole artifact available.

## Truncation is a result field

`partial_evidence` says the verdict's local evidence is capped and no whole copy was stored. It
describes what a reader can verify, not how the verdict was computed: assertions still ran
against the full in-memory transcript.

## Test requests are never retried

Deliveries (uploads, notifications) are retried within bounds; requests under test are not.

*Why:* a retried inference request hides the very failure the case exists to catch. A retried
upload only preserves evidence.

## Attribution is a hypothesis

Within one run, the summary reads a ladder off the profile: which layer failed where the layer
inside it passed. The rows come from the declared layers, ordered by `kind` (proxy, router,
engine), and a run that cannot establish an order offers none. Every result records
`served_by`.

*Why:* the ladder assumes the layers are in series and otherwise identical, and they are not.
Direct access reaches one replica; a router load-balances across many with different
configurations. "Engine passed, router failed" can mean the router is broken or that it routed
to a replica configured differently from the one port-forwarded. Calling it a hypothesis, and
recording which endpoint served each request, is what keeps it from producing confident wrong
answers.

## Comparing runs: four transitions

Human reference: [../docs/compare.md](../docs/compare.md).

`llmdv compare` keys cases by `(ingress, case_id, variant)` and reads only `pass`, `fail` and
`error` as verdicts. The transitions that follow:

| Transition | Reading |
|---|---|
| `pass → fail` | the regression |
| `fail → pass` | a recovery, reported on purpose |
| `verdict → skip` | lost evidence, not a failure; the skip class is named |
| `skip → fail` | new evidence, not new breakage |

Zero cases is never a pass, a changed `profile_digest` is reported before the findings with the
declarations that moved, and only scheduled (`trigger=nightly`) runs are baselines for
`llmdv job --compare`.

*Why:* a skip is an absence of evidence. Reporting it as a failure trains people to ignore the
alert, and hiding it lets coverage drain away. Silence after a fix is how people stop trusting
the report. A run that recorded nothing (an OOM kill, an empty selection) would otherwise
compare as "nothing failing". And one tightened declaration routinely explains a whole delta,
so the reader needs to see it before the cases.

## Raw httpx first; SDKs are witnesses

Most cases send raw HTTP through a recording httpx client. The OpenAI and Anthropic SDKs are
pinned exactly and used only where SDK compatibility is itself the requirement.

*Why:* an SDK normalises away much of what is under test: malformed JSON, wrong types, wrong
methods, header manipulation, mid-stream disconnects. Pinning matters because a proxy or SDK
version change can alter response shapes; a floating range would make results
meaningless across runs.
