# suites

The functional tests. Each package is one suite, and its name is also its pytest marker
(`llmdv verify -m <suite>`). Counts and status are in [docs/coverage.md](../../../docs/coverage.md).

| Suite | What it checks | Enabled by |
| --- | --- | --- |
| [`anthropic_messages`](anthropic_messages/) | Messages, event order, thinking, streamed thinking and effort, count_tokens, content drops | `messages`; `messages_checks`, `messages_thinking`, `messages_efforts`, `count_tokens` |
| [`cache`](cache/) | Realised prefix reuse across turns, salts, tools, clients | `cache_reuse`; `vllm_options: [cache_salt]` |
| [`contract`](contract/) | Declared surfaces exist, undeclared absent; header dispositions | `surfaces`, `headers`, `header_probe` |
| [`discovery`](discovery/) | `/v1/models` lists the model; unknown models get no fallback; retrieval failures retain JSON, text or empty-body evidence | any surface; `model_retrieval`, `model_info` |
| [`evolution`](evolution/) | Forward compat, byte fidelity, prompt and render pins, contract drift | `forward_compat`, `byte_fidelity`, `prompt_pins`, `render_pins`, `contract_baseline` |
| [`field_options`](field_options/) | Structured output, JSON mode (with reasoning beside the answer where it is on by default), reasoning usage, observable vLLM options, the effort marker the template renders for each requested effort | `structured_output`, `json_object`, `model.reasoning`, `reasoning_tokens`, `vllm_options`, `effort_rendering` |
| [`fleet`](fleet/) | Config reads, Prometheus bounds, forbidden log lines | `fleet.config`, `fleet.metrics`, `ingresses[].logs` |
| [`images`](images/) | An image is read, not only accepted: streamed, 2.4 MB, two in order, counted by `count_tokens`; an internal image URL is refused | `vision`; `count_tokens`, `error_status.internal_image_url` |
| [`long_context`](long_context/) | Requests near and past the context window | `model.max_model_len` |
| [`openai_chat`](openai_chat/) | Basic, streaming, usage, reasoning, fields, choices, logprobs, answer budget | `chat_completions`; `chat_fields`, `chat_options`, `chat_reasoning_efforts`, `model.reasoning` |
| [`openai_completions`](openai_completions/) | Prompt-based completions, fields, limits, logprobs | `completions`; `completions_fields` |
| [`openai_responses`](openai_responses/) | Basic, streaming, tools, stored state, cancellation, options | `responses`; `responses_options`, `responses_features`, `stateful_responses` |
| [`real_clients`](real_clients/) | Pinned Claude Code sessions through the declared chain | `clients[].binaries`, `client_sessions`, `client_reuse` |
| [`render`](render/) | Rendered token ids are stable and prefix-preserving | `*_render` surfaces; `prefix_stability` |
| [`scenarios`](scenarios/) | Client-shaped multi-request patterns: blank turns, cancel bursts, aliases | `scenario_checks`, `client_model_names`, `chat_quiet_efforts` |
| [`streaming`](streaming/) | SSE framing, terminal events, `[DONE]` in the model's text, timing, disconnect then recover | streaming surfaces; `transport.stream_budget` |
| [`tools`](tools/) | Tool calls, streamed arguments, parallel calls, results, loops; no declared call markup in any answer on a tool surface | `model.tools.parser`, `model.tools.auto_choice`, `model.tools.markup` |
| [`transport`](transport/) | HTTP handling, encodings, TLS, exposure, retry signals | `ingresses[].transport` |
| [`unhappy`](unhappy/) | Bad input, credentials and error envelopes get a clean 4xx | every surface; `error_status`, `unstructured_errors` |

`conftest.py` holds fixtures every suite shares.
