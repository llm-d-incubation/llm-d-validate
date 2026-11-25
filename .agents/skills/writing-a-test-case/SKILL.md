---
name: writing-a-test-case
description: Use when adding or changing a test case or a test suite in `llmdv`: choosing between surface_generic and surface markers, naming a case_id, gating on a capability or fleet policy, adding a suites/<name> package, proving a case can fail against the fake fleet, or updating the guide selections and docs a case reaches; also when a new case has only ever passed.
---

# Writing a test case

A case is a pytest function under `src/llmdv/suites/<suite>/` that drives an ingress through a surface and records what it observed. The human guide is [docs/writing-tests.md](../../../docs/writing-tests.md); this is the procedure and the checks. Paths below are under `src/llmdv/` unless they start with `tests/`, `docs/` or `profiles/`.

## Add a case

```python
pytestmark = pytest.mark.surface_generic


@pytest.mark.case_id("group.what_it_proves")
def test_something(client, surface, model, ingress):
    exchange = client.post(surface.path, json_body=surface.minimal_body(model.public_id))
    journal.record("http_status", exchange.status, "200", ok=exchange.status == 200)
    assert exchange.status == 200
```

`journal` is `from ...evidence import journal`; suites import with relative imports, as `suites/discovery/test_models.py` does. The fixtures a case takes are defined in `plugin.py` (`client`, `surface`, `model`, `ingress`, `profile`, `client_factory`) and `suites/conftest.py` (`samples`, `openai_client`, `anthropic_client`).

| The case | Mark it | It runs |
|---|---|---|
| holds on any generation surface | `surface_generic`, and take `surface` | once per drivable generation surface |
| needs a surface feature | `surface_generic("tools")` | once per drivable surface with it |
| holds on any render surface | `surface_generic("render")` | once per drivable render surface |
| asserts one surface's own grammar | `surface("chat_completions")` | on that surface only |
| does not touch a surface (e.g. `GET /v1/models`) | no `surface` argument | once per ingress |

- The closest marker decides. Both markers on one function, or a feature outside `SURFACE_FEATURES`, fails collection as an `llmdv` defect. A known feature no drivable surface has yet gives one `skip:not_implemented` row.
- Prefer `surface_generic`: a new surface then inherits the case.
- A generic case builds every request through the surface (`minimal_body`, `stream_body`, `invalid_body`, `structured_output_body`) and never writes a wire field name. A body in one surface's shape is garbled on the others: refused for the wrong reason so it passes, or ignored so it fails and blames the fleet.
- The llm-d guide profiles run only the cases their `selection:` globs name (`profiles/llm-d/<guide>/parts/common.yaml`). A new case reaches those lanes only if a glob matches it, so add it to the guides whose claims it tests. Renaming or removing a case id a selection names makes that profile exit 2; check with `llmdv plan --profile <combination>` over every combination under `profiles/llm-d/`.
- `case_id`'s prefix becomes the phase in the output. Use the package's own name. A prefix that names another suite also adds that suite's marker, so `-m openai_chat` selects `openai_chat.disconnect_recovery` in `streaming/`. A prefix that differs from its package fails collection unless the pair is listed, with its reason, in `PREFIX_EXCEPTIONS` in `plugin.py`; add one only when the output and `-m` should group the case away from its package.
- Gate on a profile capability with `@pytest.mark.needs("<capability>")`, or on one value of a closed vocabulary with `needs("<field>:<value>")`. An unknown name is a usage error at collection, never a silent skip. `needs` is read only for a case that runs on a surface (`surface` or `surface_generic`): `gate()` in `plugin.py` returns before reading it for a case with no surface, so gate such a case inside its body instead.
- Mark a case that can disturb a shared fleet `@pytest.mark.disruptive("<reason>")`: it then skips as `excluded_by_policy` under `fleet_policy: shared`.
- A new `suites/<dir>` becomes a pytest marker automatically; its place in the output order is `PHASE_ORDER` in `plugin.py`.
- Every skip is classified, never bare. There are six classes (`SkipClass` in `profile/schema.py`): `undeclared`, `not_implemented`, `requires_instrumentation`, `requires_load`, `excluded_by_policy`, `throttled`.
- Put a reusable check in `assertions/`, not in the test, and record before deciding; [extending-llmdv](../extending-llmdv/SKILL.md#add-an-assertion) has the rule, including `assertions.fail` and statistical checks with `@pytest.mark.samples`.
- An assertion about a live response follows [probing-a-fleet-before-asserting](../probing-a-fleet-before-asserting/SKILL.md): see the response before you write what it must be.

## Add a suite

- Create `suites/<name>/` with an `__init__.py` (every existing suite has one, most empty or a short docstring) and at least one `test_*.py`. Without the `__init__.py` the module is not under `llmdv.suites`, the relative imports fail and the case gets no suite marker.
- The directory name becomes the `-m` marker (`suite_markers()` in `plugin.py`), only once it holds a `test_*.py` file and only if it does not start with `_`. A package of helpers alone is not a marker.
- Case ids in it use the directory name as their prefix, or a pair listed in `PREFIX_EXCEPTIONS` with its reason.
- Its phase sorts alphabetically after `preflight`, `contract` and `discovery`, and before `logs`. Add it to `PHASE_ORDER` only when it must run at a fixed point, with the reason in the comment above it.
- A suite-wide fixture goes in the suite's own `conftest.py` (as `contract/`, `fleet/` and `real_clients/` do); one shared by several suites goes in `suites/conftest.py`.

## Prove the case can fail

A case that has never failed has not been tested. For every new case, and every change to what one decides:

1. Add the defect to the fake fleet, as a real way a layer gets it wrong. HTTP and SSE defects go in `tests/transport_fakes.py` (`DEFECTS`, through `FakeFleet(transport=…)`); response-content defects in `tests/fakes.py`, as a `FakeFleet` flag or a value of one of its `*_defect` arguments; fleet-suite fakes in `tests/fleet_fakes.py`. The healthy fake must stay healthy.
2. Write the offline test: start `FakeFleet(<defect>)`, write its profile with `write_profile` (which uses `profile_yaml` from `tests/fakes.py`), run the shipped suites with `run_suites(profile, tmp_path / "run", "-k", "<your case>")` from `tests/test_suites_against_fake.py`, and read the verdicts with `outcomes(records)`. `test_silent_fallback_to_another_model_is_caught` there is the pattern.
3. Assert that the case fails, and that a recorded check is marked not-ok (`test_a_failing_case_records_a_failing_check`). A bundle whose verdict says FAIL while every check says ok means the deciding assertion recorded nothing.
4. Check the healthy fleet still passes it: `test_healthy_fake_fleet_passes_every_case`, or the same run with no defect.

## The follow-ups

- Update the suite's row in [docs/test-suites.md](../../../docs/test-suites.md) (what it checks, what enables it, its status); close or edit a Gaps row if the case fills one. A new suite gets a new row.
- Add the case to the `selection:` of each llm-d guide whose claims it tests (`profiles/llm-d/<guide>/parts/common.yaml`), and run `llmdv plan --profile <combination>` over every combination under `profiles/llm-d/` after renaming or removing a case id.
- For a new suite, regenerate the coverage graphic with `python docs/images/make_coverage_svg.py`; a test checks the graphic lists every suite.
- A new capability or marker the case needs is a profile change, not a case change: follow [extending-llmdv](../extending-llmdv/SKILL.md#add-a-profile-capability).

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

Then confirm the prove-it-fails test above fails with the defect and passes without it. A live `llmdv verify` runs one case at a time; never add `-n` to it.
