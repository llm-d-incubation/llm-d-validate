---
name: triaging-a-run
description: Use when asked what broke in an llmdv run, why cases failed, whether failures are transient or already tracked by an issue, which layer (LiteLLM, the llm-d router or EPP, vLLM, the model, the deployment) is responsible, or how to reproduce a failure; when deep-diving a run directory, its failure bundles, logs or dashboard.
---

# Triaging a run

Turn a run directory into a short list of causes, each with the issue that tracks it (or none), whether it is real or transient, the layer responsible and how sure that is, and a command that reproduces it. Building the HTML dashboard and its notes is reporting-a-run; writing up a new cause is filing-a-fleet-finding. This skill is the investigation between them.

Investigate read-only against the fleet: rerunning cases and sending single requests is fine; changing a deployment to test a theory needs the user's explicit approval. Writing your notes into the run directory is fine.

The run directory alone answers most of this. The rest depends on what the person has ([`.agents/ENVIRONMENT.md`](../../ENVIRONMENT.md)): `gh` for issue and PR search, cluster access (`kubectl` or `oc`) for pod state and logs, bucket access for other runs. Check first; without one, do that step from what is left and say what you could not check.

## What a run directory holds

| File | Use it for |
|---|---|
| `outcome.json` | `verification.counts` and `class` (exit `1` = fleet), `stack` (every build and version header seen, with counts: two fingerprints mean a rollout mid-run), `started`/`finished` |
| `results.jsonl` | one record per case: `nodeid`, `case_id`, `outcome`, `checks` (each with `name`, `observed`, `expected`, `ok`), `reason`, `ingress`, `surface`, `ts`, `served_by`, `known_failure`, `skip_class` |
| `exchanges.jsonl` | every request and response: `nodeid`, `url`, `status`, `request_body`, `request_headers`, `response_headers`, `response_body`, `transport_error`, `elapsed_s`, and `seq` (numbered within a case, not across the run: order the run by the case's `ts` in `results.jsonl`) |
| `failures/<ingress>/<case>__<variant>/` | `verdict.txt` (the failed check's observed and expected), `request.json`, `response.json`, `exchanges.json`, `checks.json`, `assertion.txt`, `meta.json`, `repro.sh` |
| `reproduce.sh` | reruns the whole run as it was invoked |
| `summary.md` | the case-by-ingress table; with several ingresses, an attribution reading |
| `profile.used.yaml` | the declarations the verdicts were made against, with the MEASURED and TRIAGED notes beside each line |
| `llmdv.log`, `env.json` | the full transcript; llmdv and SDK versions and the resolved setup |
| `logs/` (`--pod-logs`) | pod logs, when the profile declares a log source |
| `dashboard.html` (`--analyze`) | every case with its checks, exchanges and a replay curl (reporting-a-run) |

Every request llmdv sends carries `x-request-id: llmdv-…`, `x-llmdv-run-id` and `x-llmdv-case-id`; they let you find a failing request in the router's and engine's own logs.

The real hostname is in `repro.sh`, `verdict.txt`, `meta.json`, `reason` and `request_headers`: keep all of them out of anything shared.

## 1. Group the failures

One cause usually fails many cases. Group by the first failing check **and** what it observed (a status, an error body's first words), largest first, so unrelated 400s and 500s under one check name stay apart:

```sh
python3 - runs/<run_id> <<'EOF'
import json, sys, collections
groups = collections.defaultdict(list)
for line in open(sys.argv[1] + "/results.jsonl"):
    r = json.loads(line)
    if r["outcome"] not in ("fail", "error"):
        continue
    bad = [c for c in r.get("checks", []) if c.get("ok") is False]
    check = bad[0]["name"] if bad else "reason"
    seen = str(bad[0].get("observed") if bad else (r.get("reason") or "").splitlines()[0])[:50]
    groups[(check, seen)].append(r)
for (check, seen), rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    known = sorted({(r.get("known_failure") or {}).get("issue") for r in rs} - {None})
    print(f"{len(rs):4} {check:28} {seen!r:52} known={known or '-'}")
    print("      " + ", ".join(sorted({r["case_id"] for r in rs})))
EOF
```

Then join groups that share one cause. A defect usually shows under several checks: a 500 on every forced tool call fails `http_status` for plain requests and `stream_transport_error`, `messages_stream_error` and `choice_object` for streamed ones. Split the failing requests by **what they asked for** to find the shared factor:

```sh
python3 - runs/<run_id> <<'EOF'
import json, sys, collections
split = collections.Counter()
for line in open(sys.argv[1] + "/exchanges.jsonl"):
    e = json.loads(line); b = e.get("request_body") if isinstance(e.get("request_body"), dict) else {}
    tc = b.get("tool_choice"); tc = tc if isinstance(tc, str) else (tc or {}).get("type", "named" if tc else None)
    key = (e["url"].split("/v1")[-1], bool(b.get("stream")), bool(b.get("tools")), tc,
           bool(b.get("response_format") or b.get("structured_outputs")), e["status"])
    split[key] += 1
for (path, stream, tools, tc, fmt, status), n in sorted(split.items(), key=lambda kv: str(kv[0])):
    print(f"{n:4} {path:24} stream={stream!s:5} tools={tools!s:5} tool_choice={tc!s:10} format={fmt!s:5} -> {status}")
EOF
```

A status that follows one field exactly (every forced choice 500, every `auto` 200) names the trigger. Add fields to the key until it does. A stream opens with 200 before it fails, so for streamed requests read `transport_error` and the stream checks, not the status.

## 2. For each cause, answer five questions

### Is it already tracked?

- `known_failure` on the records: `status: known` names the issue and the build the fix ships in; `fix_expected` means the fix should have landed and did not, which is a comment on that issue. Null means the profile declares no matching entry, not that nothing tracks it.
- The comment beside the profile line the case judges: a `TRIAGED <date>` note records a team decision (intended, false positive).
- Open issues in the trackers of the layers involved (for example llm-d/llm-d, or the gateway's or engine's own repository) and in llm-d-incubation/llm-d-validate, searching the check name, the case id and the symptom:
  ```sh
  for r in <owner>/<repo> llm-d-incubation/llm-d-validate; do
    gh issue list -R $r --state all --search "<keywords>" -L 20 --json number,title,state -q '.[] | "\(.number) \(.state) \(.title)"'
  done
  gh search prs --owner <owner> "<error signature or symptom>" --json repository,number,title,state
  ```
  Without `gh`, give the person these searches to run in the browser. Search pull requests too: a fix often arrives as a deployment PR that quotes the error. Search for the error's own words (an exception name, a log line) as well as the case. Read the candidates' bodies; a title match is not a match.
- The same case on other models' profiles: their latest scheduled runs are in the results bucket (running-and-comparing-live-runs, "Pull a run back"), and recent local runs under `runs/` (match on `profile` in `results.jsonl`). A failure every model shares (a vLLM API layer, the gateway, fleet-wide flags) is not this deployment's defect.

### Is it transient?

Suspect a moment in the fleet when failures:
- cluster in time **and** are interleaved with passing requests of the same shape that failed nothing: a burst of 5xx, `ReadTimeout` or `peer closed connection` inside a few minutes is a restart, a rollout or an overload. Cases run in a fixed order, suite by suite, so a deterministic defect in one suite also looks like a burst; if every request of that shape fails across the whole window, it is not transient;
- carry an HTML body (a route or ingress with nothing behind it) or a 503 "not ready";
- coincide with two `system_fingerprint` values in `outcome.json` `stack`, or a pod restart (`kubectl get pod … -o jsonpath='{.status.containerStatuses[0].restartCount}'`). Compare the build part of the fingerprint (`vllm-<version>+g<sha>`); the suffix after the parallelism (`-tp<n>-<hash>`) differs between deployments of one build, so it alone does not mean a different engine;
- flip between runs: check the same cases in the previous runs (running-and-comparing-live-runs).

Then rerun the two or three smallest cases now: `llmdv verify --profile <profile> --evidence all -k "<test functions>"`. The function name is the part of `nodeid` after `::`, without the `[...]` (`-k` does not match brackets). Model-dependent cases (tool arguments, effort ordering, repeats at temperature 0) can flip without any change; call them stochastic, not fixed or broken, from one run.

### How did it fail?

- `verdict.txt`: the observed value against the expected one.
- `request.json` and `response.json`: the exact exchange; `exchanges.json` when the case sent several requests.
- A control: a sibling that passes the same check (the non-streamed variant, another surface). No control means the check runs only there; say so.
- The split from step 1, which shows where the defect is and is not.
- The server's own logs (read-only, with access to the cluster serving it; `oc` works the same), found by the request id in `request_headers` or by time:
  ```sh
  kubectl -n <namespace> logs deploy/<epp>  --since=3h | grep <x-request-id>
  kubectl -n <namespace> logs deploy/<vllm> --since=3h | grep -E ' (ERROR|CRITICAL) |Traceback' | sort | uniq -c | sort -rn | head
  ```
  A traceback names the code path; the exact source is in the image's build (vLLM's commit is in `system_fingerprint`, e.g. `+g<sha>`), read that commit, not upstream `main`.
  Without cluster access, or when the logs are gone (the pod was replaced), look for them quoted in issues and PRs, ask the deployment owner, and rerun the failing cases with `--pod-logs all` if the profile declares a log source.

### Which layer is responsible?

Name a layer only as far as the evidence goes (writing-an-infra-profile Rule 2), and give a confidence:

| Evidence | Points at |
|---|---|
| fails at the `litellm` ingress, passes at the `llmd_router` ingress of the same run (`summary.md`); with a single ingress, the same case against the next layer's endpoint | the gateway |
| the error body's shape: LiteLLM's (`litellm.BadRequestError`, `litellm.APIConnectionError`, …), the EPP's plain text or `inference error: <Kind>`, vLLM's JSON error (a 500 from vLLM says only `Internal server error`; its log has the traceback) | the layer that wrote it |
| the same case fails on other models through the same layer | that layer, not the model |
| fails on one deployment, passes on another with the same image and arguments | state (what the pod served first, a stuck engine) before configuration |
| fails only with this model's template, parser or tool format | the model package (template, parser plugin) |
| differs only by a deployment flag between two deployments | the deployment |

A run directory does not record the deployment's arguments: read them from the deployment's manifests or, with cluster access, `oc -n <namespace> get deploy <name> -o jsonpath='{.spec.template.spec.containers[0].args}'`.

Prove it with an A/B where you can: the same request against two endpoints that differ in one thing (gateway vs router, prod vs staging, one flag), recorded with the observed status and body. Two candidate causes need two experiments, not one argument.

### How is it reproduced?

- The bundle's `repro.sh` replays the exact request. It embeds the real hostname and no credential: add the auth header from the profile's `auth.key_env` variable, and replace the host with `$BASE` before sharing it anywhere.
- Trim it to the smallest request that still fails, run it, and keep the real output; a filter (`jq`, a status) that shows the defect makes it readable.
- The library repro is `llmdv verify --profile <profile path> --evidence all -k "<test function>"`: the committed profile if there is one, else the run's own `profile.used.yaml` (with its URL variables exported). `reproduce.sh` reruns the whole run.
- The dashboard has a replay curl per exchange too.

## 3. Hand back a triage

Write `runs/<run_id>/triage.md`, one row per cause (not per check), largest first:

| Cause | Cases | Tracked | Transient? | Layer (confidence) | Evidence | Repro | Next |
|---|---|---|---|---|---|---|---|

**Evidence** is the observation that decided the row (the split, the log line, the A/B); **Next** is the action and who owns it (rerun after a fix, file, comment on an issue, ask the owner).

Lead with what changed against a baseline run of the same profile (running-and-comparing-live-runs for published runs; for local runs, join the two `results.jsonl` on `nodeid`): new causes, fixed ones, and cases a new cause now hides (a case that fails earlier for the new reason says nothing about its old one). Then what is real and whose, and what is noise. Hand the per-group notes to reporting-a-run for the dashboard, and new causes to filing-a-fleet-finding. Keep hostnames out of anything shared.

## Red flags

| Thought | Reality |
|---|---|
| "221 failures, so 221 problems" | Group them. One defect commonly fails dozens of cases. |
| "It was the only configuration difference, so it is the cause" | Same image and arguments can still differ by state. Test it before naming it. |
| "It passed this time, so it is fixed" | Model-dependent cases flip run to run. Look at several runs. |
| "The title matches an open issue" | Read the body; the same symptom can have another cause. |
| "Restarting the pod would tell us" | It would also destroy the evidence, and it changes the deployment: ask first. |
| "The upstream source shows the bug" | Read the commit the image was built from; `main` may already differ. |
