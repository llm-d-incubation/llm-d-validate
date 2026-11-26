# Getting started

Run a small check against a deployed endpoint, read its evidence, then widen coverage. This
walkthrough uses a local machine; [cluster jobs](../deploy/README.md) use the same profiles and
produce the same kind of run directory.

## Install

You need Python 3.10+ and Git. Clone the repository to obtain the installer and profiles:

```sh
git clone https://github.com/llm-d-incubation/llm-d-validate.git
cd llm-d-validate
./install.sh
source .venv/bin/activate
```

You also need a reachable model endpoint, and a credential if it authenticates. Cluster access
is optional for URL-only verification. `llmdv` does not create or supervise port-forwards.
If your profile declares setup, `llmdv profile show "$P" --setup` prints what is required;
resolving declared pods can read the cluster.

Choose one of the two paths below. Keep credentials in your environment or a private credential
store. Never put their values in committed profiles or shared output.

## Test a deployed llm-d guide

Find your deployment in the [guide catalog](../profiles/llm-d/README.md). Its README maps
configurations to profiles. The following example is for optimized-baseline's Qwen3-32B/vLLM
configuration with parsers; use the matching profile for your own deployment:

```sh
export P=profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml
export LLMDV_ROUTER_URL='https://your-router.example'
```

Replace the example URL. These profiles target the router directly and declare no authentication.
To test more layers, follow [adding a gateway or engine](../profiles/README.md#include-a-gateway-or-engine).
Continue at [Start with a smoke check](#start-with-a-smoke-check).

## Run your own stack

Copy the minimal example to a local profile:

```sh
cp profiles/examples/example-minimal.yaml profiles/my-stack.local.yaml
export P=profiles/my-stack.local.yaml
```

Edit the copy before running it:

- Set `model.public_id` to the model ID your endpoint serves.
- Set the ingress `kind` to its position: `gateway`, `router` or `engine`.
- Set `surfaces` to the APIs it is intended to serve. The starter declares Chat Completions.
- Review expected error statuses. They state the intended contract, not values copied from a
  failed request. If the endpoint does not authenticate, use `auth: {type: none}`.

The starter reads `LLMDV_BASE_URL` and, for bearer authentication, `LLMDV_API_KEY`. Set the URL
and load the key from your credential store. In Bash, you can enter it without putting it in
shell history:

```bash
export LLMDV_BASE_URL='https://your-gateway.example'
read -r -s -p 'API key: ' LLMDV_API_KEY
export LLMDV_API_KEY
```

Replace the URL; omit the key step for unauthenticated endpoints. Local `*.local.yaml` files
are gitignored. [Writing a profile](writing-profiles.md) explains additional capabilities,
per-layer model aliases and reuse through `extends`.

## Start with a smoke check

Both paths now have a profile in `$P` and its endpoint variables set:

```sh
llmdv profile validate "$P"
llmdv plan --profile "$P" --test-set smoke
llmdv verify --profile "$P" --test-set smoke --analyze
```

`validate` and `plan` are offline. Review the plan before the live command: smoke selects model
discovery and basic non-streaming/streaming generation for the declared APIs. Cases that do not
apply are skipped. A smoke pass establishes only those checks, not tools, caching, long context
or every API behavior.

Verification runs one case at a time. Do not add pytest's `-n`, and avoid competing live runs
against the same endpoint. Keep a laptop awake until the run finishes.

## Read the results

The command prints a new directory under `runs/`. Each invocation creates its own child, so
reusing `--local-output ROOT` preserves earlier runs. Use the printed child path below:

```sh
export RUN='runs/<the directory printed by verify>'
cat "$RUN/summary.md"
```

Open `$RUN/dashboard.html` in your browser. If you did not pass `--analyze`, build it with
`llmdv analyze "$RUN"`. Start with the executed/ skipped counts, then the failures and their
recorded checks. A green result applies only to checks that actually executed.

| Result | Next action |
| --- | --- |
| Passed, with executed checks | Continue to the profile's default coverage below |
| Failed | Read the failing check and its evidence; confirm the profile expectation before attributing the failure |
| Skipped | Read the reason: an omitted declaration, missing instrument, policy or throttling is not a passing check |
| Usage/profile error (exit 2) | Correct the command or profile; no verification occurred |
| Environment error (exit 3) | Read the reported endpoint, credential, local connection or results-store problem |

The [command reference](cli.md#exit-codes) defines every exit code and
[skip class](cli.md#skip-classes). [Output files](cli.md#output) lists all artifacts.

### Follow one failure

For example, suppose a generation case records `http_status = 401`, expected `200`. This is an
illustrative failure, not a result from a particular deployment.

1. Open the case in the dashboard and read its request, response and first failing check.
   Confirm the selected ingress and API path. A 401 on a case deliberately testing bad
   credentials can be expected; a 401 on ordinary generation is different.
2. Check that the profile names the correct URL and authentication variable, and that the
   credential is loaded. Never paste the credential into a report. If preflight stopped the
   run with exit 3, resolve that environment problem before interpreting fleet behavior.
3. If the profile describes the intended endpoint and valid authentication, keep the failing
   expectation and investigate the serving stack. Do not change the expected status to 401
   merely to make the case pass.
4. After correcting the cause, load the required endpoint and credential variables and replay
   the failing exchange with the script in that case's bundle:

   ```sh
   bash "$RUN/failures/<ingress>/<case>/repro.sh"
   ```

A replay sends a live request. Use the actual bundle path shown by the dashboard; the
[reproduction reference](cli.md#reproducing-a-run) also explains rerunning the whole selection.
Compare the new evidence with the original, and retain the original run for the report.

## Widen coverage

Once smoke completes, run the profile's default selection:

```sh
llmdv plan --profile "$P"
llmdv verify --profile "$P" --analyze
```

For a newly written profile, check the declared surfaces and headers explicitly with
`llmdv verify --profile "$P" --full-suite -m contract` before relying on broader results.
This checks the declarations; it is a different task from the initial smoke check.

A guide's default tests its selected API and guide behavior. The minimal starter's default is
small and leaves optional capabilities undeclared. Add the intended declarations as described
in [Writing a profile](writing-profiles.md). When you want every applicable library case,
preview `llmdv plan --profile "$P" --full-suite` before running it.

For named subsets, individual suites or layer restrictions, use
[Selection and coverage](cli.md#test-sets-and-coverage). No `--ingress` means every declared
layer; an explicit selector restricts the run to the named IDs.

## Publish a run

Follow [Publishing and running unattended](running.md#publish-a-run) for storage credentials,
archive layout and pulling results back.

## Compare runs

[Comparing runs](compare.md) explains baseline matching and how to interpret changes.

## Run unattended

Use [the job workflow](running.md#run-unattended) for CI and notifications, or
[cluster jobs](../deploy/README.md) for the runner deployment.

## Rules for every live run

The [operational checklist](running.md#rules-for-every-live-run) covers concurrency, scope and
comparison identity.

## Troubleshooting

Use [the troubleshooting table](running.md#troubleshooting) for endpoint, storage and job errors.
