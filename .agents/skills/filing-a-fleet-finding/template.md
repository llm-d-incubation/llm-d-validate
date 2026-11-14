<!--
Title: [Fleet Finding] <the symptom a client sees, with no component named>
Replace every <...> EXCEPT the values on the curl block's `export` line: <your gateway URL> and
<your gateway key> stay as written, so no host or key reaches the issue. <KEY_ENV> becomes the
variable's NAME (the profile's auth.key_env), never its value. Delete any comment block once its section is
filled in.
-->

## Summary

A finding from [llm-d-validate](https://github.com/llm-d-incubation/llm-d-validate): <one or two sentences: what a client sends, and what it gets back that it should not>.

The `<check>` check failed on <N> cases in run `<run_id>`. Re-verified on <YYYY-MM-DD> with a rerun of <n> cases.

## Issue

<What is wrong, and where it happens. Say where it does not happen too: a control that passes narrows the search.>

| <Surface / variant> | <Variant A> | <Variant B> |
|---|---|---|
| <...> | <observed> ✅ | <observed> ❌ (<k> of <m>) |

**Impact:**
- <what breaks for a real client>
- <...>

Stack: <layer and version, with the evidence for each, e.g. "LiteLLM 1.97.0, from `x-litellm-version`">.

## Fix

**Required behaviour:** <what the stack must do instead, with its source: the spec, or "confirmed by the team">.

**Where the fix likely is:** this is a lead, not a confirmed cause. <What the evidence points at, and why: e.g. only the streaming path fails, the non-streamed path is correct>.

**Done when:** `llmdv verify` against the gateway passes `<check>` on <the affected cases>.

## Repro

### curl

The suite writes a `repro.sh` for every failure, containing the exact request and no credential. This is that request with the host replaced by `$BASE`:

```sh
export BASE=<your gateway URL>  <KEY_ENV>=<your gateway key>   # stays a placeholder

curl -sS -N -X POST "$BASE<path>" \
  -H "Authorization: Bearer ${<KEY_ENV>}" -H 'content-type: application/json' <other headers> \
  --data-binary '<body>' \
  | <filter that shows the defect>
# observed: <real output>     expected: <...>
```

### With llm-d-validate

**Prerequisite:** Python 3.10 or newer. Install llmdv once. `install.sh` creates `.venv`, installs llmdv into it and checks that the `llmdv` command runs. Rerunning it is safe.

```sh
git clone https://github.com/llm-d-incubation/llm-d-validate.git
cd llm-d-validate
./install.sh                 # --s3 also installs boto3, to publish runs to S3
source .venv/bin/activate    # or call .venv/bin/llmdv directly
```

Then export `<KEY_ENV>` and run with the profile in the repo:

```sh
llmdv verify --profile profiles/<committed profile>.yaml --evidence all -k "<test_function_a> or <test_function_b>"
```

| Case | Today | After the fix |
|---|---|---|
| `<test[variant]>` | FAIL `<check>` | PASS |
| `<control[variant]>` | PASS (control) | PASS |

Every failure leaves a bundle under `runs/<run_id>/failures/<ingress>/<case>/`:

```
verdict.txt     every check, with the failing one marked
repro.sh        the exact request as a curl; needs only <KEY_ENV> exported
request.json    the request body
response.json / transcript.sse   what came back
```

**Using the suite while fixing this:**
- **Check a fix:** point the profile at staging and run the command above.
- **Check for regressions:** run without `-k`.
- **Triage:** `llmdv analyze runs/<run_id>`, or `llmdv verify --analyze`, builds one HTML page showing each test with its request, response, checks and replay curl.

## Additional information

<Anything else that helps: related failure groups that share this cause, a pending change to rerun after (an upgrade or a config switch), logs to read (e.g. `x-litellm-call-id`, the time window)>

## Tracking

- <related open issues, each with one line saying how it relates: same defect, likely cause, umbrella>
