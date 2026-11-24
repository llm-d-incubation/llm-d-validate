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

<p align="center">
  <img src="https://raw.githubusercontent.com/llm-d-incubation/llm-d-validate/main/docs/images/llmdv-coverage.svg" width="100%" alt="What llmdv tests: 19 suites in four groups. API surfaces: OpenAI Chat Completions, OpenAI Completions, OpenAI Responses, Anthropic Messages, model discovery. Model behaviour: tool calling, streaming, structured output and options, image input, long context. Failure and resilience: error paths and envelopes, transport and TLS, client scenarios, real clients. Contract and change: declared surfaces and headers, render and token parity, prefix-cache reuse, change sentinels, fleet config, metrics and logs. Every case runs per layer: gateway, router, engine.">
</p>

> [!NOTE]
> `llmdv` is read-only: it sends requests and reads what comes back, and it never deploys,
> patches, scales or restarts anything. Cluster access, when you grant it, runs through a
> read-only verb allowlist. You name credentials by
> environment variable; `llmdv` keeps them out of every file and redacts them from every log.

## Test an llm-d guide

Every [supported llm-d guide](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/README.md#supported-llm-d-guides) comes with ready
profiles. They test the guide's router (the EPP) directly: give `llmdv` the EPP's URL and run. No
key is needed.

```sh
./install.sh && source .venv/bin/activate
export LLMDV_EPP_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

Pick the combination and `--identity` flags that match your deployment from the guide's folder.
Running a gateway in front of the router (Istio, kgateway, LiteLLM)? Add it as one more ingress in
a short local file, and `llmdv` tests both layers:
[add your gateway](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/README.md#add-your-gateway). `llmdv` runs on Python 3.10 or newer.

## Test your own stack

A custom llm-d, or a stack with no llm-d at all, gets a profile of its own. Copy the closest
example, fill in its placeholders, and run it:

```sh
cp profiles/examples/example-gateway-only.yaml profiles/my-stack.local.yaml   # set base_url and model
export LLMDV_GATEWAY_KEY='<gateway key>'
llmdv profile validate profiles/my-stack.local.yaml    # check the profile, offline
llmdv plan --profile profiles/my-stack.local.yaml      # preview what runs and what skips, offline
llmdv verify --profile profiles/my-stack.local.yaml    # run it against your stack
```

Git ignores `*.local.yaml` profiles, so your hosts stay out of the repository.
[Your own stack](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/README.md#your-own-stack) walks through the other examples, `extends`
and what to declare.

## Learn more

- [Getting started](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/getting-started.md): install, run, read the results, publish,
  compare and run unattended.
- [Profiles](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/README.md) and the [field reference](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/REFERENCE.md).
- [CLI reference](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/cli.md): every command, selecting cases, exit codes and output files.
- [Test suites](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/test-suites.md): what each suite checks, the profile key that turns it on,
  and how to run one.
- [Architecture](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/architecture.md): how the code fits together.
- [Contributing](https://github.com/llm-d-incubation/llm-d-validate/blob/main/CONTRIBUTING.md): we welcome questions and ideas in the
  [llm-d Slack](https://llm-d.ai/slack) and in this repository's issues.
