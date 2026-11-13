# Functional test coverage

This page answers three questions: does llmdv test X, what is missing or blocked, and which
profile key turns a check on. Each row is one suite package under `src/llmdv/suites/`; the
package name is also its pytest marker. Case counts are the distinct `case_id` declarations in
that package. Parametrized variants (surface, ingress, option) multiply them at run time.

| Status | Meaning |
| --- | --- |
| built | implemented, with offline regressions that prove it can fail |
| built, blocked by the fleet | implemented, but needs a capability that no committed profile declares, such as a render route |
| not built | no case exists yet; the gap has an issue |

Status describes llmdv and the profiles in this repository, not one night's run. A fleet that
serves a blocked capability runs those cases once its profile declares it;
`llmdv profile show <profile.yaml> --matrix` shows what runs and what skips for that profile.
An offline regression pass says nothing about a live fleet. For what a fleet actually does, run
the cases against it (see [END-TO-END.md](END-TO-END.md)).

## Coverage

| Area (marker) | Cases | What it checks | Enabled by | Status |
| --- | ---: | --- | --- | --- |
| Discovery (`discovery`) | 7 | `/v1/models` lists the model; unknown models get no fallback; retrieval failures retain JSON, text or empty-body evidence | any surface; `model_retrieval`, `model_info` | built |
| OpenAI Chat (`openai_chat`) | 42 | Basic, streaming, usage, reasoning, fields, choices, logprobs, answer budget | `chat_completions`; `chat_fields`, `chat_options`, `chat_reasoning_efforts`, `model.reasoning` | built |
| OpenAI Completions (`openai_completions`) | 17 | Prompt-based completions, fields, limits, logprobs | `completions`; `completions_fields` | built |
| OpenAI Responses (`openai_responses`) | 27 | Basic, streaming, tools, stored state, cancellation, options | `responses`; `responses_options`, `responses_features`, `stateful_responses` | built |
| Anthropic Messages (`anthropic_messages`) | 37 | Messages, event order, thinking, streamed thinking and effort, count_tokens, content drops | `messages`; `messages_checks`, `messages_thinking`, `messages_efforts`, `count_tokens` | built |
| Tool calling (`tools`) | 63 | Tool calls, streamed arguments, parallel calls, results, loops; no declared call markup in any answer on a tool surface | `model.tools.parser`, `model.tools.auto_choice`, `model.tools.markup` | built |
| Streaming (`streaming`) | 13 | SSE framing, terminal events, `[DONE]` in the model's text, timing, disconnect then recover | streaming surfaces; `transport.stream_budget` | built |
| Field options (`field_options`) | 8 | Structured output, JSON mode (with reasoning beside the answer where it is on by default), reasoning usage, observable vLLM options, the effort marker the template renders for each requested effort | `structured_output`, `json_object`, `model.reasoning`, `reasoning_tokens`, `vllm_options`, `effort_rendering` | built |
| Image input (`images`) | 8 | An image is read, not only accepted: streamed, 2.4 MB, two in order, counted by `count_tokens`; an internal image URL is refused | `vision`; `count_tokens`, `error_status.internal_image_url` | built |
| Long context (`long_context`) | 3 | Requests near and past the context window | `model.max_model_len` | built |
| Unhappy paths (`unhappy`) | 28 | Bad input, credentials and error envelopes get a clean 4xx | every surface; `error_status`, `unstructured_errors` | built |
| Transport (`transport`) | 28 | HTTP handling, encodings, TLS, exposure, retry signals | `ingresses[].transport` | built |
| Repository contract (`contract`) | 4 | Declared surfaces exist, undeclared absent; header dispositions | `surfaces`, `headers`, `header_probe` | built |
| Render (`render`) | 3 | Rendered token ids are stable and prefix-preserving | `*_render` surfaces; `prefix_stability` | built, blocked by the fleet |
| Prefix cache (`cache`) | 33 | Realised prefix reuse across turns, salts, tools, clients | `cache_reuse`; `vllm_options: [cache_salt]` | built |
| Change sentinels (`evolution`) | 22 | Forward compat, byte fidelity, prompt and render pins, contract drift | `forward_compat`, `byte_fidelity`, `prompt_pins`, `render_pins`, `contract_baseline` | built (render pins blocked by the fleet) |
| Client scenarios (`scenarios`) | 6 | Client-shaped multi-request patterns: blank turns, cancel bursts, aliases | `scenario_checks`, `client_model_names`, `chat_quiet_efforts` | built |
| Real clients (`real_clients`) | 9 | Pinned Claude Code sessions through the declared chain | `clients[].binaries`, `client_sessions`, `client_reuse` | built |
| Fleet invariants (`fleet`) | 10 | Config reads, Prometheus bounds, forbidden log lines | `fleet.config`, `fleet.metrics`, `ingresses[].logs` | built |
| **Total** | **364** | | | |

