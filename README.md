<h1 align="center">llm-d-validate</h1>

<p align="center">
  <b>Roll out <a href="https://github.com/llm-d/llm-d">llm-d</a> with confidence.</b>
  <br><br>
  Demonstrate that your gateway, router, and engine behave as their APIs define.<br>
  Validation for deployed llm-d model serving stacks.
</p>

<p align="center">
  <a href="https://github.com/llm-d-incubation/llm-d-validate/actions/workflows/offline-tests.yaml?query=branch%3Amain"><img alt="offline tests" src="https://github.com/llm-d-incubation/llm-d-validate/actions/workflows/offline-tests.yaml/badge.svg?branch=main"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3776AB">
  <a href="https://github.com/llm-d-incubation/llm-d-validate/blob/main/LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-blue"></a>
</p>

<p align="center">
  <a href="https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/getting-started.md">Getting started</a> ·
  <a href="https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/README.md">Profiles</a> ·
  <a href="https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/cli.md">CLI reference</a> ·
  <a href="https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/test-suites.md">Test suites</a> ·
  <a href="https://github.com/llm-d-incubation/llm-d-validate/blob/main/CONTRIBUTING.md">Contributing</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/llm-d-incubation/llm-d-validate/main/docs/images/llmdv-overview.gif" width="100%" alt="Animated overview: a profile defines the test plan; llmdv sends real requests to the gateway (end to end), the llm-d router (router and engine) and the engine alone; each layer gets a verdict; the run's evidence (results, exchanges, repro scripts, a dashboard) is bundled, published to object storage and sent as a notification. Runs launch from a workstation, CI or a cluster job.">
</p>

## How it works

`llmdv` sends real requests through every layer of a deployed [llm-d](https://github.com/llm-d/llm-d)
stack, or any OpenAI- or Anthropic-compatible one. It checks each answer against the API,
pinpoints the layer that breaks, and flags what regressed since the last run.

- **It tests every layer on its own.** `llmdv` calls each layer you can reach (gateway, router,
  engine) through Chat, Completions, Messages and Responses, and exercises streaming, tools,
  caching, headers and failure paths, so you see exactly which layer holds and which one breaks.
- **A profile defines correct.** It describes your stack: its ingresses, its model and what each
  layer serves. Every verdict traces back to a line in that file.
- **It catches regressions.** Compare runs, and `llmdv` flags every case that changed and every
  build that changed under it, so a regression never hides behind a passing health check.
- **Every run produces evidence.** You get the results, every request and response, a replay
  script for each failure, and an optional dashboard to explore the whole run.

> [!NOTE]
> `llmdv` is read-only: it sends requests and reads what comes back, and it never deploys,
> patches, scales or restarts anything. Cluster access, when you grant it, runs through a
> read-only verb allowlist. You name credentials by
> environment variable; `llmdv` keeps them out of every file and redacts them from every log.

## Test a deployed llm-d guide

Every llm-d guide at [Tier 3](https://github.com/llm-d/llm-d/blob/main/guides/GUIDES-POLICY.md#5-production-readiness-tiers) (Validated: run end to end every
night) includes a ready-to-use
[validation profile](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/README.md#llm-d-guide-validation-profiles),
written against llm-d `9ca8ff0` and not yet run against a real engine.
After deploying a guide, use its profile to verify that your deployment behaves as specified by
the guide and its APIs. By default, the profiles target the guide's router (the EPP), so give
`llmdv` the EPP's URL.

```sh
./install.sh && source .venv/bin/activate
export LLMDV_EPP_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

Pick the profile and `--identity` flags that match your configuration from the guide's folder.

To include a gateway (Istio, agentgateway, LiteLLM) or engine in your tests, add it as another
ingress, and `llmdv` tests each layer on its own. See
[include a gateway or engine](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/README.md#include-a-gateway-or-engine).

## Test your own stack

For other model-serving stacks, whether or not they use llm-d, start from an example profile.
Copy the closest one, fill in its placeholders, and run it:

```sh
cp profiles/examples/example-gateway-only.yaml profiles/my-stack.local.yaml   # set base_url and model
export LLMDV_GATEWAY_KEY='<gateway key>'
llmdv profile validate profiles/my-stack.local.yaml    # check the profile, offline
llmdv plan --profile profiles/my-stack.local.yaml      # preview what runs and what skips, offline
llmdv verify --profile profiles/my-stack.local.yaml    # run it against your stack
```

Git ignores `*.local.yaml` profiles, so your hosts stay out of the repository.
[Writing a profile](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/writing-profiles.md) walks through the other
examples, `extends` and what to declare.

## What it tests

19 suites and growing, from every API surface to failure paths and fleet invariants. Run any
one on its own with `-m <marker>`; [test suites](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/test-suites.md) describes each one and
what turns it on.

<p align="center">
  <img src="https://raw.githubusercontent.com/llm-d-incubation/llm-d-validate/main/docs/images/llmdv-coverage.svg" width="100%" alt="What llmdv tests: 19 suites and growing, in four groups, each shown with the -m marker that runs it. API surfaces: OpenAI Chat Completions, OpenAI Completions, OpenAI Responses, Anthropic Messages, model discovery. Model behaviour: tool calling, streaming, structured output and options, image input, long context. Failure and resilience: error paths and envelopes, transport and TLS, client scenarios, real clients. Contract and change: declared surfaces and headers, render and token parity, prefix-cache reuse, change sentinels, fleet config, metrics and logs. Every case runs per layer: gateway, router, engine.">
</p>

## Learn more

- [Getting started](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/getting-started.md): install, run, read the results, publish,
  compare and run unattended.
- [Profiles](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/README.md), [writing a profile](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/writing-profiles.md) and the
  [field reference](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/REFERENCE.md).
- [CLI reference](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/cli.md): every command, selecting cases, exit codes and output files.
- [Test suites](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/test-suites.md): what each suite checks, the profile key that turns it on,
  and how to run one.
- [Architecture](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/architecture.md): how the code fits together.
- [Writing tests](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/writing-tests.md): add a case or a suite, prove it can fail, and run it.
- [Contributing](https://github.com/llm-d-incubation/llm-d-validate/blob/main/CONTRIBUTING.md): we welcome questions and ideas in the
  [llm-d Slack](https://llm-d.ai/slack) and in this repository's issues.
