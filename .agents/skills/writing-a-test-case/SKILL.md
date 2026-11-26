---
name: writing-a-test-case
description: >-
  Use when adding or changing a test case or suite in llmdv: markers, case IDs,
  capability and policy gates, fake-fleet failure tests, and profile test-set follow-ups.
---

# Writing a test case

Follow [Writing tests](../../../docs/writing-tests.md) for implementation details, markers,
fixtures and examples. This is the agent checklist.

## Add a case

1. Read the relevant [ground truth](../../GROUND-TRUTH.md). Before asserting a live response,
   follow [probing-a-fleet-before-asserting](../probing-a-fleet-before-asserting/SKILL.md).
2. Use a stable suite-prefixed case ID, the appropriate surface marker and surface-owned request
   builders. Ingress and surface remain independent.
3. Gate on declared capabilities and fleet policy. For a case without a surface, gate inside
   its body: `needs` is not evaluated there. Classify every skip.
4. Record the deciding evidence before asserting; use shared assertion helpers. Statistical
   cases use the resolved `samples` rule, `draw` and `judge`, without retries.

## Add a suite

Follow [Add a new suite](../../../docs/writing-tests.md#add-a-new-suite), including `__init__.py`,
marker discovery, phase ordering and fixture placement.

## Prove the case can fail

Follow the [fake-fleet procedure](../../../docs/writing-tests.md#prove-it-can-fail) for every new
case and every change to a verdict. Require failure against the injected defect, a not-ok
recorded check, and a pass against the healthy fake. A failed verdict with only passing checks
is insufficient evidence.

## The follow-ups

Use the [PR checklist](../../../docs/writing-tests.md#before-you-open-the-pr): update affected
test sets, the suite catalog, generated profile selection tables and, for new suites, the
coverage graphic. Renamed or removed IDs must leave no broken patterns. Capabilities and new
surfaces also follow [extending-llmdv](../extending-llmdv/SKILL.md).

## Preserve these, or results change meaning

Never mutate a cluster or retry a test request. Keep all six skip classes distinct. Never
replace surface-native requests with hardcoded wire fields in a generic case.

## Before you claim it works

Run the required offline tests, lint and formatting in [CONTRIBUTING.md](../../../CONTRIBUTING.md#development-and-testing).
Before an authorized live check, read [ENVIRONMENT.md](../../ENVIRONMENT.md); preview the
selection, run cases one at a time and report which behavior was actually observed.
