---
name: tracking-llm-d-guide-drift
description: Use when checking whether `profiles/llm-d/` has fallen behind llm-d: a new Tier 3 guide or nightly lane with no profile, a lane llm-d removed, or a change to a covered guide's overlays, router values, guide.yaml, README or deploy scripts since the commit the profiles were built from; also before moving that commit forward, or when someone asks what changed upstream.
---

# Tracking llm-d guide drift

The guide profiles are built from llm-d at one commit, which `profiles/llm-d/README.md` states (`at llm-d <commit>`) and every provenance tag repeats. llm-d keeps moving: guides reach Tier 3, lanes are added and removed, overlays change models, windows, parsers and images. This skill finds what moved and turns each change into profile work. Building or changing the profiles themselves follows [writing-an-infra-profile](../writing-an-infra-profile/SKILL.md#profiles-for-an-llm-d-guide); this skill decides what needs it.

**Tier 3** is llm-d's own term ([guides policy, §5](https://github.com/llm-d/llm-d/blob/main/guides/GUIDES-POLICY.md#5-production-readiness-tiers)): a guide with a `guide.yaml` and an end-to-end nightly job. A nightly lane is the evidence, so "a Tier 3 guide with no profile" and "a nightly lane no guide README names" are the same finding.

## Rules

- **Read-only, everywhere.** Clone llm-d into a scratch directory outside this repository. Render overlays locally (`kubectl kustomize <path>`), never against a cluster.
- **Copy names, not values.** A nightly workflow passes namespaces, cluster names, zones, buckets and gateway hosts. None of those enter this repository, an issue or a PR body. Cite a lane by its workflow name and a fact by its file path.
- **A lane is the source of truth,** as in the profile skill: profile what a lane deploys, and record a disagreement with `guide.yaml` or the README under the guide folder's "Upstream notes".

## Procedure

1. **Get llm-d.**

   ```sh
   D=$(mktemp -d)/llm-d
   git clone -q --filter=blob:none https://github.com/llm-d/llm-d.git "$D"
   ```

   Reusing a clone: `git -C "$D" fetch -q origin`.

2. **Run the report.** It compares the pinned commit with `origin/main` (`--from` and `--to` take any commit):

   ```sh
   python .agents/skills/tracking-llm-d-guide-drift/drift.py --llm-d "$D" > /tmp/llm-d-drift.md
   ```

   It has three sections: lanes with no profile, lanes removed, and the changed sources of each covered guide, grouped by the profile fact each kind of file owns. It runs `git` against the clone only.

3. **Lanes with no profile.**
   - **Under `(no guide folder)`:** a new Tier 3 guide. Confirm the guide has a `guide.yaml` and that the lane is scheduled (`on: schedule` in the workflow), then build its folder with the profile skill at the new commit. One issue per new guide, titled with the guide's name.
   - **Under a guide:** a new lane of a covered guide. Render the overlay it applies at the new commit. If a client sees the same model, window, parsers and engine as an existing combination, add the lane to the guide README's lane table with its `--identity` flags. Otherwise it is a new combination.
   - **Already listed under "Not covered"** in `profiles/llm-d/README.md` (fast-model-actuation-keda, #130): leave it. The report lists it every time, by design, until it is covered.

4. **Lanes removed.** Drop the lane's row from the guide README. Delete a combination no lane uses any more, and update the lane counts in `profiles/llm-d/README.md`'s coverage table.

5. **Changed sources.** Read each file's diff (`git -C "$D" diff <old> <new> -- <path>`). By owner:

   | Group in the report | What to check | Where it lands |
   | --- | --- | --- |
   | lanes (workflows, deploy scripts) | accelerator, engine, provider, gateway class, the overlay path, replicas | the guide README's lane table; a new or retired combination |
   | model-server overlays | render the lane's overlay at both commits and diff the output: model id, `--max-model-len`, reasoning and tool-call parsers, image, flags, per role | `parts/models/<m>.yaml`, `parts/engines/<e>.yaml`, the combination's parser names |
   | router configuration | plugins, flow control, scorers, headers the router adds or reads | `parts/topology/standalone.yaml` (header dispositions), the guide's `parts/common.yaml` (`selection`, claims) |
   | statements (`guide.yaml`, README) | a claim added or dropped; a support table that now disagrees with the lanes | the guide's `selection` and its comments; "Upstream notes" |
   | shared recipes, `guides/env.sh` | which guides' overlays include the changed component (`grep -rl` in the rendered output) | every combination whose engine build or image changes |

   A file can change without changing anything a client sees (a comment, a resource limit, a benchmark setting). Say so and move on: not every change is profile work.

   To render one overlay at both commits:

   ```sh
   git -C "$D" worktree add -q "$D-old" <old> && git -C "$D" worktree add -q "$D-new" <new>
   diff <(kubectl kustomize "$D-old/<overlay>") <(kubectl kustomize "$D-new/<overlay>")
   ```

6. **Make the changes** with the profile skill: tag every new or changed line `FROM values: <path>` at the new commit, and keep `WRITTEN, NOT RUN` on anything no run has measured. Then, for each changed combination:

   ```sh
   llmdv profile validate <combination>
   llmdv plan --profile <combination> -q
   python -m pytest tests/test_example_profiles.py -q
   ```

7. **Move the pin only when every guide has been checked** at the new commit. The pin is one commit for all guides, so moving it claims every guide is current. Replace it everywhere in one commit (`git grep -l <old>`): the provenance tags, `profiles/llm-d/README.md`, the guide READMEs, `README.md`, `docs/getting-started.md` and the profile skill. A guide checked but left unchanged still gets the new commit in its provenance.

8. **Report.** What moved and what it means: new guides and lanes, removed lanes, changed facts by guide, and the changes that needed no profile work, each with its file path. File an issue per new guide or per guide whose profiles must change, and link it from the PR.

## Red flags

- Writing a namespace, cluster or bucket from a workflow into a profile, README, issue or PR.
- Moving the pin after checking only the guides the report flagged.
- Changing a profile from `guide.yaml` or a README alone, without the lane and its rendered overlay.
- Rendering with `kubectl apply`, `--dry-run=server`, or anything else that talks to a cluster.
- Dropping a lane listed under "Not covered" to make the report empty.
