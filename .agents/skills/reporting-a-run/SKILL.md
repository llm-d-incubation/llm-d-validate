---
name: reporting-a-run
description: Use when an llmdv run has finished, was killed, or is still running and someone wants to review, triage, share or debug its results, asks for a dashboard, report or analysis of a run directory, or names a run published to S3 or a file:// location.
---

# Reporting a run

llmdv renders any run directory into one self-contained `dashboard.html`: the system under test, the verdict with failures grouped by first failing check, where the results are, the profile, and every test with its rerun command, checks, traceback, transcript and replay curl. Two ways to fill it, one renderer:

| | Static | Agent-assisted (this skill's job) |
|---|---|---|
| Command | `llmdv verify --analyze`, or `llmdv analyze <run_dir>` | `llmdv analyze <run_dir> --notes <notes.json>` after you write the notes |
| Content | Only what llmdv recorded | Plus your analysis, shown labelled "Agent analysis" and "agent note, not recorded by llmdv" |

The static page draws no conclusions. That is your job, and it must stay visibly separate from the evidence.

## Steps

1. **Get the run directory onto this machine.** A local run needs nothing. To pull a published one, follow running-and-comparing-live-runs ("Pull a run back"); the run's `outcome.json` gives its `publication.location`. Without bucket access, ask someone who has it for the run directory. If `llmdv compare --from "$PREFIX" --profile <profile name> --list` cannot list the folder, `aws s3 ls "$PREFIX/<profile name>/" --endpoint-url "$AWS_ENDPOINT_URL_S3"` does.
2. **Build the static page** from the repository root, with every `auth.key_env` variable the run's `profile.used.yaml` names exported, so the credential scan checks their exact values. Load them as [`.agents/ENVIRONMENT.md`](../../ENVIRONMENT.md) describes (ask where the person keeps the key; never echo it), under the profile's names:
   ```sh
   export <auth.key_env>="$<their key variable>"
   .venv/bin/llmdv analyze runs/<run_id>
   ```
   A router-direct profile has no key; nothing needs exporting.
   It prints the counts and every failure group by name; those names are the keys for group notes. If the user asked only for the dashboard, stop here and hand over the path.
3. **Write an intent for every case in the run,** passing ones included: a request body alone does not say what the test was proving. Collect the distinct case ids from `results.jsonl`; each record's `nodeid` names the test function. Read it, its docstring, and the helpers and assertions it calls, then write two or three sentences a reader who has not seen the code can follow:
   - what the case sends, in plain words (the body fields that matter, streamed or not, how many requests);
   - the property it checks;
   - what a pass means.

   Describe the general rule the case enforces, not this profile's path through it, plus one clause on when it skips. Write from the code, never from this run's outcome. For more than about 40 case ids, split them by suite across parallel subagents with these same instructions, then merge.
4. **Investigate, largest failure group first.** For each group, read one or two of its bundles: `verdict.txt`, `response.json`, `transcript.sse` and `assertion.txt` under `failures/`. Also read the comment beside the profile line the case judges (it records what was measured), and [`.agents/GROUND-TRUTH.md`](../../GROUND-TRUTH.md).
5. **Write `runs/<run_id>/agent-notes.json`,** keeping it beside the evidence so the page can be rebuilt:
   ```json
   {
     "author": "Claude <model>, <date>, from this run's evidence",
     "summary": "What to look at first, and how many distinct issues the failures reduce to.",
     "groups": {"choice_index": "What the evidence shows, the file it is in, what to check next."},
     "cases": {"anthropic_messages.streaming_sse": "...", "openai_chat.request_field [logit_bias]": "..."},
     "intents": {"openai_chat.streaming_include_usage": "Streams one Chat completion with stream_options.include_usage and checks for exactly one usage frame, with empty choices, after the finish frame and before [DONE]. A pass means a client that stops at the finish reason still receives token usage."}
   }
   ```
   Group keys are the first-failing-check names the build printed. A case key is a case id, `case_id [variant]`, or the full test name. Intent keys are case ids, and one intent covers every variant of its case.
6. **Embed it:** `llmdv analyze runs/<run_id> --notes runs/<run_id>/agent-notes.json`. It prints how many notes and intents attached. Fix any key it warns "matches no test", and add intents for any case it reports as missing. Exit 5 means a credential reached the data: stop and find it.
7. **Hand over** the dashboard path and your summary in two or three lines.

## Reading the failures

- **Note every failure group.** Write a case note only where a case differs from its group.
- **Join groups with one cause.** When several groups share a root (a broken stream fails `terminal_done`, `chunk_ids` and `terminal_event`), say so in the summary and in each group note.
- **Separate llmdv defects from fleet findings, and allow for both.** A Python error as the reason (`TypeError`, `KeyError`, `IndexError`, `NotImplementedError`) is always an llmdv defect, but can hide a fleet finding: a stream frame with no `choices`, or a choice with no `index`, surfaces as a `KeyError` or `IndexError`. Read the traceback and the transcript (`transcript.sse`, `exchanges.json`) before labelling it:
  - **The frame that raised it breaks the protocol:** label it as both a fleet finding (the malformed frame) and an llmdv defect (unguarded parsing).
  - **The input was valid:** label it "llmdv defect" only.
- **Flag transient failures.** 5xx answers clustered within seconds (a 503 "not ready") are likely a moment in the fleet. Recommend a rerun of those cases before triage.
- **Flag profile claims the evidence contradicts.** If the run's `profile.used.yaml` asserts something the evidence disputes, such as a stack claim, say so in the summary as a lead for the profile's owner. Never edit the profile.

## What a note may say

- **Cite the evidence:** the check name and observed value, the status, the header, the bundle file.
- **Say how strong the lead is.** A pattern match is not a cause.
- **Make no stack claim** beyond writing-an-infra-profile Rule 2. Write "the layer behind the gateway answered …", not "the EPP rejected …", unless the profile records the layer as confirmed.
- **Suggest, do not decide.** False positive, intended, or defect is the team's call. "Likely false positive: the model is text-only" is fine; editing the profile to make it pass is not.
- **Give a next step:** a log to read (LiteLLM's `x-litellm-call-id`, a time window), the rerun command, or the dashboard's replay curl.

## Common mistakes

| Mistake | Fix |
|---|---|
| `grep`-ing the HTML to check for secrets or content | The data is gzip-compressed inside it. The build scans the uncompressed data and refuses on a match. |
| Reporting 200 failures as 200 defects | Work from the failure groups. One stream-format defect can fail dozens of cases. |
| Reading the pass count as coverage | Read the skip classes: `undeclared` rows sent nothing. |
| Building from the repo's profile | The page always uses the run's `profile.used.yaml`, the declarations the verdicts were made against. |
| Sharing the page widely | It contains hostnames from the profile and response headers. Keep it with the run directory; scrub before sharing outside the team. |
