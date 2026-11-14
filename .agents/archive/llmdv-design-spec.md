> **Original design, kept for reference.** Kept for its reasoning. The source of truth is
> [docs/architecture.md](../../docs/architecture.md) and the code; the decisions that still hold
> are in [../DECISIONS.md](../DECISIONS.md). Superseded since:
>
> - **§1 scope:** cache reuse is no longer out of scope. `suites/cache/` asserts realised prefix
>   reuse across turns from the reported cached tokens (exact block counts with
>   `cache_reuse.block_size`). Hit *rates* and router-vs-engine agreement still need load and stay
>   out.
> - **§4 profile:** top-level `cluster:` and `namespace:` were replaced by an `environment:` block
>   (`cluster`, `context`, `namespace`, `kubeconfig`, `metrics`).
> - **§5 skip classes:** six, not four: `excluded_by_policy` and `throttled` were added.
> - **§6 exit codes:** now `0`–`6`: `5` results or notification not delivered, `6` interrupted.
> - **§7 layout:** the package has since been flattened to the repository root and the layout has
>   moved on; see [docs/architecture.md](../../docs/architecture.md).
> - **§8 CLI:** seven subcommand groups (`profile`, `verify`, `publish`, `job`, `notify`,
>   `compare`, `analyze`), past the "revisit at three" line; argparse was kept.
> - **§16 deferred:** mostly built: publishing, cross-run comparison (`llmdv compare`, with
>   `--baseline` and `--window`), and the Messages, Responses, Completions and render surfaces.
>   A standalone `llmdv matrix`, fleet sets and `llmdv profile discover` are not.
> - **Cluster access:** the kubectl verb allowlist is `get`, `version`, `config` and `logs`.
> - **§17:** says "four things" but lists six corrections.

# llmdv: a functional test library for deployed model serving infrastructure

Design spec.

## 1. Purpose

`llmdv` proves that each real client can complete the API exchanges it depends on, through every
ingress a fleet exposes, against infrastructure that is already deployed.

When an exchange fails, `llmdv` must make two things cheap: deciding **which layer**
to suspect, and **reproducing the failure** later from a saved artifact.

### In scope

- Functional tests against live endpoints: direct vLLM, the llm-d router/EPP, and LiteLLM.
- A declarative `infra-profile` describing a fleet's endpoints, model, capabilities and
  header contract.
- An `llmdv` CLI driving `pytest` as the execution engine.
- Structured results and a redacted, replayable evidence bundle per failure.

### Explicitly out of scope

- **Deploying or mutating infrastructure.** `llmdv` never applies a manifest, restarts a
  workload, or scales anything. Cluster access via `--kubeconfig` is **read-only and
  structurally enforced**: every `kubectl` invocation passes through a verb allowlist of
  `get`, `version`, `config`, so no code path reaches `apply`, `delete`, `scale` or `exec`.
- **Managing tunnels.** `llmdv` resolves and prints a port-forward command and verifies whether
  it is satisfied. It never starts, stops or supervises one: no subprocess lifecycle, and no
  background forwards to leak.
- **Performance measurement.** Latency and throughput belong to aiperf and other benchmarking
  harnesses. `llmdv` records durations for triage, never as results.
- **Accuracy evaluation.** Benchmarks such as tau2, BFCL, GSM8K, AIME and MMMU belong to
  evaluation harnesses.
- **Cache-hit and routing-accuracy assertions.** Positive prefix-cache-hit rates and
  router-vs-engine agreement require load and belong in system integration tests. `llmdv`
  covers prompt-rendering and prefix *stability*, which is assertable from one request pair.

### Success criteria

1. An operator with a fleet and a profile runs one command and learns whether a named
   client works through each ingress.
2. A failure yields a directory that reproduces it without the original terminal.
3. A surface that silently disappears from a stack fails the suite. It does not pass, and
   it does not quietly skip.
4. Adding an ingress kind, an API surface, or a client is a new module plus a registry
   entry, touching no existing test.

## 2. The infrastructure under test

Facts about the stacks `llmdv` targets, because the design is shaped by them.

### Ingresses are genuinely different code paths

| Layer | Example address | Auth |
| --- | --- | --- |
| vLLM direct | `:8000` prefill, `:8200` decode, or a model Service | none |
| llm-d router (Envoy + EPP) | the EPP Service on port 80, optionally behind an OpenShift Route | none |
| LiteLLM | the LiteLLM Service on port 4000 | virtual keys, possibly resolved by a custom auth hook, plus a master key |

Some fleets have no LiteLLM at all (an EPP-only or model-server-only arm). Ingress presence is per-fleet and must be declared, never assumed.

### The header contract is explicit and asymmetric

A proxy can replace a client-supplied header such as `x-llm-d-inference-objective` or
`x-llm-d-inference-fairness-id` with its own value, the EPP consumes those headers, and vLLM
ignores them. So the same header has three different dispositions depending on where it enters:
rewritten at the proxy, consumed at the EPP, ignored at vLLM. Real clients already send a production header
set (`anthropic-version`, `x-api-key`, `x-request-id`, and both llm-d headers), so the shapes to
test are known rather than hypothetical.

### Surface support varies per fleet, and per container within a fleet

In a disaggregated deployment, the flags `--reasoning-parser`, `--tool-call-parser` and
`--enable-auto-tool-choice` can appear on the **decode** container only, with prefill not
carrying them. Some models default thinking off and need
`chat_template_kwargs: {enable_thinking: true}` to re-enable it.

This has a direct consequence for attribution, recorded in section 12: a case that passes
against one port-forwarded rank and fails through the router may indicate a per-rank
configuration asymmetry rather than a gateway defect.

### HTTP status is not a verdict

A draining engine rejects new work in a way that surfaces as `finish_reason: "abort"` on an
**HTTP 200**. Any assertion layer that checks status alone scores a dropped request as a
success.

### Render endpoints are correctness-critical

