# Test suites

The suites llmdv runs, what each one checks, the profile key that turns it on, and what is still
missing. Each suite is a package under `src/llmdv/suites/`, and its name is also its pytest marker.

<p align="center">
  <img src="images/llmdv-coverage.svg" width="100%" alt="What llmdv tests: 19 suites in four groups (API surfaces, model behaviour, failure and resilience, contract and change), each run per layer: gateway, router, engine.">
</p>

## Run a suite

```sh
llmdv verify --profile <profile> -m openai_chat            # one suite
llmdv verify --profile <profile> -m "tools or streaming"   # several
llmdv verify --profile <profile> -k test_streaming_include_usage   # one case, by test name
llmdv plan --profile <profile> -m openai_chat              # preview: what would run and skip, offline
llmdv profile show <profile> --matrix                      # what this profile enables, per surface and ingress
```

A case whose `case_id` starts with another suite's name carries that suite's marker too. A profile
with a `selection:` runs only the cases it names; add `--all` to run a suite past it.
[cli.md](cli.md#selecting-what-runs) has every selector. Case counts are not listed here because
they drift: `llmdv plan` prints them, after parametrized variants (surface, ingress, option) have
multiplied them.

| Status | Meaning |
| --- | --- |
| built | implemented, with offline regressions that prove it can fail |
| built, blocked by the fleet | implemented, but needs a capability no committed profile declares |
| not built | no case exists yet; the gap has an issue |

Status describes llmdv and the profiles in this repository, not one night's run. A fleet that
serves a blocked capability runs those cases once its profile declares it;
`llmdv profile show <profile.yaml> --matrix` shows what runs and what skips for that profile.
An offline regression pass says nothing about a live fleet: run the cases against it (see
[getting-started.md](getting-started.md)).

## Suites

| Suite (marker) | What it checks | Enabled by | Status |
| --- | --- | --- | --- |
| Discovery (`discovery`) | `/v1/models` lists the model; unknown models get no fallback; retrieval failures retain JSON, text or empty-body evidence | any surface; `model_retrieval`, `model_info` | built |
| OpenAI Chat (`openai_chat`) | Basic, streaming, usage, reasoning, fields, choices, logprobs, answer budget | `chat_completions`; `chat_fields`, `chat_options`, `chat_reasoning_efforts`, `model.reasoning` | built |
| OpenAI Completions (`openai_completions`) | Prompt-based completions, fields, limits, logprobs | `completions`; `completions_fields` | built |
| OpenAI Responses (`openai_responses`) | Basic, streaming, tools, options; stored responses and cancellation, or their clean refusal on a stateless stack | `responses`; `responses_options`, `responses_features`, `stateful_responses`, `background_responses` or `stateless_responses`, `responses_compact`, `responses_input_tokens` | built |
| Anthropic Messages (`anthropic_messages`) | Messages, event order, thinking, streamed thinking and effort, count_tokens, content drops | `messages`; `messages_checks`, `messages_thinking`, `messages_efforts`, `count_tokens` | built |
| Tool calling (`tools`) | Tool calls, streamed arguments, parallel calls, results, loops; no declared call markup in any answer on a tool surface | `model.tools.parser`, `model.tools.auto_choice`, `model.tools.markup` | built |
| Streaming (`streaming`) | SSE framing, terminal events, `[DONE]` in the model's text, timing, disconnect then recover | streaming surfaces; `transport.stream_budget` | built |
| Field options (`field_options`) | Structured output, JSON mode (with reasoning beside the answer where it is on by default), reasoning usage, observable engine extension options, the effort marker the template renders for each requested effort | `structured_output`, `json_object`, `model.reasoning`, `reasoning_tokens`, `extension_options`, `effort_rendering` | built |
| Image input (`images`) | An image is read, not only accepted: streamed, 2.4 MB, two in order, counted by `count_tokens`; an internal image URL is refused | `vision`; `count_tokens`, `error_status.internal_image_url` | built |
| Long context (`long_context`) | Requests near and past the context window | `model.max_model_len` | built |
| Unhappy paths (`unhappy`) | Bad input, credentials and error envelopes get a clean 4xx | every surface; `error_status`, `unstructured_errors` | built |
| Transport (`transport`) | HTTP handling, encodings, TLS, exposure, retry signals | `ingresses[].transport` | built |
| Repository contract (`contract`) | Declared surfaces exist, undeclared absent; header dispositions | `surfaces`, `headers`, `header_probe` | built |
| Render (`render`) | Rendered token ids are stable and prefix-preserving | `*_render` surfaces; `prefix_stability` | built |
| Prefix cache (`cache`) | Realised prefix reuse across turns, salts, tools, clients | `cache_reuse`; `extension_options: [cache_salt]` | built |
| Change sentinels (`evolution`) | Forward compat, byte fidelity, prompt and render pins, contract drift | `forward_compat`, `byte_fidelity`, `prompt_pins`, `render_pins`, `contract_baseline` | built |
| Client scenarios (`scenarios`) | Client-shaped multi-request patterns: blank turns, cancel bursts, aliases | `scenario_checks`, `client_model_names`, `chat_quiet_efforts` | built |
| Real clients (`real_clients`) | Pinned Claude Code sessions through the declared chain | `clients[].binaries`, `client_sessions`, `client_reuse` | built |
| Fleet invariants (`fleet`) | Config reads, Prometheus bounds, forbidden log lines | `fleet.config`, `fleet.metrics`, `ingresses[].logs` | built |

Keys without a prefix live under `capabilities`, at the top level or per ingress; see
[profiles/REFERENCE.md](../profiles/REFERENCE.md#capabilities). Surfaces, transport, logs and
error declarations are per ingress, and an ingress can override model and capability
declarations ([#ingresses](../profiles/REFERENCE.md#ingresses)). An undeclared key skips its case
as `undeclared` without sending a request.

## Gaps

| Gap | Issue | Why open |
| --- | --- | --- |
| Chat field options | none | Reasoning efforts, structured output, response fields and `n>1` are built; the issue stays open for acceptance |
| `priority` and flow-control effects | none | Not built: needs a bounded load experiment; header consumption skips as `requires_load` |
| `chat_template_kwargs` effects, cache metric fallback | none | Not built: needs a template fixture and a metric contract |
| Other Responses request options | none | Partly built: metadata echo, output logprobs, reasoning effort and structured output exist |
| Valid `output_config.effort` reaching the model | none | Partly built: invalid-value rejection, an output-size effect probe (`messages_checks: [effort_effect]`) and `field_options.messages_effort_rendered` exist; the last needs prompt logprobs on Messages, which the LiteLLM gateway does not return |
| Render pins measured on a real build | none | Not yet measured: precise-prefix-cache-routing declares render surfaces; its first run supplies the pins |
| Sanitized production-shaped prefix fixtures, cross-ingress render parity | none | Not built |
| HTTP/2 framing | none | Not built: needs the `h2` package |
| 429, 5xx and errors after a stream opens | none | Needs load or fault injection |
| Generation continuing after the client disconnects | none | Needs engine metrics |
| Header forwarding seen by the backend | none | Built, needs the instrument: deploy `python -m llmdv.instruments.header_echo` behind the gateway |

Left out on purpose: unsafe HTTP methods on API paths, by policy. The real-client
`multi_file_fix` session runs model-written code, so run it in a disposable container.

Stop strings matching inside reasoning are not a fleet defect: keeping them dormant there
is not an established contract
([vllm#52393](https://github.com/vllm-project/vllm/issues/52393)). The reasoning-collision
case was removed rather than kept as a skip. Answer-stop cases remain; on a model that reasons
by default they turn reasoning off with the declared quiet effort (else `none`), so the stop
string can only meet the answer; they do not establish reasoning-channel isolation.

Why some of these checks exist, and the measured behaviours behind them, are in
[.agents/GROUND-TRUTH.md](../.agents/GROUND-TRUTH.md#why-some-checks-exist).

## See the cases

- What a profile runs and skips, and why, as a surface × ingress view:
  `llmdv profile show <profile.yaml> --matrix`.
- Every case `llmdv verify` would run, with per-suite counts and each skip's class:
  `llmdv plan --profile <profile> [-m <suite>]`. See
  [Selecting what runs](cli.md#selecting-what-runs).
- Every profile key: [profiles/REFERENCE.md](../profiles/REFERENCE.md).
