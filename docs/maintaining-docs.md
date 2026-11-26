# Maintaining the documentation

Give each topic one authoritative home. Other pages may repeat a runnable command or a short
reminder needed for their task, then link to the explanation. Keep prerequisites and material
limitations beside the action they affect.

| Document | Owns |
| --- | --- |
| Root README | Purpose, prerequisites, brief quick start and navigation |
| Getting started | Installation through the first result and its interpretation |
| Profiles overview | Choosing an existing profile or a starting example |
| llm-d overview | Guide catalog and shared lane/identity conventions |
| Individual guide README | Configuration mapping, exact commands, coverage and limitations |
| Writing profiles | Creating and adapting a profile through examples |
| Profile reference | Field semantics, defaults, inheritance and validation |
| Command reference | CLI selection rules, exit codes and output artifacts |
| Comparing runs | Baseline matching and interpretation of changes |
| Deployment README | Installing and operating the validation runner in a cluster |
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
  Run the required repository checks before reporting the change complete.

Review the result as four tasks: a first guide run, a first custom-stack run, investigating a
failure, and comparing published runs. Each task should have a clear start and next action.