`POST /v1/chat/completions/render`, `/v1/completions/render` and `/v1/messages/render`
exist, and router builds can carry native `/v1/messages` render and Anthropic prefix token
alignment. Comparing `token_ids` between two renderers (for example a real vLLM rank and a CPU
shim) and reporting the first divergence index is the mechanism prefix-stability testing should
reuse.

### Vantage point is a known confounder

Over a VPN every virtual user shares one proxy hop, and that hop is what gets measured; runs
from inside the cluster bypass proxies deliberately. A profile therefore records where it is
being driven from, and an unsatisfied or proxied tunnel must be reported as an environment
problem, not a fleet failure.

## 3. Core model

The deliverable question is "can client X complete its exchanges through ingress Y", so
**client is a first-class axis**, not a docstring note.

```
llmdv verify --profile example-three-ingress --client <client>   # can this client work against this fleet?
llmdv verify --profile example-three-ingress --ingress litellm   # is LiteLLM the broken layer?
```

Three axes stay orthogonal. This is the central structural decision.

| Axis | What varies | Abstraction |
| --- | --- | --- |
| **Ingress** | transport: base URL, auth, timeouts, header disposition, expected error statuses | `Ingress` |
| **Surface** | wire shape: paths, request bodies, response and SSE grammar, error envelope | `Surface` |
| **Case** | the property being proven | a pytest function |

A test function receives an `Ingress` and a `Surface` and knows the internals of neither.
A new ingress kind is one module and a registry entry; a new surface likewise; neither
touches the other.

Both axes are real parametrisations. A case marked `surface_generic` runs **once per drivable
surface** the ingress exposes, and builds every request through the surface -- `minimal_body`,
`stream_body`, `invalid_body` -- so it never names a request field itself. A case that genuinely
belongs to one wire shape marks `surface("<name>")` instead, and a case that depends on no
surface at all runs once per ingress. This is what makes "a new surface inherits the existing
cases" true rather than aspirational, and it is the precondition for the Anthropic and Responses
surfaces being a surface implementation rather than a suite rewrite.

**Client** is a selector over cases, not a fourth abstraction: a client declares the
surfaces and capabilities it needs, and `--client` compiles to a marker expression.

## 4. The infra-profile

Hand-written, git-committed YAML, validated offline. No cluster access is required to write
or validate one. One file per (fleet, vantage point): an external profile and an in-cluster
profile for the same fleet differ only in `base_url`, `vantage` and `setup`, so the same
suite runs unchanged from either place.

```yaml
schema: llmdv.profile/1
name: example-three-ingress
cluster: <cluster>
namespace: <namespace>
vantage: external               # external | in-cluster ('laptop' is a deprecated alias)

model:
  public_id: <org>/<model>
  aliases: []
  max_model_len: 131072
  reasoning:
    parser: <reasoning-parser>
    field: reasoning_content    # where reasoning text arrives, non-streaming and streamed
    default_on: true
  tools:
    parser: <tool-call-parser>
    auto_choice: true

ingresses:
  - id: vllm
    kind: vllm
    base_url: http://localhost:8000
    auth: {type: none}
    health_path: /health
    surfaces: [chat_completions, completions, messages, chat_render, messages_render]
    timeouts: {connect: 5, request: 600, stream_idle: 60}
    error_status:
      unknown_model: [400, 404]
    setup:
      proxy: bypass
      requires:
        - kind: port_forward
          namespace: <namespace>
          target: pod/<decode-pod>
          local_port: 8000
          remote_port: 8200
          command: >-
            oc port-forward -n <namespace>
            pod/<decode-pod> 8000:8200

  - id: router
    kind: llmd_router
    base_url: http://localhost:8080
    auth: {type: none}
    health_path: /health
    surfaces: [chat_completions, completions, messages]
    timeouts: {connect: 5, request: 900, stream_idle: 120}
    error_status:
      unknown_model: [400, 404]
    setup:
      proxy: bypass
      requires:
        - kind: port_forward
          namespace: <namespace>
          target: svc/<epp-service>
          local_port: 8080
          remote_port: 80
          command: "oc port-forward -n <namespace> svc/<epp-service> 8080:80"

  - id: litellm
    kind: litellm
    base_url: http://localhost:4000
    auth: {type: bearer, key_env: LLMDV_LITELLM_KEY}
    health_path: /health/liveliness      # /health/readiness 401s under custom_auth
    surfaces: [chat_completions]
    timeouts: {connect: 5, request: 3600, stream_idle: 120}
    error_status:
      unknown_model: [400, 404]
    setup:
      proxy: bypass
      requires:
        - kind: port_forward
          namespace: <namespace>
          target: svc/<litellm-service>
          local_port: 4000
          remote_port: 4000
          command: "oc port-forward -n <namespace> svc/<litellm-service> 4000:4000"

capabilities:
  count_tokens: true
  stateful_responses: false
  structured_output: true
  vllm_options:
    [cache_salt, priority, ignore_eos, min_tokens, truncate_prompt_tokens, return_token_ids]

headers:
  x-llm-d-inference-objective:
    vllm: ignored
    router: consumed
    litellm: rewritten
  x-llm-d-inference-fairness-id:
    vllm: ignored
    router: consumed
    litellm: rewritten
  x-request-id:
    vllm: echoed
    router: echoed
    litellm: echoed
  x-ratelimit-remaining:
    litellm: promised
    router: absent
    vllm: absent

clients:
  - id: <agent-client>
    surfaces: [chat_completions, messages]
    needs: [streaming, tools, reasoning]
  - id: claude-code
    surfaces: [messages]
    needs: [streaming, tools, thinking]
  - id: tau2
    surfaces: [chat_completions]
    needs: [tools]
  - id: <completions-probe>
    surfaces: [completions]
    needs: []
```

### Resolution rules

- `auth.key_env` names an environment variable. Secrets never appear in a profile.
  A missing variable for a declared-and-selected ingress is a preflight failure, not a
  test failure.
- `error_status` values are lists, because the acceptable status for a class legitimately
  differs by layer. An empty or absent entry means "any 4xx".
- `surfaces` is the set a given ingress exposes. A surface absent from the list is
  **undeclared** for that ingress and its cases skip with that reason; it is not expected
  to fail.

