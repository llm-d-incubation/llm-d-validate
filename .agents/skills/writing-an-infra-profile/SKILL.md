---
name: writing-an-infra-profile
description: Use when writing, updating or reviewing an `llmdv` infra-profile; when choosing error_status, unstructured_errors, header dispositions, efforts, capabilities or change sentinels; when deciding which case families to leave out; when tempted to declare what a probe returned; before naming LiteLLM, an llm-d router or EPP, or vLLM behind an ingress; when one stack needs several profiles (models, engines, an llm-d guide's deployment configurations); when publishing a profile that was written against a private stack; or when a profile change must be reflected in its folder README's description of what a verification command selects.
---

# Writing an infra-profile

Follow [Writing a profile](../../../docs/writing-profiles.md) and
[Profile evidence and provenance](../../../docs/profile-provenance.md). Field semantics live
in [REFERENCE.md](../../../profiles/REFERENCE.md). This checklist retains agent obligations.

## Procedure

1. Reuse the [closest profile](../../../profiles/README.md), or copy the minimal starter to a
   gitignored `*.local.yaml`. Declare only reachable ingresses. The starter is partial coverage.
2. Before any live step, read [ENVIRONMENT.md](../../ENVIRONMENT.md), establish available access
   and load credentials without printing them. Never mutate the cluster.
3. Measure deployment facts and confirm layer identities with read-only evidence. Follow
   [the probing skill](../probing-a-fleet-before-asserting/SKILL.md) before asserting live behavior.
4. Declare intended behavior; record probes separately. Cover every eligible vocabulary value
   for the target surfaces, respecting schema constraints. Name each exclusion and its reason;
   an unprobed or failing promised feature is not an exclusion.
5. Validate, explain and plan offline; then, within the authorized live scope, run
   `--full-suite -m contract` before broader verification of a newly authored profile.
6. Seed sentinels from a known-good run using the [seeding procedure](../../../docs/profile-provenance.md#seed-change-sentinels).
   Never invent measured values; keep unseeded status visible until evidence exists.
7. Keep failures for triage. Change a behavior expectation only after a recorded decision.
   Known-failure labels never turn a failure into a pass.
8. Keep committed profiles, comments, issues and PRs free of cluster-identifying details.
   Update the folder README and generated selection section with the profile.

## Test sets for shorter runs

Respect requested scope. Use the [CLI selection rules](../../../docs/cli.md#test-sets-and-coverage)
and report partial coverage as partial coverage. A smaller test set does not justify removing
an intended capability. The broader-validation requirements for maintained guide profiles appear below.

### Keep each folder's "What `llmdv verify` selects" section current

Follow [coverage documentation maintenance](../../../docs/maintaining-docs.md#generated-profile-coverage).
Run `selection_docs.py` and its `--check` mode whenever profiles or case IDs change. Commit the
generated README changes in the same PR.

## More than one configuration: parts and combinations

Use the [parts and combinations procedure](../../../docs/profile-provenance.md#more-than-one-configuration-parts-and-combinations).
Write each fact once; name combinations by client-visible configuration, and distinguish configurations
with identity flags, never labels.

### Profiles for an llm-d guide

Read nightly workflows, deploy scripts, rendered overlays, `guide.yaml` and the guide README.
Profile the configurations those workflows deploy; record disagreements with other sources. Follow the
[guide procedure](../../../docs/profile-provenance.md#profiles-for-an-llm-d-guide), including a
full-suite validation run for each configuration and distinct engine build.

#### How profiles/llm-d is built

The [layout and source ownership](../../../docs/profile-provenance.md#how-profilesllm-d-is-built)
define where each declaration belongs and how its provenance is recorded.

### Publishing a profile written against a private stack

Follow [the publication procedure](../../../docs/profile-provenance.md#publishing-a-profile-written-against-a-private-stack).
Scrub identifiers, preserve measured behavior, retain a private overlay and run
`compare_profiles.py` per ingress to prove equivalence. Review the diff, commit and PR text.

## Rule 1: declare intent; probes go in comments

Use [intended behavior](../../../docs/writing-profiles.md#declare-intended-behaviour). Never fit
expectations to a failing response or downgrade a header disposition to produce a green cell.

## Rule 2: no stack claim without read-only evidence

Use [layer evidence](../../../docs/profile-provenance.md#identify-layers-with-evidence), state
confidence, and never quote backend-identifying values. A passed-through engine fingerprint
does not identify the ingress itself.

## Rule 3: everything eligible runs

Follow [coverage requirements](../../../docs/profile-provenance.md#coverage-and-known-failures).
Record gaps explicitly; do not silently widen a user's requested run.

## Rule 4: failures are left for the team

Keep the case failing until triage establishes a defect or an intended deviation. Use
[known failures](../../../profiles/REFERENCE.md#known_failures) only for tracked findings.

In guide documentation and YAML comments, lead with deployment configuration and validation
purpose. Put upstream workflow mappings in labeled maintainer provenance. Follow the
[documentation terminology guidance](../../../docs/maintaining-docs.md).
