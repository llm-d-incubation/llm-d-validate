---
name: extending-llmdv
description: Use when adding or changing a surface, client, assertion or profile capability in the `llmdv` functional test library, or when supporting another gateway, router or engine product. For a test case or a test suite, use writing-a-test-case.
---

# Extending `llmdv`

Each addition is one module plus one registry line. [docs/architecture.md](../../../docs/architecture.md) has the layout; this is the procedure and the checks. Paths below are under `src/llmdv/` unless they start with `tests/`.

## Add a surface

1. Implement `WireSurface` in `surfaces/<name>.py` for the native request and error contract. A generation API also implements `Surface`: `parse_completion`, `assemble`, `invalid_body` and streaming. Render APIs declare the `render` feature and skip generation parsing.
2. Declare only features in `SURFACE_FEATURES` (`surfaces/base.py`), each with the methods it gates: `structured_output` → `structured_output_body`, `image_input` → `image_body` and `image_url_body`, `tools` → `tools_body`, `cache_usage` → `parse_cache_usage` and `stream_cache_usage`. An unknown feature is refused at registration.
3. Implement `invalid_body` for every class in `REQUEST_ERROR_CLASSES`, and `TOOL_ERROR_CLASSES` when the features include `tools`, in the surface's own shape. That is what lets surface-generic cases run unchanged. Put headers the wire needs in `extra_headers`; every request sends them.
4. Register it: one import in `surfaces/__init__.py`.
5. Add its path to `SURFACE_PATHS` and a shape-valid body to `probe_request` in `surfaces/base.py`, so the contract suite can probe for it.
6. Add cases under `suites/<name>/`.
7. Teach the fake fleet (`tests/fakes.py`, with wire helpers in `tests/fake_openai.py` and `tests/fake_anthropic.py`) to serve it, validating its own shape, with at least one injectable defect. Run the surface-generic suites against it.

## Support another gateway, router or engine

A product needs no code: `kind` is a position (`gateway`, `router`, `engine`), never a product, and a profile names the product in `id` and in the top-level `engine` and `gateway`. Every behaviour (surfaces, statuses, envelopes, headers) is declared per ingress. Never add a product to `IngressKind`; `tests/test_stack_agnostic_core.py` refuses it.

What a product's responses reveal is evidence, not behaviour, and it lives in one table, `ingress/signals.py`, read at every layer. A profile can declare a header the table does not know (`ingresses[].version_headers`) without any code. To add it for everyone, add a row once the header has been seen on a real response, with a comment saying where: `Reveals.VERSION` if the value names a build or layer and is safe to publish (mark `build=True` only if it changes when the build does), `Reveals.BACKEND` if it may name a host, pod or Service (never published), `Reveals.RECORDED` for other evidence, `Reveals.SIGNATURE` for a product's own body text. An exact `backend` header always wins over a published prefix.

## Add a case or a suite

Cases, suites, markers, fixtures and proving a case can fail have their own skill: [writing-a-test-case](../writing-a-test-case/SKILL.md).

## Add a client

A selector is one entry under `clients:` in a profile. A pinned Claude Code probe also declares `binaries` with exact versions and paths. The pin lives in the `Containerfile` and in the profiles' `binaries`; move it with the procedure in [deploy/README.md](../../../deploy/README.md#claude-code-in-the-runner). A different client needs its own invocation adapter and native-wire evidence checks; a successful process exit alone is never a generation verdict.

## Add an assertion

Put it in `assertions/`, not in a test. Record what it observed through `journal.record(..., ok=...)` **before** asserting, so a failure shows the value that caused it. For a deciding failure call `assertions.fail(name, observed, expected)`: it redacts the values, records the failed check and raises. Passing checks of large payloads record counts and lengths; the raw exchanges keep the detail.

A claim that holds over many requests rather than per request (repeats agree, outputs differ, one effort reasons less than another) is one statistical check. Declare its default rule on the case with `@pytest.mark.samples("count" | "ratio" | "order", n=..., <bounds>)`, take the resolved rule from the `samples` fixture, send its requests through `assertions.samples.draw` and decide with `judge`. `draw` sends exactly n per group, never fewer and never a retry; `judge` records the rule and every sample with its exchange `seq`. A profile sets n and the bounds under `capabilities.samples`, keyed by case id; see `profiles/REFERENCE.md`.

Return `Completion` from `parse_completion` and `assemble` so shared assertions apply. A parallel assertion stack is how streaming and non-streaming drift apart.

## Add a profile capability

- A cross-surface switch takes `false`, `true`, or an explicit surface list (`structured_output: [chat_completions, responses]`). Follow that rule for new ones.
- An API-specific value list (`chat_options`, `messages_thinking`, `responses_reasoning_efforts`) selects native option values, not surfaces. Each vocabulary lives once in `profile/schema.py` and is shared by parsing and case generation.
- Structured configuration such as `cache_reuse` keeps its `surfaces` selector alongside its own settings.
- Every capability may be overridden per ingress. When adding one, check that `profile explain --field` reports its parsed effective value and source assignments, and that `profile show --yaml` reloads with the same contract. Extend `tests/test_profile_inspection.py` when the new field introduces a normalization or composition case not already covered; the authoring views use the existing loader and composer.
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

Finally, update the suite's row in [docs/test-suites.md](../../../docs/test-suites.md) (what it checks, what enables it, its status); close or edit a Gaps row if the case fills one.