### Validation, offline

`llmdv profile validate` checks, with no network:

1. Schema, enum membership (`kind`, `auth.type`, dispositions, `vantage`, `proxy`), types.
2. Internal consistency: every `clients[].surfaces` entry is exposed by at least one
   ingress; every `headers.<name>` key names a declared ingress id; ingress ids are unique.
3. Implementability: every declared surface is implemented by this version of `llmdv`.
   Unimplemented surfaces are reported as **warnings**, so profiles may be written ahead of
   the code, and their cases skip as `not_implemented` at verify time.

A client needing a surface no ingress exposes is dead config, and should fail in a second
rather than after a deploy.

## 5. Declare, then verify

Declaration without verification is a comment. A `contract` suite runs before the
functional suites and makes the profile falsifiable in both directions.

| Profile says | Reality | Verdict |
| --- | --- | --- |
| `responses: false` | 404 | pass |
| `responses: false` | 200 | **fail** — profile is stale, coverage is silently missing |
| `messages: true` | 404 | **fail** — surface regressed |
| `objective: rewritten` at LiteLLM | not visible from outside | **skip**, `requires_instrumentation` |

The fourth row is deliberate and load-bearing. Backend stripping cannot be proven from the
client side, so the honest outcome names what is missing instead of passing green.

### Declared deviations

Two behaviours are declared per ingress rather than asserted uniformly, because layers differ
legitimately and a single hardcoded expectation would fail a healthy fleet. Dropping the case
instead would test nothing, so the profile states the behaviour and the suite verifies the
statement.

| Declaration | Means | Asserted as |
| --- | --- | --- |
| `unstructured_errors: [<class>]` | this layer answers the class in prose, not with the surface's envelope | the envelope must be **absent** |
| `error_status: {<class>: [2xx]}` | this layer does not enforce the class at all | the request must **succeed**, and the consequence is recorded |

Both are falsifiable in **both** directions, which is what distinguishes this from loosening an
assertion: a layer that starts enforcing fails, and so does one that stops deviating, so a stale
declaration cannot hide a fix. The measured instances are an endpoint picker that rejects failed
body extraction as plain text, and an engine that ignores unknown top-level request fields.

### Header disposition vocabulary

| Disposition | Meaning | Verifiable by |
| --- | --- | --- |
| `ignored` | accepted, no effect at this layer | `status` |
| `consumed` | read and acted on, not forwarded | `load_experiment` |
| `forwarded` | passed to the next layer unchanged | `instrumented_backend` |
| `stripped` | removed, not forwarded | `instrumented_backend` |
| `rewritten` | replaced with a value this layer computes | `instrumented_backend` |
| `echoed` | returned in the response | `response_headers` |
| `rejected` | request fails when the header is present | `status` |
| `promised` | response header this layer guarantees | `response_headers` |
| `absent` | response header this layer does not emit | `response_headers` |

Only `status` and `response_headers` are client-observable. The other two mark cases that
skip with a reason naming the missing instrument, mapping onto the skip classes below:
`instrumented_backend` to `requires_instrumentation`, and `load_experiment` to
`requires_load`. This is how header-propagation coverage stays honest before those
instruments exist.

### Skip classes

Four distinguishable reasons, carried into results. A matrix that collapses them is worse
than no matrix, because it reads as coverage.

| Class | Meaning |
| --- | --- |
| `undeclared` | the fleet does not expose this; nothing is wrong |
| `not_implemented` | `llmdv` cannot test this yet; coverage gap in the tool |
| `requires_instrumentation` | needs an instrumented backend to observe |
| `requires_load` | needs a load experiment, i.e. a system test |

## 6. Setup requirements and preflight

`setup.requires` is structured and **non-executing**. `llmdv` never starts, stops or
supervises a tunnel: no subprocess lifecycle, no leaked background port-forwards.

It does four things:

1. **`llmdv profile show --setup`** prints every ingress's command in order, as one
   copy-pasteable block.
2. **Preflight gates the run.** Before collection, `llmdv` TCP-connects to each selected
   ingress and requests `health_path`. On failure it names the unsatisfied requirement and
   its command rather than surfacing a bare `ConnectError`.
3. **Proxy hygiene is checked.** `proxy: bypass` with `HTTP_PROXY`/`HTTPS_PROXY` set and
   `NO_PROXY` not covering the host is a hard stop. An unnoticed proxy hop produces
   plausible, wrong results.
4. **The resolved setup is recorded** in the evidence bundle, because the same `base_url`
   means different things through a tunnel and in-cluster. Without it a bundle is not
   reproducible.

### Exit codes

Separating environment problems from fleet problems is the point.

| Code | Meaning |
| --- | --- |
| 0 | every selected case passed (skips do not fail a run) |
| 1 | test failures — **the fleet** |
| 2 | bad usage, or an invalid profile |
| 3 | preflight unsatisfied — **the vantage point** |
| 4 | internal error in `llmdv` |

A connect-phase failure against `localhost` is almost never a fleet bug, and the suite must
refuse to record it as one.

## 7. Repository layout

The design notes and agent-facing references live in `.agents/`, not in the repository's
documentation tree.

The functional suite ships **inside** the package, because `llmdv verify` must find it when
installed into a pod. `tests/` is `llmdv`'s own unit tests, which need no infrastructure and
run in CI.

