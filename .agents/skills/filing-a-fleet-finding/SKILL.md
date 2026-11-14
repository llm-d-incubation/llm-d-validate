---
name: filing-a-fleet-finding
description: Use when an llmdv run failure needs to be reported, filed or written up as a GitHub issue, when someone asks to "open an issue" for a failure group, check, or finding, or asks for an issue draft they can paste.
---

# Filing a fleet finding

A fleet finding is a defect an llmdv run caught in the deployed serving stack. Each is filed in the issue tracker of the project that owns the failing layer (for example llm-d/llm-d, or the gateway's or engine's own repository), or in llm-d-incubation/llm-d-validate for a defect in llmdv itself, from [template.md](template.md): Summary, Issue, Fix, Repro (curl, then the library), Additional information, Tracking. A finding is one root cause, not one failing test: a single defect can fail dozens of cases.

## Steps

1. **Pick the failure group.** Get the groups as reporting-a-run does, building to a scratch path (`llmdv analyze runs/<run_id> -o <scratch>/dashboard.html`) so the run's own `dashboard.html` is not rewritten. One issue covers one group, or several groups that share a cause. A group's cases are the `results.jsonl` records with `outcome` `fail` or `error` whose first check with `ok: false` has that `name`; a record with no failed check is grouped by the first line of its `reason`, with quoted strings and numbers replaced by `…` and cut to 90 characters. Use the group names `llmdv analyze` prints rather than rebuilding them. A group the dashboard counts as **known** (a `known_failure` label from the profile's `known_failures`) already has an issue: go to it instead of filing. A `fix_expected` label means the fix should have landed and did not; that is a comment on the existing issue.
2. **Collect the evidence** from the run directory:
   - the count of failing cases;
   - **a control:** cases that pass the same check (such as the non-streamed variant); else the nearest surface doing the same thing, labelled a stand-in; else "no control: the check runs only on <X>";
   - one bundle under `failures/<ingress>/<case>/`, giving `verdict.txt` (the failed check's observed and expected values) and `repro.sh` (the exact request);
   - the gateway version, where the ingress reports its own (e.g. `x-litellm-version`);
   - **a split** wherever it shows where the defect is and is not. Each line of `exchanges.jsonl` has `url`, `status`, `request_body`, `response_headers` and `response_body` (a stream is one SSE string; the body can be null). Count by path and stream mode.
3. **Re-verify it now.**
   - **Rerun the cases:** the two or three smallest, with `llmdv verify --profile <p> --evidence all -k "<test functions>"` (function names without the `[...]` suffix: `-k` does not match brackets). Record the date and result.
   - **Run the curl:** run the request from the new `repro.sh` as the curl the issue will show: the same path and body, trimmed headers, and a filter that shows the defect. Paste its real output as "observed".
     - Put the host in `BASE`, read without printing. A committed profile names it by variable (`base_url_env`), a local one may write `base_url` inline; this reads either:
       `BASE=$(.venv/bin/python -c "import os,yaml;i=next(x for x in yaml.safe_load(open('<p>'))['ingresses'] if x['id']=='<ingress>');print(i.get('base_url') or os.environ[i['base_url_env']])")`
       Run it from the repository root; `<ingress>` is the segment of `failures/<ingress>/<case>/`.
     - Load the key from wherever the person keeps it, never echoing it.

   If the defect no longer reproduces, say so and do not file.
4. **Search for existing issues** (needs `gh`; without it, give the person the searches to run) in the tracker you will file in and in llm-d-incubation/llm-d-validate (`gh issue list -R <owner>/<repo> --state open --search "<keywords>"`), and read the candidates' bodies. When one already describes the defect, draft a comment with the new evidence instead, and post it only with the user's explicit go-ahead.
5. **Fill in [template.md](template.md).** Save the draft as `runs/<run_id>/issue-<check>.md`, next to the evidence.
6. **Show the user the title and body in full, ready to copy.** Do not run `gh issue create` or `gh issue comment` without their explicit go-ahead: both publish under the user's name. Once they approve:
   ```sh
   gh issue create -R <owner>/<repo> \
     --title "[Fleet Finding] <what the client sees>" --body-file runs/<run_id>/issue-<check>.md
   ```
   `<owner>/<repo>` is where the fix lives, as far as the evidence shows (Rule 2 of writing-an-infra-profile). When that is unknown or needs a team decision, ask the user which tracker to file in.
7. **Once the fix is traced,** offer to add a `known_failures` entry to the profile (check, cases, issue, fix, and the build the fix ships in; writing-an-infra-profile Rule 4), so later runs label the failure as known instead of reopening it.

## Rules for the content

- **Make no secrets and no host.** Write `$BASE`, and the key as the variable the profile names in `ingresses[].auth.key_env` (`${<auth.key_env>}`). A bundle's `repro.sh` contains the real hostname, so replace it with `$BASE`. Drop `x-request-id` and `x-stainless-*` headers from the curl.
- **Show only commands you ran.** Every curl and `llmdv` command in the issue must have been run in step 3. Its "observed" comment must quote real output.
- **Write the title as the symptom a client sees.** Do not name a component in the title.
- **Name a stack layer only as far as the evidence goes.** Follow writing-an-infra-profile Rule 2. Cite the evidence, for example "vLLM, according to its `system_fingerprint`", or "LiteLLM 1.97.0, from `x-litellm-version`". If you cannot tell whether a path is translated or passed through, say it is unknown.
- **Give the cause as a lead.** The Fix section states the required behaviour as fact. Where the code is likely to be is labelled "a lead, not a confirmed cause".
- **State who confirmed what is expected.** Link the spec section, or write "the team has confirmed". If you only have your own reading of a spec, write "expected per <spec>, not yet confirmed by the team". Leave the profile's declaration out.
- **Point at what changes the picture.** If a pending change, such as a passthrough switch or an upgrade, could alter the behaviour, say to rerun after it.

## Common mistakes

| Mistake | Fix |
|---|---|
| One issue per failing test | One per root cause; list the case count |
| `grep -m1` on a stream | It cuts the pipe short (curl error 23) and may match an `event:` line. Grep on `"type":"<event>"` instead |
| Copying a hostname from `verdict.txt` or `repro.sh` | Replace it with `$BASE` |
| Filing a duplicate | Search the trackers in step 4 and read the bodies. Comment on a match instead |
| A `.local.yaml` path in the library repro | Use the profile's committed path in `profiles/`, without `.local`, so readers can run it as written. If no committed profile exists, write `<profile.yaml>` and link the [profile README](../../../profiles/README.md) |