Keys without a prefix live under `capabilities`, either at the top level or per ingress; see
[profiles/REFERENCE.md](profiles/REFERENCE.md#capabilities). Surfaces and transport, logs and
error declarations are per ingress ([#ingresses](profiles/REFERENCE.md#ingresses)). An
undeclared key skips its case as `undeclared` without sending a request. Ingresses can override
model and capability declarations; see [#ingresses](profiles/REFERENCE.md#ingresses).

## Gaps

| Gap | Issue | Why open |
| --- | --- | --- |
| Chat field options | none | Reasoning efforts, structured output, response fields and `n>1` are built; the issue stays open for acceptance |
| `priority` and flow-control effects | none | Not built: needs a bounded load experiment; header consumption skips as `requires_load` |
| `chat_template_kwargs` effects, cache metric fallback | none | Not built: needs a template fixture and a metric contract |
| Other Responses request options | none | Partly built: metadata echo, output logprobs, reasoning effort and structured output exist |
| Valid `output_config.effort` reaching the model | none | Partly built: invalid-value rejection, an output-size effect probe (`messages_checks: [effort_effect]`) and `field_options.messages_effort_rendered` exist; the last needs prompt logprobs on Messages, which the LiteLLM gateway does not return |
| Render stability and synthetic prefix fixtures | none | Blocked by the fleet: needs a render route that answers 200 with `token_ids` |
| Sanitized production-shaped prefix fixtures, cross-ingress render parity | none | Not built |
| HTTP/2 framing | none | Not built: needs the `h2` package |
| 429, 5xx and errors after a stream opens | none | Needs load or fault injection |
| Generation continuing after the client disconnects | none | Needs engine metrics |
| Header forwarding seen by the backend | none | Built, needs the instrument: deploy `python -m llmdv.instruments.header_echo` behind the gateway |

Left out on purpose: unsafe HTTP methods on API paths, by policy. The real-client
`multi_file_fix` session runs model-written code, so run it in a disposable container.

Stop strings matching inside reasoning are not a fleet defect: keeping them dormant there
is not an established contract (#272). The reasoning-collision case was removed rather than
retained as a skip. Answer-stop cases remain, and on a model that reasons by default they turn
reasoning off with the declared quiet effort (else `none`), so the stop string can only meet the
answer; they do not establish reasoning-channel isolation.

## Why some checks exist

**A 200 must be well-formed, not just successful.** Some defects hide behind correct responses.
A reasoning model given a small budget can spend it all thinking and stop at `length` with no
answer; `length` is legal and reasoning counts as output, so the smoke cases pass it. The
`answer_budget` cases fail that combination. A client that puts a per-request value at the head
of its prompt destroys prefix reuse while every response is correct, so the `cache` and
`real_clients` cases assert realised reuse across turns, not a status.

**What the client sent must reach the model.** A gateway can drop or rewrite part of a
conversation, such as a `role: "system"` entry inside `messages` or earlier thinking blocks, and
still return a clean 200. The `messages_checks` cases assert on what reached the model, usually
through input token counts, not on the response shape. The `images` cases do the same for a
picture: each draws four random colour stripes and requires the answer to name them in order,
which a layer that drops the image or inlines its base64 as text cannot do. They also run in a
tool result (`tools`), a Claude Code session (`real_clients`) and a prefix repeat (`cache`).

**Prefix stability needs two requests, not load.** Changing a volatile value (a billing line,
an inline reminder) must not change the reusable prompt prefix. That is visible in two render
calls, which makes it a functional test. Cache-hit rates and router scoring accuracy belong to
system tests.

**Change sentinels go red the night a change lands.** Each `evolution` case pins one declared
behaviour: how an unknown field, block or header is treated; that integers above 2^53, key order
and escapes survive every hop; the input token count or render digest of a fixed conversation;
and the key paths and JSON types of responses, streams and errors. A template edit, tokenizer
swap or re-encoding hop changes these while every response stays correct.

**Two allowed answers, one forbidden.** Where the spec allows two answers, either passes unless
the profile pins one under `error_status`. A 5xx, a hang or a dropped connection never passes,
because SDKs retry them.

**Argument schemas are enforced only where the fleet promised it.** A tool sent without
`strict: true` has unconstrained arguments on vLLM (below `--tool-strict-level parameter`) and on
OpenAI alike, so the tools cases hold a call's arguments to the schema only for strict tools, or
for every tool when the profile declares `tools.strict_level: parameter`. Elsewhere a mismatch
is recorded (`tool_call_arguments_unconstrained`) as evidence. `tools.strict`,
`tools.schema_features` and `tools.args_types` send strict tools, so they still verify the
schema is honoured (#270).

**Call markup is a leak wherever it appears.** Every generation on a tool surface runs
`tool_markup_in_answer` as part of the shared contract when the profile declares
`model.tools.markup`, not only the cases that expect a call. A parser that finds no call it can
parse (a truncated call, an unhandled `tool_choice`, a name outside its charset) can hand the raw
framing back as the answer: a case expecting a call then fails as a missing call, and one that
judges only token counts passes. `tools.result_quotes_markup` asks the answer to quote the tags,
so there the markup found is recorded and not judged.

**A schema constrains the answer, not the reasoning.** Where the profile declares reasoning on
by default through a reasoning parser, the JSON-schema and JSON-mode cases
(`field_options.structured_output`, `field_options.structured_output_stream`,
`field_options.json_object`, `evolution.key_order`, `openai_chat.structured_key_order`,
`openai_chat.sdk_parse_structured[json]`, `tools.result_quotes_markup`, the answer of
`tools.with_output_format`) also require reasoning text or a positive reasoning token count
beside the answer (`structured_output_reasoning`). A grammar applied from the first token leaves
valid JSON with no reasoning channel, and the model writes its reasoning into the JSON's
strings. It is judged after the schema, so a broken answer still fails as one.
`scenarios.structured_client` turns reasoning off, so it is not judged there.

**`max_tool_calls` limits built-in tools only.** `tools.max_tool_calls` checks that a function-tool
request carrying this field returns valid calls or a clean 4xx refusal; multiple function calls
are valid even with `max_tool_calls: 1`. The Responses feature probe separately checks the
declared acceptance and echo. Enforcement of the cap on built-in tools is not covered.

**Fleet checks read, never write.** Configuration reads use kubectl `get` only; a missing tool
or a forbidden read skips as `requires_instrumentation`. Only `metrics.header_label` sends model
traffic.

**Layer attribution is a hypothesis.** The attribution ladder is built from the ingresses the
profile declares, and every result records which endpoint served it, so a per-replica
difference is not blamed on a gateway. A single-ingress run is complete but cannot compare
layers.

## See the cases

- A profile's surface × ingress view, including what will skip and why:
  `llmdv profile show <profile.yaml> --matrix`.
- The per-case list for one area:
  `pytest --collect-only -q -m <marker>`, where the marker is the suite package name. See
  [Selecting what runs](README.md#selecting-what-runs).
- Every profile key: [profiles/REFERENCE.md](profiles/REFERENCE.md).