```
llm-d-validate/
  .agents/                          # design notes, agent references, skills
  pyproject.toml                    # console_script llmdv; pytest11 entry point
  install.sh                        # venv + editable install; --s3, --dev
  README.md                         # how to run it
  ARCHITECTURE.md                   # layout, module responsibilities, how to extend
  COVERAGE.md                       # every planned test, and its state
  profiles/
    README.md                       # the profile schema, field by field
    example-two-ingress.yaml        # engine + gateway, placeholders for the environment
    example-three-ingress.yaml      # with a proxy in front
    *.local.yaml                    # real values; gitignored
  src/llmdv/
    __init__.py                     # __version__
    cli/
      main.py                       # argparse, parent parser, subcommands
      profile_cmd.py                # validate | show
      verify_cmd.py                 # verify (aliases: run, execute)
      banner.py                     # run banner, phase headings, closing summary
      style.py                      # colour and box drawing, decided per stream
      exit_codes.py                 # 0 ok / 1 fleet / 2 usage / 3 vantage / 4 internal
    profile/
      schema.py                     # dataclasses, Disposition and SkipClass vocabularies
      loader.py                     # YAML -> dataclasses, env resolution, diagnostics
    ingress/
      base.py                       # IngressAdapter + ServedBy + registry
      vllm.py  router.py  litellm.py
    surfaces/
      base.py                       # Surface protocol, Completion/Chunk/Usage, registry
      chat_completions.py           # the only drivable surface so far
    preflight.py                    # reachability, proxy hygiene, secret presence
    kube.py                         # read-only kubectl adapter (verb allowlist)
    http/
      client.py                     # recording httpx client + SdkRecorder
      sse.py                        # SSE line protocol, transcript, size clamp
      redact.py                     # single source of truth for redaction
      repro.py                      # recorded exchange -> runnable curl
    assertions/
      common.py  errors.py          # shared, recording into the evidence journal
    results/
      record.py                     # results.jsonl writer, schema llmdv.results/1
    evidence/
      journal.py                    # per-case record of what was checked
      bundle.py                     # per-case / per-failure evidence directory
      reproduce.py                  # run-level reproduce.sh
    output/
      base.py                       # OutputSink protocol
      local.py                      # LocalDirSink
      remote.py                     # URI parsing + validation; publishing not implemented
    log/                            # not `logging/`: that name shadows the stdlib for readers
      setup.py                      # logger tree, handlers, formatters
      writer.py                     # LoggerWriter stdout/stderr adapter
      filters.py                    # redaction filter
    collect/
      base.py                       # LogCollector protocol
      kubectl.py                    # pod logs, windowed to the run
      noop.py                       # when collection is off or no cluster is reachable
    plugin.py                       # pytest plugin: options, fixtures, markers, reporter
    suites/
      conftest.py
      contract/test_declarations.py
      discovery/test_models.py
      openai_chat/test_basic.py  openai_chat/test_streaming.py
      unhappy/test_error_paths.py   # error paths: every surface must fail correctly
  tests/                            # llmdv's own unit tests
    fakes.py                        # local fake fleet
    fixtures/*.sse                  # streams captured from a live fleet
    test_profile_loader.py  test_surfaces.py  test_assertions.py  test_sse.py
    test_redact.py  test_logging_streams.py  test_logger_writer.py
    test_preflight.py  test_repro_and_results.py  test_kube_and_remote.py
    test_evidence_journal.py  test_cli_style.py  test_collect_and_reproduce.py
    test_suites_against_fake.py
```
Registering the pytest plugin through a `pytest11` entry point means raw
`pytest --llmdv-profile=profiles/x.yaml -m contract` behaves identically. `llmdv verify` is an
ergonomic wrapper, never a gatekeeper.

Python ≥ 3.10.

## 8. CLI

```
llmdv profile validate PROFILE            # offline: schema, consistency, implementability
llmdv profile show PROFILE [--setup] [--matrix]
llmdv verify --profile PROFILE
             [--ingress ID ...] [--client ID ...] [--surface NAME ...]
             [-k EXPR] [-m EXPR]
             [--local-output DIR] [--remote-output URI]
             [--raw-pytest] [-- PYTEST_ARGS...]

llmdv job    --profile PROFILE             # one unattended run: verify, publish, notify
             [--remote-output URI] [--fallback-output URI] [--budget SECONDS]
             [... anything verify takes, passed through]
llmdv publish RUN_DIR --remote-output URI  # publish a finished run, including a crashed one
llmdv notify  RUN_DIR                      # send the outcome summary
```

- `verify` has hidden aliases `run` and `execute` for muscle memory.
- `job` exists because an unattended run has to outlive its own verification: it runs `verify` as
  a child process, so a verification killed by the OOM killer or by the run budget is still
  published and still notified. It owns `--profile`, `--local-output` and `--remote-output`, and
  refuses to pass them through, so verify and the fallback publish cannot disagree about where
  the run went.
- `--ingress`, `--client` and `--surface` compile to marker expressions; `-k` and `-m` pass
  through unchanged. Unknown trailing arguments are forwarded to pytest.
- Repeating a selector is a union (`--ingress vllm --ingress router` means either), and
  different selectors intersect (`--client <client> --ingress litellm` means that client's cases
  at LiteLLM only). A `-m` or `-k` given alongside them intersects too, so an explicit
  expression can narrow a selection but never widen it.
- `--profile` has no short form: `-p` is pytest's plugin flag, and a collision there would
  be silent.
- `profile show --matrix` prints the case matrix a profile implies, including which cells
  will skip and why. This is the discoverability surface and the precursor to `llmdv matrix`.

Global flags live on an `add_help=False` parent parser passed via `parents=[...]` to every
subparser, so they are accepted before or after the subcommand.

### Why argparse, not Typer

1. Typer's presentation layer conflicts with section 9: Click's `echo` and Typer's help and
   error rendering write to stdout and stderr directly, so satisfying the logging contract
   means disabling the parts one would adopt Typer for.
2. `llmdv verify` must forward unknown arguments to pytest. `parse_known_args()` does this
   plainly; Click needs `ignore_unknown_options` plus `allow_extra_args`, and a pytest-bound
   `-p` or `-k` reaching Typer's parser is the same collision class noted above.
3. The surface is one level of nesting and about a dozen flags. The boilerplate saving is
   marginal.
4. It adds no dependency that is not already load-bearing, keeping the tool simple and
   transparent.

Revisit if the CLI exceeds three subcommand groups or shell completion is requested. The
CLI layer stays deliberately dumb — parse, build a config object, call a function — so the
swap would never reach the library.

## 9. Logging

**No `print` anywhere.** Enforced by ruff rule `T20` rather than by discipline.

### Two streams, one logging system

