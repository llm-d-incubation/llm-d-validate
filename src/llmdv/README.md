# llmdv

The package behind the `llmdv` command. [docs/architecture.md](../../docs/architecture.md)
explains how the pieces fit.

| Path | What it holds |
| --- | --- |
| [`cli/`](cli/) | one module per subcommand, shared flags, output rendering and exit codes |
| [`profile/`](profile/) | the profile schema, loader and offline validation |
| [`ingress/`](ingress/) | per-layer adapters: vLLM, router, LiteLLM |
| [`surfaces/`](surfaces/) | per-API wire shapes: Chat, Completions, Responses, Messages, render |
| [`clients/`](clients/) | bounded real-client invocation and the native-request relay |
| [`http/`](http/) | the recording client, SSE reader, redaction and curl generation |
| [`assertions/`](assertions/) | shared assertions, so each lesson is encoded once |
| [`evidence/`](evidence/) | what backs a verdict: checks, bundles, reproduction, outcome, alerts, dashboard |
| [`compare/`](compare/) | reading archived runs back and diffing them |
| [`collect/`](collect/) | each layer's own logs, through the read-only adapter |
| [`fleet/`](fleet/) | configuration, metrics and log invariants; the read-only metrics client |
| [`instruments/`](instruments/) | operator-run helpers, such as a header-echo backend |
| [`output/`](output/) | where a run's artifacts go: local, `file://` and `s3://` |
| [`results/`](results/) | the structured result record |
| [`log/`](log/) | the logger tree |
| [`suites/`](suites/) | the functional tests themselves |
| `plugin.py` | the pytest plugin that ties it together |
| `kube.py` | read-only kubectl/oc adapter with a verb allowlist |
| `preflight.py` | checks the environment is ready, before anything is tested |
| `notify.py` | the Slack notification for a finished run |
