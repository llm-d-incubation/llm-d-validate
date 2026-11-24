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

`llmdv` sends real requests to a deployed [llm-d](https://github.com/llm-d/llm-d) stack, or any
OpenAI- or Anthropic-compatible one, and checks every answer.

- **Real requests, per layer.** It calls each layer you can reach (gateway, router, engine)
  through Chat, Completions, Messages and Responses, with streaming, tools, caching, headers and
  failure paths, and judges each layer on its own.
- **A profile declares the stack:** its ingresses, model and capabilities. Every verdict traces
  to a line in that file.
- **Every run is evidence:** results, each request and response, a replay script per failure,
  and an optional dashboard.

> [!NOTE]
> `llmdv` never deploys, patches, scales or restarts anything. Cluster access, where given, is
> read-only and enforced through a verb allowlist. Credentials are named by environment
> variable, never stored, and are redacted from every log and evidence file.

## Test an llm-d guide

Every [supported llm-d guide](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/README.md#supported-llm-d-guides) has ready profiles. They test the
guide's router (the EPP) directly: give llmdv the EPP's URL and run. No key is needed.

```sh
./install.sh && source .venv/bin/activate
export LLMDV_EPP_URL=<your EPP's URL>
llmdv verify --profile profiles/llm-d/optimized-baseline/vllm-qwen3-32b-parsers-standalone.yaml \
  --identity accelerator=gpu --identity provider=gke
```

Pick the combination and `--identity` flags for your deployment from the guide's folder. A gateway
in front of the router (Istio, kgateway, LiteLLM) is one more ingress in a short local file, and
llmdv then tests both layers:
[add your gateway](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/README.md#add-your-gateway). Requires Python 3.10 or newer.

## Test your own stack

A custom llm-d, or a stack with no llm-d at all, gets its own profile. Copy the closest example,
fill in its placeholders, and run it:

```sh
cp profiles/examples/example-gateway-only.yaml profiles/my-stack.local.yaml   # set base_url and model
export LLMDV_GATEWAY_KEY='<gateway key>'
llmdv profile validate profiles/my-stack.local.yaml    # offline check of the profile
llmdv plan --profile profiles/my-stack.local.yaml      # what would run and skip, offline
llmdv verify --profile profiles/my-stack.local.yaml
```

`*.local.yaml` profiles are gitignored, so your hosts never reach the repository.
[Your own stack](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/README.md#your-own-stack) covers the other examples, `extends` and
what to declare.

## Learn more

- [Getting started](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/getting-started.md): install, run, read the results, publish,
  compare, run unattended.
- [Profiles](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/README.md) and the [field reference](https://github.com/llm-d-incubation/llm-d-validate/blob/main/profiles/REFERENCE.md).
- [CLI reference](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/cli.md): every command, selecting cases, exit codes, output files.
- [Test suites](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/test-suites.md): what each suite checks, the profile key that enables it, and how to run one.
- [Architecture](https://github.com/llm-d-incubation/llm-d-validate/blob/main/docs/architecture.md): how the code is laid out.
- [Contributing](https://github.com/llm-d-incubation/llm-d-validate/blob/main/CONTRIBUTING.md). Questions and ideas are welcome in the
  [llm-d Slack](https://llm-d.ai/slack) and in this repository's issues.