A CLI emits two different things, and collapsing them breaks one.

| Logger | Sink | Format | Level control |
| --- | --- | --- | --- |
| `llmdv` and children | stderr | timestamped, levelled | `--log-level` |
| `llmdv.output` | stdout | `%(message)s`, `propagate=False` | always enabled |

`llmdv.output` carries primary output — the resolved profile, the matrix, the run summary —
and is never filtered by `--log-level`, so `llmdv profile show | yq` keeps working. A thin
`emit()` helper makes those call sites greppable. `--log-level` governs diagnostics, which
is what it should mean.

### Third-party and embedded output

- **Libraries**: `httpx`, `openai`, `anthropic` and `urllib3` all use `logging`, so the
  root configuration captures them by propagation, each with its own level. httpx at DEBUG
  logs every request: valuable at `-vv`, intolerable by default.
  `logging.captureWarnings(True)` folds in the `warnings` module.
- **pytest**: `llmdv verify` embeds it via `pytest.main()`, wrapped in
  `redirect_stdout(LoggerWriter("llmdv.pytest", INFO))` and
  `redirect_stderr(LoggerWriter("llmdv.pytest", WARNING))`. `LoggerWriter` buffers partial
  writes until a newline, implements `flush`, and reports `isatty() -> False`, which makes
  pytest and any Rich-based plugin drop colour and cursor control on their own.
  Disabling the terminal plugin with `-p no:terminal` is rejected: third-party plugins look
  up `terminalreporter` and break. pytest runs `-q --no-header` so native chatter stays thin.
- **Output from inside a test** needs no new machinery. pytest already captures per-test
  stdout and stderr onto the report object; the reporter routes it into that case's
  evidence bundle rather than interleaving it into the console.

A reporter plugin consuming `pytest_runtest_logreport` is the source of `results.jsonl` and the
human run summary. The redirect is a safety net, not the primary path.

### Redaction belongs at the logging layer

httpx at DEBUG logs request headers. Without a filter, `--log-level DEBUG` writes
a tenant key and a master key into `llmdv.log`, which `--remote-output` would then copy to a
PVC or bucket.

So a `logging.Filter` is installed on **every** handler, scrubbing `Authorization`,
`x-api-key`, and the resolved value of every `key_env` the profile names.
`http/redact.py` is the single source of truth, used by the filter, the evidence writer and
the curl generator alike. Redaction at the logging layer, or it does not hold.

### Flags

```
--log-level {DEBUG,INFO,WARNING,ERROR}    default INFO
-v / -vv / -q                             shortcuts for DEBUG tiers and WARNING
--log-format {text,json}                  json lines for pod and CI consumption
--log-file PATH                           additional sink
```

`<local-output>/llmdv.log` always receives full DEBUG regardless of console level. The console
stays readable; the bundle keeps everything.

## 10. Output sinks

```
--local-output DIR      default ./runs/<utc-timestamp>/
--remote-output URI     s3:// | file://
--fallback-output URI   `llmdv job` only: file:///path, used only when --remote-output is
                        unavailable. A mounted PVC is the intended case.
```

`pvc://` is deliberately not a scheme. A mounted volume is a path, which `file://` already
covers, and a scheme would have implied llmdv knows how to attach one. The PVC is declared in the
deployment and reached as a path.

Two rules, built in from the start:

**Local is always written, even when remote is requested.** Remote is a publish step over a
completed local bundle, never a stream target. In a pod this keeps the suite working when
upload credentials are wrong, and the reproducible-asset guarantee never depends on a
network hop.

**An unsupported `--remote-output` fails during preflight.** The URI is parsed and its scheme
checked before the first test runs, so an unsupported destination costs two seconds rather than
a forty-minute suite. A stub that silently no-ops and reports success is worse than no flag,
because the loss is discovered only when the results are needed.

**Publication never masks the verification.** A failed upload after passing tests exits
`NOT_DELIVERED`; a test failure always takes precedence over a delivery failure, and both are
reported. `MANIFEST.json` is published last, so a run is complete in storage if and only if the
manifest is there -- it means "publication complete", not "tests passed".

## 11. Results and evidence

### results.jsonl

One object per case, schema-versioned so runs from different dates, fleets and `llmdv`
versions can be diffed rather than eyeballed.

```json
{
  "schema": "llmdv.results/1",
  "run_id": "<utc-timestamp>-9f3a",
  "ts": "<utc-timestamp>",
  "profile": "example-three-ingress",
  "profile_digest": "sha256:...",
  "llmdv_version": "0.1.0",
  "nodeid": "discovery/test_models.py::test_unknown_model_rejected_without_fallback[litellm]",
  "case_id": "discovery.unknown_model_no_fallback",
  "clients": ["<agent-client>", "claude-code", "tau2"],
  "ingress": "litellm",
  "ingress_kind": "litellm",
  "surface": "chat_completions",
  "outcome": "fail",
  "skip_class": null,
  "reason": "expected 400|404, got 200 with choices present",
  "duration_s": 1.231,
  "http_status": 200,
  "served_by": {
    "x_request_id": "llmdv-9f3a-0007",
    "backend_pod": null,
    "model_echo": "some/model"
  },
  "variant": null,
  "checks": [
    {"name": "unknown_model_status", "observed": 200, "expected": "one of [404]", "ok": false}
  ],
  "evidence": "failures/litellm__unknown_model_no_fallback/"
}
```

`served_by` is populated from day one even though nothing reads it until `llmdv matrix`
exists. Section 12 explains why it is not optional.

`reason` carries pytest's `reprcrash.message` rather than a head-truncated `longrepr`. The
`longrepr` leads with fixture values and the failing function's source and ends with the actual
message, so truncating from the front keeps the context and discards the finding. The full
traceback is still written to `assertion.txt` in the evidence bundle.

`skip_class` is recovered from a reason beginning `[<class>] ...` when a case skips in its own
body rather than through the gate fixture, so the documented prefix is sufficient and no test has
to remember a second registration call.

### Evidence bundle

