---
name: extending-llmdv
description: Use when adding or changing a surface, ingress kind, test case, client, assertion or profile capability in the llmdv functional test library, when choosing between surface_generic and surface markers, or when a new case has only ever passed.
---

# Extending llmdv

Each addition is one module plus one registry line. [docs/architecture.md](../../../docs/architecture.md) has the layout; this is the procedure and the checks. Paths below are under `src/llmdv/` unless they start with `tests/`.

## Add a surface

1. Implement `WireSurface` in `surfaces/<name>.py` for the native request and error contract. A generation API also implements `Surface`: `parse_completion`, `assemble`, `invalid_body` and streaming. Render APIs declare the `render` feature and skip generation parsing.
2. Declare only features in `SURFACE_FEATURES` (`surfaces/base.py`), each with the methods it gates: `structured_output` → `structured_output_body`, `image_input` → `image_body` and `image_url_body`, `tools` → `tools_body`, `cache_usage` → `parse_cache_usage` and `stream_cache_usage`. An unknown feature is refused at registration.
3. Implement `invalid_body` for every class in `REQUEST_ERROR_CLASSES`, and `TOOL_ERROR_CLASSES` when the features include `tools`, in the surface's own shape. That is what lets surface-generic cases run unchanged. Put headers the wire needs in `extra_headers`; every request sends them.
4. Register it: one import in `surfaces/__init__.py`.
5. Add its path to `SURFACE_PATHS` and a shape-valid body to `probe_request` in `surfaces/base.py`, so the contract suite can probe for it.
6. Add cases under `suites/<name>/`.
7. Teach the fake fleet (`tests/fakes.py`, with wire helpers in `tests/fake_openai.py` and `tests/fake_anthropic.py`) to serve it, validating its own shape, with at least one injectable defect. Run the surface-generic suites against it.

## Add an ingress kind

1. Add a member to `IngressKind` in `profile/schema.py`, and its depth in `_KIND_DEPTH` beside it (outermost first; the report orders layers by it).
2. Subclass `IngressAdapter` in `ingress/<kind>.py`, overriding only what differs, usually `backend_headers` or `served_by`. Declare `version_headers` (and `version_header_prefixes`) for the headers whose value names the layer's build and is safe to publish; `backend_headers` may name a host or Service and never reach `outcome.json`.
3. Register it: one import in `ingress/__init__.py`.

## Add a case

```python
pytestmark = pytest.mark.surface_generic


@pytest.mark.case_id("group.what_it_proves")
def test_something(client, surface, model, ingress):
    exchange = client.post(surface.path, json_body=surface.minimal_body(model.public_id))
    journal.record("http_status", exchange.status, "200", ok=exchange.status == 200)
    assert exchange.status == 200
```

| The case | Mark it | It runs |
|---|---|---|
| holds on any generation surface | `surface_generic`, and take `surface` | once per drivable generation surface |
| needs a surface feature | `surface_generic("tools")` | once per drivable surface with it |
| holds on any render surface | `surface_generic("render")` | once per drivable render surface |
| asserts one surface's own grammar | `surface("chat_completions")` | on that surface only |
| does not touch a surface (e.g. `GET /v1/models`) | no `surface` argument | once per ingress |

- The closest marker decides. Both markers on one function, or a feature outside `SURFACE_FEATURES`, fails collection as an llmdv defect. A known feature no drivable surface has yet gives one `skip:not_implemented` row.
- Prefer `surface_generic`: a new surface then inherits the case.
- A generic case builds every request through the surface (`minimal_body`, `stream_body`, `invalid_body`, `structured_output_body`) and never writes a wire field name. A body in one surface's shape is garbled on the others: refused for the wrong reason so it passes, or ignored so it fails and blames the fleet.
- `case_id`'s prefix becomes the phase in the output. Reuse an existing group unless you mean to create one.
- Gate on a profile capability with `@pytest.mark.needs("<capability>")`, or on one value of a closed vocabulary with `needs("<field>:<value>")`. An unknown name is a usage error at collection, never a silent skip.
- Mark a case that can disturb a shared fleet `@pytest.mark.disruptive("<reason>")`: it then skips as `excluded_by_policy` under `fleet_policy: shared`.
- A new `suites/<dir>` becomes a pytest marker automatically; its place in the output order is `PHASE_ORDER` in `plugin.py`.
- Every skip is classified, never bare. There are six classes (`SkipClass` in `profile/schema.py`): `undeclared`, `not_implemented`, `requires_instrumentation`, `requires_load`, `excluded_by_policy`, `throttled`.

## Add a client

A selector is one entry under `clients:` in a profile. A pinned Claude Code probe also declares `binaries` with exact versions and paths. A different client needs its own invocation adapter and native-wire evidence checks; a successful process exit alone is never a generation verdict.

## Add an assertion

Put it in `assertions/`, not in a test. Record what it observed through `journal.record(..., ok=...)` **before** asserting, so a failure shows the value that caused it. For a deciding failure call `assertions.fail(name, observed, expected)`: it redacts the values, records the failed check and raises. Passing checks of large payloads record counts and lengths; the raw exchanges keep the detail.

Return `Completion` from `parse_completion` and `assemble` so shared assertions apply. A parallel assertion stack is how streaming and non-streaming drift apart.

## Add a profile capability

- A cross-surface switch takes `false`, `true`, or an explicit surface list (`structured_output: [chat_completions, responses]`). Follow that rule for new ones.
- An API-specific value list (`chat_options`, `messages_thinking`, `responses_reasoning_efforts`) selects native option values, not surfaces. Each vocabulary lives once in `profile/schema.py` and is shared by parsing and case generation.
- Structured configuration such as `cache_reuse` keeps its `surfaces` selector alongside its own settings.
- Every capability may be overridden per ingress.
- Parse it in `profile/loader.py`, add its row to [profiles/REFERENCE.md](../../../profiles/REFERENCE.md) (`tests/test_profile_reference.py` fails on an undocumented field), and add a closed vocabulary to `_DECLARED_VALUES` in `plugin.py` so `needs("<field>:<value>")` can use it.
- Gate a capability on what the fleet can actually report: `reasoning_tokens` needs native reasoning accounting from a reasoning parser; inline `<think>` text alone does not qualify.

## Preserve these, or results change meaning

- **Ingress and surface stay orthogonal.** A case receives both and knows the internals of neither, or cross-layer comparison stops meaning anything.
- **Never mutate a cluster.** Cluster reads go through `kube.py`'s verb allowlist.
- **Never retry a test request.** A retry hides the failure under test.

## Before you claim it works

From the repository root:

```sh
source .venv/bin/activate   # ./install.sh --dev creates it
python -m pytest tests/ -q -n auto
ruff check src/ tests/ && ruff format --check src/ tests/
llmdv verify --profile <profile> -k <your case>
```

Then **prove the case can fail**: add the defect to the fake fleet and assert the failure. HTTP and SSE defects go in `tests/transport_fakes.py` (`DEFECTS`, through `FakeFleet(transport=…)`); response-content defects in `tests/fakes.py`; fleet-suite fakes in `tests/fleet_fakes.py`. Assert that the case fails, and that a recorded check is marked not-ok. A bundle whose verdict says FAIL while every check says ok means the deciding assertion recorded nothing.

Finally, update the suite's row in [docs/coverage.md](../../../docs/coverage.md) (case count, status) and the Total row; close or edit a Gaps row if the case fills one.
