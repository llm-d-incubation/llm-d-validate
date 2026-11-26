# Writing a test case

A case is a pytest function in a module under `src/llmdv/suites/<suite>/`. `llmdv` runs it against
each selected ingress (gateway, router, engine), providing evidence to help locate a failure.
A case records what it observed, then asserts what the API defines: not what one deployment
happens to do today.

Read [architecture.md](architecture.md) for the layout, and
[GROUND-TRUTH.md](../.agents/GROUND-TRUTH.md) before you assert anything about a live response.

## Anatomy of a case

```python
"""Why this module exists, and what a failure here means."""

from __future__ import annotations

import pytest

from ...evidence import journal
from ..conftest import budget_for, check_json_response

pytestmark = pytest.mark.surface_generic


@pytest.mark.case_id("scenarios.finish_reason_reported")
def test_finish_reason_reported(client, surface, model, ingress):
    """A minimal generation says why it stopped."""
    body = surface.minimal_body(model.public_id, max_tokens=budget_for(model))
    exchange = client.post(surface.path, json_body=body)
    completion = surface.parse_completion(check_json_response(exchange, ingress))

    reason = completion.finish_reason
    journal.record("finish_reason", reason, "a reported reason", ok=reason is not None)
    assert reason is not None, f"{ingress.id!r} returned no finish reason"
```

`check_json_response` (in `suites/conftest.py`) handles a throttled `429`, records the status and
checks for a JSON object. The shared helpers there and in `src/llmdv/assertions/` already cover
most of the common checks, so reuse them before you write your own.

### `case_id`