```
runs/<utc-timestamp>-9f3a/
  profile.resolved.yaml          # redacted, including resolved setup and vantage
  env.json                       # llmdv, python, httpx, openai, anthropic versions; OS
  results.jsonl
  summary.md                     # per-ingress outcome table + attribution reading
  llmdv.log                      # full DEBUG, redacted
  failures/<ingress>__<case>/
    request.json                 # method, url, headers (redacted), body
    response.json                # status, headers, body
    transcript.sse               # streaming cases only, raw lines
    captured_output.txt          # pytest-captured stdout/stderr for the case
    meta.json                    # timings, x-request-id, served_by, setup_hint
    repro.sh                     # runnable curl reproducing the exchange
```

`repro.sh` reads its credential from the same `key_env` variable the profile names, so the
script is shareable without carrying a secret.

## 11a. Evidence for every verdict, not only failures

The first implementation wrote evidence only for failures, which left "22 passed" as an
unfalsifiable claim: a passing case was indistinguishable from one that asserted nothing.

Three additions close that:

**A check journal.** The assertion helpers record what they inspected and what they observed,
on success as well as on failure, into a per-case journal (`llmdv/evidence/journal.py`). Because
the helpers do the recording, a new case inherits evidence without writing any evidence code.
Checks land in `results.jsonl` under `checks`, in `llmdv.log` at DEBUG, and in the bundle's
`verdict.txt` and `checks.json`.

A check is recorded **before** its assertion fires, so a failure shows the observed value that
caused it rather than stopping just short. Where a case fails but every recorded check passed,
`verdict.txt` states that the deciding assertion recorded nothing -- a bundle whose evidence
silently contradicts its own verdict is worse than one with no evidence, because it reads as a
spurious failure.

**`exchanges.jsonl`.** Every HTTP request and response of every case, redacted, keyed by case
and ingress. This is the raw audit trail and is always written.

**`--evidence all`.** Writes a per-case bundle rather than failures only. The bundle directory
name includes the non-ingress parametrisation, because without it the six header-disposition
cases shared two names and overwrote one another -- a 22-case run wrote 18 bundles.

One consequence worth stating: the pinned SDKs construct their own transport, so SDK-backed
cases are handed an httpx client with event hooks (`SdkRecorder`). Without it, four of
twenty-two cases carried a verdict and no exchange. Timing there comes from a request hook,
since `response.elapsed` is unpopulated inside a response hook and recording `0.000s` in
evidence someone relies on is worse than recording nothing.

## 11b. Pod logs and run-level reproduction

`LogCollector` was listed as deferred in §16; it is implemented. `--pod-logs
{off,failures,all}` (default `failures`) collects each layer's own account of the run window
through the read-only kubectl adapter.

Whether that is possible is a preflight verdict (`LogAccess`: available, not_declared,
no_namespace, no_kubectl, denied, unknown), not something discovered afterwards, and the
probe reads `pods/log` rather than treating `get pods` as a proxy for it -- separate RBAC
verbs, so a Role granting `list` on pods need not grant reading their logs. When nothing is
collected the reason rides on `Collection.reason` into `env.json`, the banner and
`summary.md`, and no `logs/` directory is created: an empty one is indistinguishable from a
run whose layers had nothing to say. `logs` was added to the verb allowlist -- reading a
container's stdout changes nothing, and the allowlist exists to keep `apply`, `delete`, `scale`
and `exec` unreachable, not to keep llmdv from reading.

Three decisions worth recording:

**Collected once per run, not per failure.** Five failures against one decode pod would
otherwise fetch five near-identical copies of the same log. One windowed collection, referenced
from the summary, is cheaper and easier to read.

**Windowed with `--since-time`.** A vLLM pod up for days would otherwise bury the handful of
lines belonging to this run.

**Sources default to the port-forward target.** A profile already names the pod or Service its
traffic goes to, which is where its logs come from, so the common case needs no new
configuration. `logs:` overrides per ingress when a layer's pods differ from its ingress.

Logs pass through the same redaction as the rest of the evidence, because the run directory is
what `--remote-output` publishes.

**Correlation is by timestamp, not request id.** Measured live: the llm-d EPP mints its
own `x-request-id` rather than propagating the client's, which is independently why that
profile declares the header `ignored` at the router. `summary.md` states this so nobody wastes
time grepping for an llmdv request id in an EPP log.

### reproduce.sh

A case bundle's `repro.sh` replays one exchange; `reproduce.sh` at the run root replays the
verification. It reads `profile.used.yaml` beside it rather than the profile in git -- a
profile can change after a run, and a verdict only means something against the declarations it
was made with -- records the llmdv version and profile digest, reprints the resolved port-forward
commands while saying plainly that pod names go stale, reads credentials from the environment,
and writes to a fresh directory so a rerun cannot overwrite the evidence it reproduces.

## 12. Attribution

`summary.md` carries the per-ingress outcome table and, where the run can support one, a
ladder read off the profile.

**The ladder is generated, not fixed.** Its rows come from the layers this profile declares,
ordered by `kind` -- gateway in front of router in front of engine -- and narrowed to the ones
that actually reported a result. One row says the innermost layer failed, and one row per
adjacent pair says the inner layer passed where the outer failed. A two-layer profile gets two
rows naming its two layers; a profile with no router never mentions one. For a three-layer
stack (gateway, router, engine) that reads:

| pattern | suggests |
| --- | --- |
| engine passes, router fails | router parsing, mutation, routing, auth, or timeout |
| router passes, gateway fails | gateway parsing, mutation, routing, auth, or timeout |
| engine fails | backend, model template, or fixture |

**A run that cannot establish an order offers no ladder at all.** The ladder's rows are an
ordering claim -- which layer sits in front of which -- and only the profile supplies that.
So a single-layer run, a run summarised without a profile, and a run whose profile declares
fewer than two of the layers that reported all print a short section saying attribution is not
available, which layers did report, and why no order over them is known. They name those
layers: the outcome table, the per-layer tally and the delta table above all label those same
columns. What is withheld is the ordering, never the names.

