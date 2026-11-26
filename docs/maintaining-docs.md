# Maintaining the documentation

Give each topic one authoritative home. Other pages may repeat a runnable command or a short
reminder needed for their task, then link to the explanation. Keep prerequisites and material
limitations beside the action they affect.

Describe guides and profile comments in terms of deployment configurations, validation scope
and comparison identity. Explain what users test and why it matters. Keep upstream workflow
names and automation-specific overrides in clearly labeled maintainer provenance; preserve
those identifiers exactly. Use “workflow” for automation, “configuration” for the deployed
model/engine/options, and “validation run” for an execution. Avoid “lane.”

| Document | Owns |
| --- | --- |
| Root README | Purpose, prerequisites, brief quick start and navigation |
| Getting started | Installation through the first result and its interpretation |
| Profiles overview | Choosing an existing profile or a starting example |
| llm-d overview | Guide catalog and deployment configuration and comparison identity |
| Individual guide README | Configuration mapping, exact commands, coverage and limitations |
| Writing profiles | Creating and adapting a profile through examples |
| Profile evidence and provenance | Measuring, seeding sentinels, guide parts and public-profile preparation |
| Profile reference | Field semantics, defaults, inheritance and validation |
| Command reference | CLI selection rules, exit codes and output artifacts |
| Publishing and running unattended | Archive setup, retrieval, job operation and delivery troubleshooting |
| Comparing runs | Baseline matching and interpretation of changes |
| Deployment README | Installing and operating the validation runner in a cluster |
| Extending llmdv | Surface, client, assertion and capability implementation procedures |
| Writing tests | Implementing and proving a case, including evidence and gates |
| Architecture and design decisions | Structure, invariants and their rationale |
| Agent skills | Task checklists, required evidence and agent-specific guardrails |

Human guides must be usable without loading a skill. Keep essential safety rules in agent
instructions too, but link shared explanations rather than maintaining a second tutorial.
Design specifications should identify their status and link to current user references.

## Editing and verification

- Start from the behavior being documented, including pending changes explicitly named by the
  task. Do not describe an unresolved review finding as fixed.
- Keep a first-run sequence separate from optional alternatives. Use one profile variable
  consistently and make placeholder replacement explicit.
- Distinguish planned coverage, executed checks and passing results. Keep guide-specific gaps
  visible; a shorter page must not imply broader validation.
- Preserve useful anchors when moving sections, using a short forwarding section where needed.
- Keep generated profile coverage sections reproducible with
  `PYTHONPATH=src python .agents/skills/writing-an-infra-profile/selection_docs.py`, then run
  it with `--check`. Edit the generator rather than hand-editing generated explanations.
- Check local links and fragments, CLI examples, and the relevant documentation/profile tests.
  Check fenced shell examples with `bash -n`, then inspect unquoted placeholders: `<profile>`
  can parse as redirection even when the syntax check passes. Use quoted paths or named
  variables, explain what to replace, and verify selectors against the declared test sets.
  Run the required repository checks before reporting the change complete.

Review the result as four tasks: a first guide run, a first custom-stack run, investigating a
failure, and comparing published runs. Each task should have a clear start and next action.

## Generated profile coverage

Every folder of runnable profiles has a README with a "What `llmdv verify` selects" section.
It records the source of the default, its patterns, optional named sets, and offline plan counts
for the default, smoke, all profile sets and the full suite.

```sh
PYTHONPATH=src python .agents/skills/writing-an-infra-profile/selection_docs.py
PYTHONPATH=src python .agents/skills/writing-an-infra-profile/selection_docs.py --check
```

Regenerate after adding, removing or renaming a runnable profile or case, or changing test sets,
inheritance, declarations or capabilities. Commit the README changes with the source change.
Create a title and introduction before generating a new folder's section, and link it from the
profile catalog.

The helper requires a shared set definition and a default in each folder. It refuses a folder
whose profiles define different sets, or whose planned suites do not match the default patterns.
Such a folder needs an explicit per-profile explanation and a corresponding generator update;
do not silently omit it from verification. Counts are planning evidence, not a live measurement.
