---
name: extending-llmdv
description: Use when adding or changing a surface, client, assertion or profile capability in the `llmdv` functional test library, or when supporting another gateway, router or engine product. For a test case or a test suite, use writing-a-test-case.
---

# Extending `llmdv`

Follow [Extending llmdv](../../../docs/extending.md) for implementation procedures and
[Architecture](../../../docs/architecture.md) for the layout. Use the section for your change.

## Add a surface

Use the [surface procedure](../../../docs/extending.md#add-a-surface). Require native request
builders, valid error probes, registration and fake-fleet defects. Run the generic suites.

## Support another gateway, router or engine

Use the [product procedure](../../../docs/extending.md#support-another-gateway-router-or-engine).
`kind` is position, never product. Do not publish backend identifiers. Confirm new response
signals with read-only evidence before adding them to the shared table.

## Add a case or a suite

Use [writing-a-test-case](../writing-a-test-case/SKILL.md).

## Add a client

Use the [client procedure](../../../docs/extending.md#add-a-client). Require native-wire evidence;
a successful process exit alone is not a generation verdict.

## Add an assertion

Use the [assertion procedure](../../../docs/extending.md#add-an-assertion). Record before deciding;
statistical checks use the configured samples rule without retrying requests. Follow
[probing-a-fleet-before-asserting](../probing-a-fleet-before-asserting/SKILL.md) for live behavior.

## Add a profile capability

Use the [capability procedure](../../../docs/extending.md#add-a-profile-capability), including
schema, parsing, gates, authoring views and reference documentation.

## Preserve these, or results change meaning

Keep ingress and surface independent, all skip classes distinct, and the core stack-agnostic.
Never mutate a cluster. Never retry a test request. Every verdict carries evidence.

## Before you claim it works

Complete the [verification steps](../../../docs/extending.md#before-you-claim-it-works), including
a healthy fake and a defect that produces a recorded failing check. Read
[ENVIRONMENT.md](../../ENVIRONMENT.md) before any authorized live check.