**The ladder is labelled a hypothesis, never a proof**, and the reason is concrete. It
assumes the layers are in series and otherwise identical. They are not. Direct vLLM
reaches one rank; the router load-balances across many with P/D disaggregation and prefix
scoring; a gateway such as LiteLLM adds its own retry, cooldown and timeout policy, and may
remap reasoning deltas.

Concretely: because the reasoning and tool-call parsers can sit on the decode container only,
"direct passed, router failed" can mean the gateway is broken, or that the router routed to a
rank configured differently from the one that was port-forwarded. Same symptom, different fix.

That is why `served_by` is recorded on every result: `x-request-id` always, and backend pod
identity wherever a layer exposes it. Without it, attribution produces confident wrong
answers.

## 12a. Comparison across runs

Section 12 attributes a failure to a **layer** within one run. This attributes a change to
**time**: the same profile, two runs, which cases answered differently. Nothing in a single run
can do it, because a run has nothing earlier to compare itself to.

`llmdv compare --from <s3://|file://> --profile <name>` reads two runs out of the archive that
`--remote-output` wrote. Cases are keyed `(ingress, case_id, variant)` — per layer, because a
stack can regress at the gateway while the engine is untouched.

**A verdict is `pass`, `fail` or `error`. Everything else is an absence of evidence, not
evidence of health**, and the four transitions that follow from that are the whole design:

| Transition | Reading |
| --- | --- |
| `pass → fail` | the regression |
| `fail → pass` | a recovery, reported on purpose — silence after a fix is how people stop trusting the report |
| `verdict → skip` | **lost evidence, not a failure.** The case stopped running, so the fleet may be fine or may be broken and the run no longer says which. Reporting it as a failure trains people to ignore the alert; hiding it lets coverage drain away. The skip class is named, because `throttled` and `excluded_by_policy` are different problems. |
| `skip → fail` | **new evidence, not new breakage.** The fleet did not change; the suite started looking. It must not hide inside a neutral "started running" count, which is where the first implementation put it. |

A case that appeared or vanished is the **suite** changing, never the fleet.

**Zero cases is not a pass.** A run that recorded no results — an OOM kill, a crash, a selector
that matched nothing — compares against a healthy run as every case vanishing, which renders as
"unchanged, nothing failing" with a clean exit. That is a green verdict over an empty set, the
same shape `banner.finish` already refuses, so comparison refuses it too. One run, or no runs,
likewise exits non-zero rather than reporting clean.

**Declaration drift is reported before the findings.** Every result carries its
`profile_digest`. When the two runs disagree, the report says the contract moved, so a case may
have changed because a declaration was tightened rather than because the fleet did. The absence
of that warning is itself the signal.

Exit codes follow §6: `1` when something regressed, `2` when there was nothing to compare.
`llmdv job --compare` runs it **after** publishing — the archive must contain the run that just
finished — writes `comparison.md` into the run directory so the analysis is published with the
run, and lets `llmdv notify` post it. A regression makes the job exit `1` even when every case
passed, because a run can be green against its own declarations and still be worse than the last
one.

**A verdict is only evidence against the contract it was made under.** The baseline defaults to
the most recent run, because "what changed since last time" is the question. `--window N` judges
against the last N instead: a change is a regression only when every run agreed, and a case they
disagree about is reported as flaky with its pattern rather than paged on. A skip is not a
verdict and does not dilute that agreement, and runs carrying a different `profile_digest` are
dropped from the window entirely — "every run agreed" means nothing if half of them answered
different declarations. Two facts are kept apart there: a window shortened by dropped runs is
not the same as a baseline whose contract differs, and only the second turns findings into
questions.

**Drift is named, not just detected.** When the baseline's profile differs, the report diffs the
two `profile.used.yaml` files and lists the declarations that moved, comments ignored. One
tightened line routinely accounts for an entire delta; without it a reader diffs two profiles by
hand.

**Profile names and run ids become path segments**, and a profile name comes from a field
somebody writes in YAML. They are validated before use: on a shared bucket a name such as
`../<other-prefix>` would otherwise read runs that belong to someone else, and publishing the
comparison would write into them.

Deferred: per-case history as a first-class output, so a trend across many runs does not have to
be assembled from one self-contained comparison at a time. That is the `llmdv matrix` half of the
compatibility matrix, and it is what a running average would be built on.

## 13. The first two cases

### Fixtures

`conftest.py` provides session-scoped `profile`, and `ingress` parametrized over the
selected ingresses with the ingress id as the parametrization id — so `-k litellm` collapses
the matrix to one column for free, and `-k "not litellm"` serves a fleet without LiteLLM.
`client` is a recording httpx client bound to an ingress, with that ingress's timeouts, auth
and base URL already applied.

### A. Model discovery — `suites/discovery/test_models.py`

| Case | Assertion |
| --- | --- |
| `test_models_lists_declared_model` | `GET /v1/models` returns 200, parses as JSON, and `data[].id` contains `model.public_id` and every declared alias |
| `test_models_parses_with_pinned_openai_sdk` | `client.models.list()` returns typed objects through the pinned SDK |
| `test_unknown_model_rejected_without_fallback` | `POST /v1/chat/completions` with `llmdv-nonexistent-<uuid4>` returns a status in that ingress's `error_status.unknown_model`, matches the surface's error envelope, and the body carries **no `choices`** |

The third case is the half of model discovery that actually bites: silent fallback to a
different model is a correctness failure that a status-only check cannot see.

### B. Basic non-streaming chat — `suites/openai_chat/test_basic.py`

| Case | Assertion |
| --- | --- |
| `test_chat_basic` | 200; `object == "chat.completion"`; `choices[0].message.role == "assistant"`; content or the declared reasoning field is non-empty; `usage` present with `prompt + completion == total`; `finish_reason in {"stop","length"}` and explicitly **not** `"abort"` |
| `test_chat_basic_parses_with_pinned_openai_sdk` | the same exchange through the pinned SDK yields a typed `ChatCompletion` |

Two traps designed around up front, both following from how these stacks are deployed:

