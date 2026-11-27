# Getting started

Run a small check against a deployed endpoint, read its evidence, then widen coverage. This
walkthrough uses a local machine; [cluster jobs](../deploy/README.md) use the same profiles and
produce the same kind of run directory.

## Install

For a local Python installation, you need Python 3.10+ and Git. Use the released package from
[PyPI](https://pypi.org/project/llm-d-validate/) with profiles from the matching repository tag.
The package installs the CLI and test library; profiles and their shared `extends` parts come
from the checkout. Keep the directory structure intact rather than downloading a single YAML.

| How you want to run | Installation |
| --- | --- |
| Local released tool | Clone the release tag and install from PyPI, as below |
| Released container | Clone the same tag for profiles and use the [matching image](../deploy/README.md#use-the-released-image-locally); host Python is not required |
| Develop or run source changes | Follow [source installation and builds](../CONTRIBUTING.md#development-and-testing) |

These examples pair release `v0.1.3` with package version `0.1.3`:

```sh
git clone --branch v0.1.3 https://github.com/llm-d-incubation/llm-d-validate.git
cd llm-d-validate
python3 -m venv .venv
source .venv/bin/activate
python -m pip install 'llm-d-validate==0.1.3'
llmdv --version
```

Check `python3 --version` first. If it is older than 3.10, as macOS's built-in `python3` is,
create the environment with a newer interpreter, for example `python3.12 -m venv .venv`. With
an older one, pip reports `No matching distribution found for llm-d-validate==0.1.3` instead of
naming the Python version.

Run the remaining examples from the checkout root with the virtual environment active.
The release checkout is detached at its tag; use gitignored `*.local.yaml` overlays for local
profile changes. Editing a profile takes effect immediately; editing `src/` does not change the
installed PyPI package. Use the source installation when developing cases or library code.

Python installation does not install external executables: cluster reads need `kubectl` or
`oc`, and real-client cases need the binary/version declared by their profile. Smoke does not
run those real-client cases. The released image includes the pinned Claude Code client and
both object-storage backends. See [storage extras](running.md#publish-a-run) when publishing
from a PyPI installation.

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

Verification runs cases sequentially. Individual cases can send concurrent requests, such as
a cancellation burst. Do not add pytest's `-n`, and avoid competing live runs against the same
endpoint. Keep a laptop awake until the run finishes.

## Read the results

The command prints a new directory under `runs/`. Each invocation creates its own child, so
reusing `--local-output ROOT` preserves earlier runs. Use the printed child path below:

```sh
export RUN='<paste the full directory printed by verify>'
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

Guide profiles select checks related to the guide's configuration and promises. They provide
focused feedback and a starting point for production validation. A routine guide check does not
need every unrelated library case.

After smoke, preview and run the profile's default:

```sh
llmdv plan --profile "$P"
llmdv verify --profile "$P" --analyze
```

Grow coverage according to what your deployment needs:

| Your purpose | Next step |
| --- | --- |
| Check a guide's intended serving behavior | Use its default and read the guide's coverage boundaries |
| Adopt another API, client or model feature | Declare the intended capability and add the relevant cases to a named test set |
| Qualify an engine/model upgrade or prepare for production | Review all relevant APIs, error paths, clients and deployment-specific risks; preview broader suites |
| Test a contract the library does not cover | [Add a case](writing-tests.md), prove it against a fake defect, and include it in your profile |

For a newly written profile, check its surface and header declarations with
`llmdv verify --profile "$P" --full-suite -m contract` before relying on broader results.

Users can build extensive coverage through profile declarations, named test sets and additional
cases. `--full-suite` selects every existing library case before filters and applicability gates;
it is not a guarantee of complete API or production coverage. Review skipped checks and
[known library gaps](test-suites.md#gaps). Load, routing internals and other operational claims
may need instruments or experiments beyond a functional run.

[Selection and coverage](cli.md#test-sets-and-coverage) explains how to combine suites and
restrict ingresses. [Writing a profile](writing-profiles.md) explains how to declare expected
behavior without weakening it to obtain a pass. For maintainers, the
[broader validation assignments](../profiles/llm-d/README.md#for-maintainers) explain how
shared configurations receive full-library checks alongside focused guide validation.

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

## Upgrade a release installation

Check [releases](https://github.com/llm-d-incubation/llm-d-validate/releases) and choose a package,
profile checkout and image from the same release. For an existing PyPI installation upgrading
to the version used in this guide, activate its environment and run:

```sh
python -m pip install --upgrade 'llm-d-validate==0.1.3'
llmdv --version
```

If you use a storage extra, include it in the requirement as shown in
[storage setup](running.md#publish-a-run). Obtain the matching tagged profiles in a separate
checkout so existing local work is preserved. Review and adapt local overlays, then run
`llmdv profile validate "$P"` and `llmdv plan --profile "$P" --test-set smoke` before live checks.
For containers, update the image tag and matching profile checkout together. Keep existing run
directories for comparison; installing a release does not rerun or replace their evidence.