`@pytest.mark.case_id("<suite>.<what_it_proves>")` is the case's stable key in results, the
matrix, run comparisons and profile `test_sets.default` lists. Renaming it breaks all of those, so choose
it once. The prefix before the dot is the phase heading in the output, and must be the suite's
package name (see [Add a new suite](#add-a-new-suite)).

### Markers

The marker decides how many times the case runs. The closest marker wins: a function marker
overrides the module's `pytestmark`.

| The case | Mark it | It runs |
|---|---|---|
| holds on any generation surface | `surface_generic`, and take `surface` | once per drivable generation surface |
| needs a surface feature | `surface_generic("tools")` | once per drivable surface with it |
| holds on any render surface | `surface_generic("render")` | once per drivable render surface |
| asserts one surface's own grammar | `surface("chat_completions")` | on that surface only |
| does not touch a surface (e.g. `GET /v1/models`) | no `surface` argument | once per ingress |

Prefer `surface_generic`: a surface added later inherits the case. A generic case builds every
request through the surface (`minimal_body`, `stream_body`, `invalid_body`,
`structured_output_body`) and never writes a wire field name; a body in one surface's shape is
garbled on the others. Both markers on one function, or a feature outside `SURFACE_FEATURES`,
fails collection as a tool defect. A known feature with no drivable surface produces one
`not_implemented` skip.

### Fixtures

The plugin (`src/llmdv/plugin.py`) and `src/llmdv/suites/conftest.py` provide these:

| Fixture | What it is |
|---|---|
| `ingress` | the layer under test, from the profile; parametrized, one run per ingress |
| `model` | that ingress's resolved model (`model.public_id`, `model.all_names`, `model.max_model_len`) |
| `surface` | the `Surface` implementation for this run; needs a `surface` or `surface_generic` marker |
| `client` | a recording HTTP client bound to the ingress; every exchange lands in the evidence |
| `profile` | the whole profile |
| `openai_client`, `anthropic_client` | the pinned SDKs, recorded the same way; only where SDK compatibility is the claim |
| `client_factory` | a client for an ingress named at runtime |
| `samples` | the resolved statistical rule of a `samples` case |

### Record, then assert

`journal.record(name, observed, expected, ok=...)` writes one check into the case's results row
and evidence bundle. Call it **before** the assertion that decides, so a failure shows the value
that caused it. A verdict carries its evidence: a pass with no recorded checks cannot be told
apart from a test that checked nothing. For a deciding failure, `assertions.fail(name, observed,
expected)` redacts the values, records a not-ok check and raises in one call.

### Gates

- `@pytest.mark.needs("<capability>")` skips the case as `undeclared` unless the ingress declares
  that capability; `needs("<field>:<value>")` gates on one value of a closed vocabulary, such as
  `needs("messages_checks:parallel_tool_calls")`. An unknown name is a usage error at
  collection, never a silent skip. The gate is applied only to cases that run on a surface.
  For a case without a surface, `gate()` returns before reading `needs`; check the capability
  inside the case body and raise a classified skip instead.
- `@pytest.mark.disruptive("<reason>")` marks a case that costs a fleet real work or sends
  deliberately malformed input. Under `fleet_policy: shared` it skips as `excluded_by_policy`
  before any connection opens.
- `@pytest.mark.samples("count" | "ratio" | "order", n=..., <bounds>)` declares a claim that holds
  over many requests rather than one (repeats agree, outputs differ). Send through
  `assertions.samples.draw` using the resolved `samples` fixture and decide with `judge`; see
  [Adding an assertion](extending.md#add-an-assertion).

## Skips

Every skip carries one of the [six skip classes](cli.md#skip-classes): `undeclared`,
`not_implemented`, `requires_instrumentation`, `requires_load`, `excluded_by_policy`, `throttled`.
Record each shipped case ID and its scheduling reason in `src/llmdv/execution.py`.
Review the requests of every variant before marking a case eligible. Hold cache observations,
timing budgets, shared counters, cancellation, repeatability and disruptive cases exclusive.
New extension IDs run exclusively by default.

Never write a bare `pytest.skip`. The markers above classify their own skips; a skip you raise in
the case body starts its reason with the class in brackets, which the plugin reads:

```python
pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no model.answer_max_tokens")
```

## Prove it can fail

A case that has never failed has not been tested. Before you trust a pass, make the fake fleet
commit the defect your case exists to catch, and show that the case catches it.

1. Add the defect to the fake. Response-content defects go in `tests/fakes.py`, usually as a
   `FakeFleet` keyword argument. HTTP and SSE defects go in `tests/transport_fakes.py`, fleet-suite
   defects in `tests/fleet_fakes.py`.
2. Write an offline test that runs the real suites against the fake with the defect on, and
   assert the case fails with a not-ok check. Assert also that it passes without the defect.

`tests/test_suites_against_fake.py` provides the helpers. This is the pattern its tests use,
here with an `omit_finish_reason` flag you would add to `FakeFleet` for the example case:

```python
from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


def test_missing_finish_reason_is_caught(tmp_path):
    with FakeFleet(omit_finish_reason=True) as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "finish_reason_reported")

    assert code == 1
    for record in records:
        assert record["outcome"] == "fail"
        assert any(c["name"] == "finish_reason" and not c["ok"] for c in record["checks"])


def test_finish_reason_passes_on_a_healthy_fleet(tmp_path):
    with FakeFleet() as fleet:
        profile = write_profile(tmp_path, fleet)
        code, records = run_suites(profile, tmp_path / "run", "-k", "finish_reason_reported")

    assert code == 0 and records
    assert all(record["outcome"] == "pass" for record in records)
```

`run_suites` runs pytest on `src/llmdv/suites/` in a subprocess, as `llmdv verify` does, and
returns the exit code and the rows of `results.jsonl`. A row whose outcome is `fail` while every
check is ok means the deciding assertion recorded nothing.

HTTP and SSE defects belong in `tests/transport_fakes.py` (`DEFECTS`, passed through
`FakeFleet(transport=...)`); response-content defects belong in `tests/fakes.py`, and fleet-suite
defects in `tests/fleet_fakes.py`. Keep the healthy fake healthy. Never retry a request to make
a case pass.

## Run it

Offline, against the fakes (replace `<area>` with the test module you added):

```sh
source .venv/bin/activate
python -m pytest 'tests/test_<area>.py' -q
```

Then against a real stack. Set `P` to the local profile path and `CASE` to your test function
name. `plan` shows what would run and skip, and sends nothing:

```sh
llmdv plan --profile "$P" --full-suite -k "$CASE"
llmdv verify --profile "$P" --full-suite -k "$CASE"
```

`--full-suite` widens the scope to every case in the library, so `-k` finds your new case even
though no profile's test set lists it yet. Without it, `-k` would search only the profile's
`default` set, find nothing and exit 2.

## Add a new suite

1. Create `src/llmdv/suites/<name>/` with an `__init__.py` (every suite has one; cases import
   shared helpers relatively, such as `from ..conftest import ...`) and at least one `test_*.py`.
2. The directory name becomes the suite's pytest marker, so `llmdv verify --profile <profile> --full-suite -m <name>` selects it.
   No registration is needed.
3. Every `case_id` prefix must equal `<name>`. A prefix that differs from its package fails
   collection unless the pair is listed, with its reason, in `PREFIX_EXCEPTIONS` in
   `src/llmdv/plugin.py`. A prefix that names another suite also gives the case that suite's
   marker.
4. Suites run in the order of `PHASE_ORDER` in `plugin.py`: `preflight`, `contract` and
   `discovery` first, `logs` last, everything else alphabetically between. Add an entry only if
   your suite must run at a fixed place.

A suite directory needs a `test_*.py` to become a marker; helper-only packages and directories
starting with `_` are not suites. Put a suite-only fixture in that suite's `conftest.py`, and a
fixture shared by suites in `src/llmdv/suites/conftest.py`.

## Before you open the PR

- [ ] The case can fail: the defect is in a fake, and an offline test shows it fails with the
      defect and passes without it.
- [ ] Every skip is classified.
- [ ] The suite's row in [test-suites.md](test-suites.md) says what it checks, what enables it,
      and its status. Close or edit a Gaps row if the case fills one.
- [ ] The case is in the `test_sets.default` of every llm-d guide whose claims it tests
      (`profiles/llm-d/<guide>/parts/common.yaml`); guide configurations run only the cases a test set
      names. Then plan every combination, since a test set naming a missing case id exits `2`:
      `for p in profiles/llm-d/*/*.yaml; do llmdv plan --profile "$p" -q || echo "$p"; done`.
- [ ] Built-in and profile-defined test sets have no stale case IDs. Keep smoke small: discovery,
      basic generation and streaming. Reuse case IDs instead of creating a duplicate smoke suite.
- [ ] [Generated selection sections](maintaining-docs.md#generated-profile-coverage) are current.
- [ ] For a new suite, the coverage graphic is regenerated with
      `python docs/images/make_coverage_svg.py`. The offline tests check that it lists every
      suite.
- [ ] `ruff check src/ tests/`, `ruff format --check src/ tests/` and
      `python -m pytest tests/ -q -n auto` pass.

Contributor process (issues, sign-off, PR description) is in
[CONTRIBUTING.md](../CONTRIBUTING.md).

## For agents

Agents follow the `writing-a-test-case` skill,
[`.agents/skills/writing-a-test-case/SKILL.md`](../.agents/skills/writing-a-test-case/SKILL.md),
which provides a checklist and links to these procedures.