**`finish_reason: "abort"` on HTTP 200.** This lives in `assertions/common.py`, applied by
every surface, not in one test. It is the draining-engine lesson from §2 generalised.

**"content is non-empty" is the wrong assertion for a reasoning model.** With a reasoning parser
enabled, a request can spend its whole budget reasoning and return empty `content`, and a
gateway that drops reasoning deltas makes such a response look like an error. So the assertion
is "content **or** `model.reasoning.field` is non-empty", and `max_tokens` is set from the
profile's reasoning declaration rather than hardcoded.

Both files run the full ingress matrix, so the attribution machinery and the three-column
summary are exercised from the first pass.

## 14. Dependencies

`pytest`, `pyyaml`, `httpx`, `openai`, `anthropic`. Every one load-bearing.

**Raw httpx is the primary instrument; the SDKs are witnesses.** Most of the eventual list
is untestable through an SDK because the SDK normalises away the thing under test:
malformed JSON, wrong field types, wrong HTTP methods, unknown paths, header manipulation,
mid-stream disconnects. The SDKs appear only where SDK compatibility is itself the
requirement.

Both SDKs are pinned with `==`, and the pinned version must be the one the client under
test actually uses — gateway and SDK versions can differ in how they handle reasoning deltas,
so a floating range would make results meaningless. Installed versions are recorded in
`env.json` in every bundle.

## 15. Testing llmdv itself

`tests/` runs in CI with no infrastructure: profile loading and validation
diagnostics, SSE parsing against recorded transcripts, redaction (including the logging
filter, asserting a DEBUG httpx-shaped record leaks nothing), curl generation round-trip,
preflight decisions against a local socket, logger wiring (asserting `llmdv.output` reaches
stdout while `llmdv` diagnostics reach stderr), and `results.jsonl` schema conformance.

The suites themselves are exercised against a local fake serving the three ingress shapes, so
suite logic is testable without a cluster.

By convention, every test module opens with a docstring stating what silently breaks when the
test is absent.

## 16. Deliberately deferred

Named here so they are visible as planned, not forgotten.

| Item | Note |
| --- | --- |
| `llmdv matrix` | folds one or more `results.jsonl` into a terminal, Markdown or self-contained HTML matrix |
| `--baseline results.jsonl` | diffs two matrices; where this starts catching regressions rather than describing the present |
| Fleet sets | one client suite across several profiles, for the cross-fleet matrix |
| `RemoteSink.publish` | URI parsing and validation ship now; publishing does not |
| `llmdv profile discover` | generating a profile from a live namespace |
| Remaining surfaces | `messages`, `responses`, `completions`, render; and the remaining planned cases |

## 17. Corrections from the first implementation

Four things in the sections above were wrong when first written, and were corrected against a
live fleet. They are recorded because each was a plausible assumption that a code reading would
not have caught.

**An empty `{}` body cannot probe whether a route exists.** The llm-d router gateway validates a
request body *before* it resolves the route, so `/v1/chat/completions/render` answers 400 to `{}`
and 404 to a shape-valid body. A probe reading "not 404 means present" therefore reported three
render surfaces as served when they are absent, on a layer that does not serve them. The contract
suite now probes with a minimally valid body (`surfaces.base.probe_request`), which gives a
consistent signal at every layer. This is a general hazard: any discriminator based on a status
code assumes the layer got far enough to produce a meaningful one.

**`x-request-id` is not echoed.** The example profile in §4 declared `echoed` at all three
ingresses. Neither the vLLM build tested nor the router gateway returns it; only
`x-went-into-resp-headers` and `server` come back. This is exactly the stale-declaration case §5
exists to catch, and it was caught by writing a profile against a fleet rather than against the
manifests.

**`argparse.REMAINDER` cannot be used for the pytest passthrough.** §8 argued that argparse
handles passthrough plainly, which is true only of `parse_known_args`. With `REMAINDER`, a flag
after the first selector is swallowed into the remainder: `--client bogus --local-output X`
silently routed `--local-output` into the pytest argv, so an llmdv usage error surfaced as a
pytest one. The CLI uses `parse_known_args` instead.

**An unsupported option is ignored, not rejected.** The error-class vocabulary assumed an
undefined request field would be refused. It is not: the engine returns 200 and generates
normally. The assertion was written, and shipped red, on a probe that reported 400 — the probe had
inlined its JSON body in the shell, the quoting mangled it, and the 400 was the server failing to
parse the body rather than rejecting the field. Two corrections came out of it. `error_status` now
accepts a success status so the real behaviour can be declared and asserted, and probe bodies go
in a file sent with `-d @`. The wider lesson is that a probe is an instrument: when an ad-hoc curl
and a recorded test exchange disagree, the recorded exchange wins, because it captured the exact
bytes sent.

**The surface axis was documented before it existed.** Sections 3 and 7 described cases running
once per *(ingress, surface)* pair, and said the existing cases would run unchanged against a new
surface. Only the ingress was actually parametrised, and the unhappy-path cases built
chat-shaped bodies by hand -- so against another surface they would have crashed, passed for the
wrong reason, or failed and blamed the fleet. The axis is now real, via a `surface_generic`
marker and `Surface.invalid_body`. Worth recording as a documentation failure rather than a code
one: the design was right, the prose asserted it was built, and nothing in review catches a doc
that describes an intention in the present tense.

**The same fact was declared twice in the profile.** Whether `/v1/responses` is served appeared
both per ingress under `surfaces` and once for the whole profile as `capabilities.responses`. A
profile-wide boolean cannot describe a fleet whose ingresses differ -- with the engine serving it
and a proxy not, either value fails one of them -- and when the two agreed, one regression was
reported twice. The per-ingress list is the single declaration now. The remaining capabilities
describe features *within* a surface rather than routes, which is why they stay.

A fifth item is a consequence of packaging rather than a mistake: an editable install leaves a
discoverable `llmdv.egg-info` on the path, so pytest auto-loads the plugin from its `pytest11`
entry point and a second explicit `-p llmdv.plugin` aborts the session with "Plugin already
registered under a different name". Both the CLI and the test helper check before loading it.
