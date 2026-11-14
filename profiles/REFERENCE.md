# Profile reference: `llmdv.profile/1`

Every key a profile accepts, block by block. For how to write a profile, start with the [guide](README.md). For the `fleet` block, see [FLEET.md](FLEET.md).

Conventions:

- An undeclared capability makes its cases skip as `undeclared`; it never fails them.
- Booleans and `model` values are parsed strictly: `"false"` is not `false`, `max_model_len: "8192"` is an error, and a numeric model id must be quoted.
- A list in an enumerated field rejects unknown values. The capability vocabularies (efforts,
  thinking, checks, sessions, options, repeats, `byte_fidelity`) also reject repeats.
- Unknown keys in a per-ingress override, in `cache_reuse` and in `transport` are errors.

`tests/test_profile_reference.py` checks that every table below matches the schema in `src/llmdv/profile/schema.py`.

## Top-level keys

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `schema` | string | `llmdv.profile/1` | Schema version. Any other value is an error. |
| `name` | string | the file name, without `.yaml` | Profile name, shown in output and `results.jsonl`. |
| `vantage` | `external` \| `in-cluster` | `external` | Where the client ran, relative to the cluster. `external` covers a laptop, a CI runner or a jump host; what drove the run goes in `--label`. `laptop` is read as `external`. |
| `environment` | mapping | none | Cluster coordinates for read-only lookups. See [environment](#environment). |
| `model` | mapping | **required** | The model the fleet serves. See [model](#model). |
| `ingresses` | list | **required**, at least one | One entry per layer you can address. See [ingresses](#ingresses). |
| `capabilities` | mapping | none | Optional features, inherited by every ingress. See [capabilities](#capabilities). |
| `headers` | mapping | none | What each layer does to a header. See [headers](#headers). |
| `clients` | list | none | Real consumers and the surfaces they need. See [clients](#clients). |
| `fleet_policy` | `dedicated` \| `shared` | `dedicated` | `shared` means other people depend on this fleet: cases marked `disruptive` skip as `excluded_by_policy`. Cheap well-formed refusals (a bad token limit, an unknown path, a bad credential) still run. Declared, never inferred. |
| `fleet` | mapping | none | Configuration and metrics rules read from the cluster. See [FLEET.md](FLEET.md). |
| `known_failures` | list | none | Failures already filed and traced to a pending fix. Labels, never excuses. See [known_failures](#known_failures). |

## environment

Optional. Without it, llmdv works against bare URLs and never contacts a cluster. All cluster access is read-only: a verb allowlist leaves no path to `apply`, `delete`, `scale` or `exec`.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `cluster` | string | none | Informational; shown in the banner. |
| `context` | string | none | kubeconfig context. `--context` overrides it. |
| `namespace` | string | none | Default namespace for lookups and log collection. |
| `kubeconfig` | path | none | `~` is expanded. `--kubeconfig` overrides it. |
| `metrics` | mapping | none | A read-only Prometheus-compatible API for the `fleet.metrics` rules. See [FLEET.md](FLEET.md#metrics). |

## model

The profile-wide model. Each ingress inherits it and may override any field (see [Per-ingress overrides](#per-ingress-overrides)).

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `public_id` | string | **required** | The model id clients request, as `/v1/models` advertises it at the ingress. |
| `aliases` | list of string | `[]` | Further ids that must also be advertised. |
| `max_model_len` | int or `null` | none | Context length. Without it, the context-overflow case skips as `undeclared`. |
| `answer_max_tokens` | positive int or `null` | none | Opts into the Chat and Messages answer-budget cases, using exactly this output cap in the default reasoning mode. Other cases' budgets do not change. |
| `reasoning` | mapping | see below | How reasoning text reaches the client. |
| `tools` | mapping | see below | Tool-calling support. |

### model.reasoning

Declared because asserting the wrong shape fails a healthy fleet. Served without `--reasoning-parser`, vLLM puts reasoning in a literal `<think>` block inside `content` and leaves the separate field null.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `parser` | string or `null` | `null` | The engine's reasoning parser; `null` means none is configured. |
| `field` | string or `null` | `null` | Response field carrying reasoning, such as `reasoning_content`. |
| `inline_tag` | string or `null` | `null` | Tag wrapping inline reasoning when there is no parser, such as `think`. |
| `default_on` | bool | `false` | The model reasons without being asked. Raises smoke-test budgets. With a `parser`, the structured-output cases also require reasoning beside the schema answer (`structured_output_reasoning`). |

### model.tools

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `parser` | string or `null` | `null` | Tool-call parser. `null` makes the tool cases skip as `undeclared`. A declared parser enables the tool loops and the tools suite on every surface with tools; results are synthetic and nothing is executed. |
| `auto_choice` | bool | `false` | Automatic tool choice is enabled. Cases that leave the choice to the model need it. |
| `forced_finish_reason` | `tool_calls` \| `stop` | `tool_calls` | Chat `finish_reason` for a request that forces a named function. OpenAI answers `stop`, so neither is a defect. |
| `markup` | list of string | `[]` | Raw call markup the model writes before the parser runs. Answer text containing an entry fails any case on a surface with tools (`tool_markup_in_answer`, part of the shared generation contract); `tools.result_quotes_markup` records it unjudged, since it asks for the tags. Empty strings are refused. |
| `strict_level` | `auto` \| `function` \| `parameter` | `auto` | The server's `--tool-strict-level` (vLLM). Under `auto` and `function` a tool's argument schema is enforced only when that tool is sent with `strict: true`, so the tools cases hold arguments to the schema only for strict tools; `parameter` pins every tool's schema and every case holds every call to it. A mismatch on a non-strict tool is recorded as `tool_call_arguments_unconstrained`, not failed. |

## ingresses

One entry per addressable layer. One ingress is a complete profile; each further layer adds a column, and comparison sections appear once there is more than one.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `id` | string | none | Short, unique name. It becomes the pytest parameter id, so `-k <id>` selects one column. |
| `kind` | `vllm` \| `llmd_router` \| `litellm` | `vllm` | Selects the adapter. In reports it also orders the layers outermost first: `litellm`, `llmd_router`, `vllm`. |
| `base_url` | string | **required** unless `base_url_env` is set | Must start with `http://` or `https://`. |
| `base_url_env` | string | none | Environment variable holding the URL. A committed profile uses it so no hostname enters the repository. Declaring both is an error; an unset variable fails preflight. |
| `auth` | mapping | `{type: none}` | See [ingresses[].auth](#ingressesauth). |
| `health_path` | string | `/health` | Used by preflight. A non-2xx still counts as reachable. |
| `surfaces` | list | `[]` | Surfaces this ingress serves. Anything absent skips as `undeclared`. See [Surface names](#surface-names). |
| `model` | mapping | inherited | Partial override of the top-level `model`. |
| `capabilities` | mapping | inherited | Partial override of the top-level `capabilities`. |
| `timeouts` | mapping | see below | Connection, request and stream timeouts. |
| `error_status` | mapping | `{}` | Error class to the statuses acceptable at this layer. Absent or empty means any 4xx; a scalar is a one-element list. A 2xx means the layer does not enforce that class. See [Error classes](#error-classes). |
| `unstructured_errors` | list | `[]` | Error classes this layer answers without a parseable error envelope. The case then requires the envelope to be absent, so the declaration fails if the layer is fixed. |
| `rate_limited` | bool | `false` | This layer may refuse us for exceeding our own quota. A `429` on a generation then skips as `throttled`, with rate-limit headers recorded. Without it, a `429` fails. No other status is excused. |
| `setup` | mapping | see below | What must be true to reach this layer from this vantage point. |
| `logs` | list | the `setup.requires` targets | Where this layer's logs live. |
| `tls` | mapping | `{verify: true}` | See [ingresses[].tls](#ingressestls). |
| `transport` | mapping | `{}` | HTTP, SSE and exposure promises. See [ingresses[].transport](#ingressestransport). |

### ingresses[].auth

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | `none` \| `bearer` \| `x-api-key` | `none` | How the credential is sent. |
| `key_env` | string | none | Environment variable holding the credential. Required unless `type` is `none`. An unset variable for a selected ingress fails preflight. |

### ingresses[].timeouts

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `connect` | seconds | `5` | Connection timeout. |
| `request` | seconds | `600` | Whole-request timeout. |
| `stream_idle` | seconds | `60` | Longest gap between stream events. |

### ingresses[].tls

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `verify` | bool | `true` | `false` skips certificate checks for this ingress only, such as behind a private ingress CA. Validation warns. Other connections still verify. |

### ingresses[].setup

Non-executing. llmdv prints the commands and checks whether they are satisfied; it never starts or supervises a tunnel.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `proxy` | `bypass` \| `allow` | `bypass` | `bypass`: the request must not go through an HTTP proxy, and preflight fails if one would capture it. |
| `requires` | list | `[]` | Preconditions such as a port-forward. |

### ingresses[].setup.requires[]

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `kind` | string | `port_forward` | The only kind today. |
| `selector` | string | none | Label selector, resolved to a live pod. Preferred over `target`, because pod names change. |
| `target` | string | none | `pod/<name>` or `svc/<name>`. One of `selector` and `target` is required. |
| `local_port` | int | none | Required for `port_forward`. |
| `remote_port` | int | none | Port on the target. |
| `namespace` | string | `environment.namespace` | Namespace of the target. |
| `command` | string | generated | Replaces the generated command. |

### ingresses[].logs[]

Where this layer's own account of a request lives. Without `logs`, llmdv reads the `setup.requires` targets.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `selector` | string | none | Label selector for the pods. |
| `target` | string | none | `pod/<name>` or `svc/<name>`; a Service resolves through its selector. |
| `container` | string | all | Restrict to one container. |
| `tail` | int | `2000` | Line cap per container for failure evidence. |
| `forbid` | list of `{id, pattern}` | `[]` | Lines that must not appear during the run. See [FLEET.md](FLEET.md#logs). |
| `allow` | list of pattern | `[]` | Patterns that excuse a line a `forbid` entry matched. |

### ingresses[].transport

Every key is optional; a case whose key is absent skips as `undeclared`. Paths must start with `/`, and engine admin routes such as `/pause` or `/collective_rpc` are refused. Exposure cases send only GET.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `health_status` | list of int | `[]` | Statuses the health path answers. |
| `present_paths` | list of path | `[]` | Paths a GET must answer with a 2xx. |
| `absent_paths` | list of path | `[]` | Paths a GET must answer with one of `absent_status`. |
| `absent_status` | list of int | `[403, 404]` | Statuses that count as absent. |
| `http_redirect` | `expected` | none | `http://` plus the host of this ingress's own HTTPS base URL must redirect to HTTPS. The address is derived at run time, so a committed profile names no hostname. See below. |
| `http_url` | `http://` URL | none | A plain-HTTP listener somewhere other than the ingress's own host, which must redirect to HTTPS. It is a literal hostname, so it belongs in a local profile only. Declaring it together with `http_redirect` is an error. |
| `cors_preflight` | `allowed` \| `refused` | none | How a browser-origin OPTIONS preflight is answered. |
| `alpn` | `h2` \| `http/1.1` | none | Protocol the TLS listener picks when offered both. |
| `credential_headers` | list | none | Headers besides `Authorization` that carry the key, from `x-api-key` and `api-key`. `[]` means both are refused. |
| `model_rewrite_header` | header name | none | Header naming the served model when a request names an alias. |
| `idempotency` | `replayed` \| `ignored` | none | What a repeated `Idempotency-Key` returns. |
| `stream_budget` | mapping | none | Streaming latency limits; see below. |
| `max_body_bytes` | int | none | Request body limit; a larger body must get a fast 4xx. |

`http_redirect` belongs on the client-facing ingress reached over TLS, usually the gateway: `transport.http_redirect` requests `http://<host of the base URL>` on the default port and requires a 301, 302, 307 or 308 whose `Location` is `https://`. An internal layer reached over plain HTTP, such as a port-forward to an EPP or an engine, should not declare it. The base URL, whether `base_url` or the value of `base_url_env`, must be `https://` on the default port (443, written or not). An explicit other port says nothing about where a plain listener would be, so llmdv does not guess one; declare `http_url` instead. A literal `base_url` that does not qualify is a profile error at load; a `base_url_env` value that does not qualify is a `base_url` preflight problem (exit 3) naming the variable, never its value. The derived address is a hostname: no message quotes it, and it appears only in `exchanges.jsonl`, as `base_url` does.

### ingresses[].transport.stream_budget

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `first_event_s` | seconds | none | Request to first output delta. |
| `max_gap_s` | seconds | none | Longest gap between deltas. |
| `min_tokens_per_s` | number | none | Lowest output rate. |
| `next_request_s` | seconds | none | Time to serve the next request after a cancel or client timeout. The retry after a client timeout streams and is held to this until its first output delta; with `min_tokens_per_s` declared, the whole retry also gets its reported output at that rate. |

### Per-ingress overrides

`model` and `capabilities` at the top are defaults. An ingress may override any field of either, so a gateway and its engine can expose different names and shapes for one model.

| Declaration | Effect |
| --- | --- |
| Field omitted, or an empty mapping | Inherit. |
| Nested mapping (`reasoning`, `tools`, `model_info`, `contract_baseline`, …) | Replace only the fields given. |
| A list, including `[]` | Replace the whole list; never append. |
| `null` | Clear `max_model_len`, `answer_max_tokens`, `model_info`, `model_retrieval`, `responses_input_tokens`, `responses_compact`, `cache_reuse`, `effort_rendering`, a parser, `reasoning.field` or `reasoning.inline_tag`. Other fields cannot be null. |

`llmdv profile show`, `show --matrix` and the run banner print each ingress's effective values.

### Surface names

`chat_completions`, `completions`, `messages`, `responses`, and the render surfaces `chat_render`, `completions_render`, `messages_render`, `responses_render`. Render surfaces run engine-input checks, not generations. A surface llmdv cannot drive yet is a validation warning, and its cases skip as `not_implemented`.

### Error classes

Keys accepted by `error_status` and `unstructured_errors` (`ERROR_CLASSES` in `schema.py` and `TRANSPORT_ERROR_CLASSES` in `transport.py`):

- Request validation: `bad_credentials`, `context_overflow`, `invalid_api_version`, `invalid_field_value`, `invalid_token_limit`, `invalid_tool_choice`, `invalid_tool_schema`, `internal_image_url`, `malformed_json`, `missing_required_field`, `orphan_tool_result`, `output_overflow`, `streaming_validation`, `unknown_model`, `unknown_path`, `unknown_response`, `unsupported_feature`, `unsupported_option`, `wrong_field_type`, `wrong_method`.
- Transport and variants: `body_too_large`, `empty_input`, `header_underscore`, `image_input`, `invalid_image`, `invalid_role`, `invalid_thinking_type`, `malformed_body`, `missing_content_type`, `missing_model`, `missing_token_limit`, `path_variant`, `request_content_encoding`, `sampling_option`, `server_tool`, `token_limit_over_context`, `tool_choice_without_tools`, `wrong_content_type`.

Where serving and refusing are both correct, a transport case accepts a 2xx or a 4xx unless the class has declared statuses. `output_overflow` is the one request-validation class where a 2xx is a contract rather than a gap: `[200]` states the engine clamps an output budget the window cannot hold and generates within it (vLLM since vllm-project/vllm#57035); undeclared or a 4xx means the request is refused naming the window. Both are verified: a refusal fails the clamp declaration and a generation fails the refusal one. `image_input` with declared statuses states the model is text-only. `invalid_api_version` without a declaration skips its probe.

## capabilities

Optional features within the declared surfaces. Whether a route is served at all belongs in `ingresses[].surfaces`, not here. Every key is inherited by each ingress and can be overridden there. "Surface list" means `true`, `false`, or a list of surface names.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `anthropic_betas` | list of string | `[]` | `anthropic-beta` values the Messages ingress accepts; one case sends them together. |
| `background_responses` | bool | `false` | Bounded background cancellation, alongside `stateful_responses`. Excluded on shared fleets. |
| `byte_fidelity` | list | `[]` | Probes that a request reaches the model unchanged. See [Change sentinels](#change-sentinels). |
| `cache_reuse` | mapping or `null` | `null` | Native prefix-cache probes. See [cache_reuse](#cache_reuse). |
| `chat_fields` | mapping | `{}` | Chat field probe to `accepted`, `ignored` or `rejected`. Names are `CHAT_FIELDS` in `schema.py`. |
| `chat_options` | list | `[]` | Chat `n` and/or `logprobs`. |
| `chat_quiet_efforts` | list | `[]` | Chat efforts answered with no reasoning. Disjoint from `chat_reasoning_efforts`. The cases that turn reasoning off send the first, else `none` from `chat_reasoning_efforts`, else the lowest declared Chat effort, recorded as reasoning that stays on. |
| `chat_reasoning_efforts` | list | `[]` | Chat `reasoning_effort` values the fleet supports. |
| `chat_repeats` | list | `[]` | `seed` (same seed, same text), `greedy` (temperature 0 repeats; also enables stream-against-JSON parity) and/or `fresh` (two sampled requests differ). |
| `client_model_names` | list of string | `[]` | Model names clients send by default. Names in `public_id` or `aliases` must be served; every other must be refused as `unknown_model` with no generation. |
| `client_reuse` | bool | `false` | The pinned client's three-turn cache case. Needs a pinned binary and `cache_reuse` on `messages`. |
| `client_sessions` | list | `[]` | Real Claude Code sessions to run. See [Claude Code clients](#claude-code-clients). |
| `completions_fields` | mapping | `{}` | Completions field probe to `accepted`, `ignored` or `rejected`. Names are `COMPLETIONS_FIELDS` in `schema.py`. |
| `contract_baseline` | mapping | `{}` | Declared key paths and JSON types of responses, streams and errors, per surface. See [Change sentinels](#change-sentinels). |
| `count_tokens` | bool | `false` | `/v1/messages/count_tokens` cases, on ingresses serving `messages`. |
| `effort_rendering` | mapping or `null` | `null` | The effort marker the template renders for each requested effort, read back through `prompt_logprobs`. See [effort_rendering](#effort_rendering). |
| `forward_compat` | mapping | `{}` | Declared outcome of each unknown request element. See [Change sentinels](#change-sentinels). |
| `header_probe` | bool | `false` | An operator-run header instrument is reachable. See [headers](#headers). |
| `header_rewrites` | mapping | `{}` | Non-credential header to the value the instrument must see. |
| `json_object` | surface list | `false` | JSON-object mode on Chat and Responses. |
| `image_url_fixture` | `https://` URL | none | An image (PNG, JPEG, GIF or WebP) hosted online that the deployment can fetch, sent as the Messages `url` image source by `messages_features.image_url`, as Anthropic documents that source. Undeclared, that feature skips as `requires_instrumentation` when declared `accepted`; declared `rejected`, it sends the fixture when one is declared, so a 4xx proves a policy rather than a failed fetch, and an unreachable URL otherwise. A deployment that refuses the source or fails to fetch the declared image fails. |
| `messages_checks` | list | `[]` | Opt-in Messages cases. See [Opt-in checks](#opt-in-checks). |
| `messages_efforts` | list | `[]` | Messages output efforts the fleet supports. Declaring any also checks an invalid effort is refused. |
| `messages_features` | mapping | `{}` | Messages request feature to `accepted` or `rejected`. Undeclared features are not sent. |
| `messages_thinking` | list | `[]` | Thinking modes the fleet supports. |
| `model_info` | mapping or `null` | `{}` | Field to the value the gateway's `GET /v1/model/info` entry must report for the model. Values are booleans, numbers, strings or string lists; booleans match exactly, so `null` never satisfies `false`. Use `null` on an ingress that does not serve the route. |
| `model_retrieval` | bool or `null` | `null` | `true`: `GET /v1/models/{id}` returns the model. `false`: it is refused with a 4xx. |
| `openai_contract` | bool or list | `false` | Spec-derived Chat and Completions cases: every documented field, nullable fields as null, token limits, stop strings, message shapes, stream parity and body fidelity. A list selects `chat_completions` and/or `completions`. |
| `prefix_stability` | surface list | `false` | Synthetic token-prefix checks on render surfaces. |
| `prompt_pins` | mapping | `{}` | Input token count of a fixed conversation, per surface. See [Change sentinels](#change-sentinels). |
| `reasoning_tokens` | surface list | `false` | Native reasoning token accounting on Chat and Responses. Declare only where the parser provides it. |
| `render_pins` | mapping | `{}` | SHA-256 of the fixed conversation's rendered token ids, per render surface. See [Change sentinels](#change-sentinels). |
| `responses_compact` | bool or `null` | `null` | `true`: `POST /v1/responses/compact` compacts, and the replay case runs. `false`: refused with a 4xx and an envelope. `null` accepts either, never a 5xx. |
| `responses_features` | mapping | `{}` | Responses feature to `accepted`, `ignored` or `rejected`. Undeclared features may be refused or ignored, never a 5xx. |
| `responses_input_tokens` | bool or `null` | `null` | `true`: `POST /v1/responses/input_tokens` counts. `false`: refused with a 4xx and an envelope. `null` accepts either, never a 5xx. |
| `responses_options` | list | `[]` | Responses `metadata` and/or `logprobs`. |
| `responses_reasoning_efforts` | list | `[]` | Responses `reasoning.effort` values the fleet supports. |
| `scenario_checks` | list | `[]` | Opt-in session and scenario cases. See [Opt-in checks](#opt-in-checks). |
| `stateful_responses` | bool | `false` | Stored response retrieval and cancellation. Only ids the case created are used. |
| `stateless_responses` | bool | `false` | The fleet keeps no response state: retrieval, chaining and background generation of its own responses are refused. Exclusive with `stateful_responses` and `background_responses`. |
| `structured_output` | surface list | `false` | The JSON Schema case on the selected surfaces. |
| `structured_output_streaming` | surface list | `false` | The streamed JSON Schema case, alongside `structured_output`. |
| `vision` | surface list | `false` | The model reads images: the image cases run on the selected surfaces. Each sends a freshly drawn picture and requires the answer to describe it. |
| `vllm_options` | list of string | `[]` | vLLM request options the fleet accepts. See [Vocabularies](#vocabularies). |

### Vocabularies

Each list lives once in `src/llmdv/profile/schema.py` and is shared by parsing and case generation.

- **Chat and Responses efforts** (`chat_reasoning_efforts`, `chat_quiet_efforts`, `responses_reasoning_efforts`): `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`.
- **Messages efforts** (`messages_efforts`): `low`, `medium`, `high`, `xhigh`, `max`.
- **Messages thinking** (`messages_thinking`): `enabled`, `adaptive`, `disabled`.
- **Client sessions** (`client_sessions`): `skill`, `hook`, `parallel_reads`, `nested_subagent`, `compaction`, `multi_file_fix`, `image_read` (needs `vision` on Messages).
- **Chat fields** (`chat_fields`): sampling fields, roles and message shapes, vLLM extensions and OpenAI-only fields; the full list is `CHAT_FIELDS`.
- **Completions fields** (`completions_fields`): the full list is `COMPLETIONS_FIELDS`.
- **Field dispositions**: `accepted` and `ignored` both require a normal generation and differ only in what the profile documents; `rejected` requires a 4xx with the surface's error envelope. A 5xx fails under every disposition.
- **Responses features** (`responses_features`, full list `RESPONSES_FEATURES`): request options such as `temperature`, `service_tier`, `prompt_cache_key`, `reasoning_summary` and `encrypted_reasoning`; references to stored state (`conversation`, `prompt_template`, `item_reference`, which accept only `ignored` or `rejected`); input parts (`input_image`, `input_file`); built-in tools such as `web_search`, `file_search`, `code_interpreter`, `mcp` and `shell`. `accepted` requires a healthy generation that honours the feature, `ignored` a healthy generation with no built-in tool call, `rejected` a 4xx with an envelope. `encrypted_reasoning: accepted` also runs the encrypted replay case.
- **Messages features** (`messages_features`, full list `MESSAGES_FEATURES`): fields (`metadata_user_id`, `top_p`, `top_k`, `service_tier`, `container`, `mcp_servers`, `inference_geo`, `diagnostics`, `speed_fast`, `stop_sequences_five`), caching (`top_level_cache_control`, `cache_control_ttl_1h`, `cache_breakpoints_five`, `cache_control_message_block`, `cache_control_tool`), limits (`max_tokens_zero`, `max_tokens_large`, `max_tokens_over_window`, which needs `max_model_len`), thinking (`thinking_display_omitted`, `thinking_display_updates`, `thinking_between_tools`), turns (`consecutive_user_turns`, `consecutive_assistant_turns`, `leading_assistant`, `assistant_prefill`) and content (`document_text`, `document_pdf`, `document_content`, `document_citations`, `search_result`, `image_base64`, `image_url`, `tool_result_blocks`, `tool_result_image`, `redacted_thinking_passback`, `server_tool_history`, `text_citations_passback`). `accepted` content must reach the model; `rejected` needs a 4xx whose `error.type` matches its status. `image_url` declared `accepted` sends the declared `image_url_fixture` and skips as `requires_instrumentation` without it; declared `rejected`, it needs no fixture, and sends the declared one if there is.
- **vLLM options** (`vllm_options`, free strings): `return_token_ids`, `min_tokens`, `ignore_eos` and `truncate_prompt_tokens` enable Chat probes (Responses supports `ignore_eos` only); `return_token_ids` and `truncate_prompt_tokens` also enable the Completions probes where `openai_contract` covers `completions`. `include_stop_str_in_output`, `continue_final_message`, `echo`, `continuous_usage_stats`, `structured_outputs`, `guided_choice`, `return_tokens_as_token_ids`, `prompt_logprobs`, `allowed_token_ids`, `bad_words`, `logit_bias`, `enable_thinking`, `include_reasoning` and `stop_reason` enable Chat cases judged by their effect (`stop_reason` also enables `openai_completions.stop_reason`). `cache_salt` enables the salt-isolation probe and `chat_template_kwargs` sends quiet efforts as a template kwarg.

### Opt-in checks

`messages_checks` names Messages cases with no other capability to hang on:

| Name | Declares |
| --- | --- |
| `mid_conversation_system` | A `role: "system"` entry inside `messages` reaches the model. |
| `prompt_accounting` | Reported input tokens track the rendered prompt. |
| `context_management` | The `context_management` field is accepted with its beta value. |
| `effort_effect` | `low` and `high` effort change output size; both must be in `messages_efforts`. |
| `incremental_streaming` | A stream is relayed as it is produced. |
| `parallel_tool_calls` | Several tool calls in one turn. Needs a tool parser. |
| `stop_sequences` | A matched stop sequence ends the text with `stop_reason: stop_sequence`. |
| `max_tokens_limit` | `max_tokens: 1` ends with `stop_reason: max_tokens`. |
| `response_shape` | The native Message fields a strict client reads, and stream-to-message agreement. |
| `count_tokens_parts` | `count_tokens` grows with a system prompt or a tool. Needs `count_tokens`. |
| `block_forms` | A string and one text block report the same input tokens. |
| `tool_result_error` | `is_error: true` on a tool result reaches the model. |
| `error_types` | Errors carry the `error.type` their status calls for. |

`scenario_checks` names shapes a fleet may legitimately refuse, or that cost it real work:

| Name | Declares |
| --- | --- |
| `inline_system` | A system entry inside the history renders where it was sent, on each `cache_reuse` surface. |
| `parallel_subagents` | Four concurrent requests sharing a prefix. Disruptive. |
| `long_context` | Prompts sized from `max_model_len`. Needs `cache_reuse`; disruptive. The near-limit requests stream, each gap bounded by the larger of `timeouts.request` and `timeouts.stream_idle`, as the first event waits on the prefill. |
| `output_overflow` | An output budget the window cannot hold is refused with a 4xx naming `max_model_len`. |
| `blank_turns` | Empty earlier turns are answered or refused with a 4xx, never a 5xx. |
| `cache_hints` | The cache hints newer clients send are accepted and leave the prompt unchanged. |

### Change sentinels

These declare what a fleet does so the first run after a fleet or client change fails the case that names it. The values come from a run against a known-good build: declare placeholders, run once with `--evidence all`, and copy each observed value from the failing check. The [`writing-an-infra-profile`](../.agents/skills/writing-an-infra-profile/SKILL.md) skill has the procedure.

- **`forward_compat`**: probe to `accepted`, `rejected` or `either`, or a mapping of surface to outcome. Probes: `unknown_message_field`, `unknown_block`, `unknown_tool_type`, `unknown_header`, `unknown_beta`, `unknown_item`, `unknown_enum_value`, `count_tokens_block`. Every outcome fails a 5xx, and each probe is followed by a plain request that must succeed.
- **`byte_fidelity`**: `big_integer`, `big_integer_history` (both need a tool parser), `key_order` (needs `structured_output`) and `unicode_escapes`.
- **`prompt_pins`**: surface to `{conversation: <tokens>, tools: <tokens>}`. Any change to the template, tokenizer or a layer's translation fails the pin.
- **`render_pins`**: render surface to `{conversation: <sha256>, tools: <sha256>}`, a 64-character lowercase hex digest. Each run records the observed digest as `canonical_render_digest`.
- **`contract_baseline`**: surface to parts (`response`, `stream`, `error`, `invalid`, `tool_response`, `tool_stream`), each a key path to allowed JSON types: `string`, `integer`, `number`, `boolean`, `null`, `object`, `array`, `absent`. Array elements use `[]`, as in `choices[].message.content`; stream event paths are prefixed with the event type, as in `message_start:message.usage.input_tokens`.

## cache_reuse

`capabilities.cache_reuse`, or the same under an ingress. Declare only APIs that report native per-request cache usage. Set exactly one of `single_replica: true`, `replica_header` or `affinity: routed`.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `min_ratio` | number | **required** | Warm-reuse threshold, above 0 and at most 1. |
| `surfaces` | list | `[chat_completions]` | Non-empty, from `chat_completions`, `messages`, `responses`. |
| `prefix_words` | int | `1024` | 128 to 4096. Choose a prefix within the window and above the block size. |
| `single_replica` | bool | `false` | Requests can reach only one backend replica. |
| `replica_header` | header name or `null` | `null` | Non-credential response header naming the serving replica. A changed replica skips as `requires_instrumentation`. |
| `affinity` | `routed` or `null` | `null` | The ingress itself places a repeat on a replica holding its prefix, so a miss fails. |
| `block_size` | int or `null` | `null` | The engine's KV block size, at most `prefix_words`. Replaces the ratio checks with exact block counts. Declare it only where the template renders less than one block before the request's own text. |
| `withheld_blocks` | int | `0` | Trailing blocks of every hit the deployment withholds by design: declare it where the engine recomputes the last chunk of a hit for a speculative-decoding draft's KV (vLLM with an MTP or EAGLE draft whose layers it tags as draft). Needs `block_size`; the probe prefix must fill more blocks than are withheld. It lowers every expected block floor, repeat and turn, by that many blocks, on top of a turn case's own slack; reading the full floor still passes. It does not excuse a read of 0: a warm repeat that reads nothing still fails, and a floor lowered to 0 fails as too short to judge. The checks record the declared allowance in their expected value. |
| `canonical_tool_keys` | bool | `false` | The template renders tool schema keys in a fixed order. Enables `cache.tool_key_order`. |
| `cold_max_ratio` | number or `null` | `null` | Highest cache ratio a cold (salted) request may read, from 0 up to but not including `min_ratio`. With `vllm_options: [cache_salt]`, this or `block_size` enables the salt-isolation probe. |
| `render_equivalent` | list | `[]` | Two or more of `surfaces` that render one plain conversation alike. Enables `cache.cross_api`. |
| `stripped_system_prefix` | string or `null` | `null` | Opening text of a leading system line the stack removes before rendering. Enables `cache.attribution_line`. |
| `equivalent_settings` | list of pairs | `[]` | Pairs of `surface:setting` that render alike, such as `[messages:disabled, chat_completions:low]`. A Messages `enabled` entry may carry its thinking budget, `messages:enabled:2048`: a whole number of at least 1024, the default; the request's `max_tokens` is set above it. Declare the budget where a gateway maps budgets onto efforts. Enables `cache.setting_equivalence`. |
| `shared_with` | list | `[]` | Ingress ids in front of the same engines and prefix cache. Enables `cache.cross_ingress`; an unknown id is an error. |

Template keyword and aggregate-metric observability for the cache cases is not built yet.

## effort_rendering

`capabilities.effort_rendering`, or the same under an ingress. A deployment can map a requested effort onto another (vLLM's `--reasoning-effort-map`), and no response says which effort the model was given. For each declared effort, and for a request with no effort, the `field_options.*_effort_rendered` cases send one token with `prompt_logprobs: 0`, rebuild the rendered prompt from each position's `decoded_token`, and require `pattern` to capture exactly the declared marker, once. A missing `prompt_logprobs` on a declared surface fails. Declare a surface only where its layer returns prompt logprobs; through the LiteLLM gateway only Chat does (see [`.agents/GROUND-TRUTH.md`](../.agents/GROUND-TRUTH.md)).

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `pattern` | regex | **required** | A regular expression with exactly one capture group, such as `'Reasoning Effort: (\w+)'`. |
| `efforts` | mapping | **required** | Requested effort to the text the group captures, or `null` where the template renders no marker. Keys are `unset` (no effort sent) and the Chat and Responses efforts; each API runs the keys its own field takes (Messages has no `none` or `minimal`). An effort left out skips as `undeclared`. |
| `surfaces` | list | `[chat_completions]` | Distinct names from `chat_completions`, `responses`, `messages`. |

The effort travels in each API's own field: Chat `reasoning_effort`, Responses `reasoning.effort`, Messages `output_config.effort`. Overriding `efforts` under an ingress replaces only the efforts given.

`cache.kwargs_effort_reuse` reads the Chat markers too. It sends one effort both as `reasoning_effort` and as `chat_template_kwargs.reasoning_effort`, and an effort map rewrites only the first, so a mapped effort sent as a kwarg reaches the template unmapped and is refused. It takes the first declared effort, quiet efforts first, that the markers do not show as folded onto another: one whose marker is `null`, spells the effort itself (`low` rendering `Low`), or is rendered by no other effort. Without Chat `effort_rendering` it takes the first declared effort.

## headers

Header name to a mapping of ingress id to disposition. The same header can be handled differently at each layer.

```yaml
headers:
  x-llm-d-inference-objective:
    vllm: ignored
    router: consumed      # skips: requires_load
    litellm: rewritten    # skips: requires_instrumentation
```

| Disposition | Meaning | Verifiable by |
| --- | --- | --- |
| `ignored` | Accepted, no effect at this layer | status |
| `consumed` | Read and acted on, not forwarded | load experiment |
| `forwarded` | Passed on unchanged | instrumented backend |
| `stripped` | Removed, not forwarded | instrumented backend |
| `rewritten` | Replaced with a value this layer computes | instrumented backend |
| `echoed` | Returned in the response | response headers |
| `rejected` | The request fails when the header is present | status |
| `promised` | A response header this layer guarantees, non-empty on success | response headers |
| `absent` | A response header this layer does not emit | response headers |

Only status and response headers are visible to a client. Backend dispositions need the header instrument: on an isolated route, declare `capabilities.header_probe: true`, `surfaces: [chat_completions]` and `model.public_id: llmdv-header-probe`, and run `python -m llmdv.instruments.header_echo --allow-header <name>`, which binds to localhost and never allowlists credential headers. Without it they skip as `requires_instrumentation`. `consumed` needs an approved load experiment and skips as `requires_load`.

## clients

A real consumer and what it depends on. `--client <id>` selects the relevant cases.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `id` | string | none | Client name for `--client`. |
| `surfaces` | list | `[]` | Surfaces it needs. Each must be served by some ingress, or validation fails. |
| `needs` | list | `[]` | Capabilities it needs, such as `streaming`, `tools`, `reasoning`. |
| `binaries` | list | `[]` | Pinned Claude Code binaries. |

### clients[].binaries[]

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `version` | string | **required** | Exact version, `2.1.0` or newer, quoted. Distinct within a client. |
| `executable` | path | **required** | Path to the binary; relative paths resolve against the profile's folder. |

### Claude Code clients

llmdv never installs a client. The runner image ships Claude Code 2.1.289 at `/usr/local/bin/claude`; elsewhere, install one and pin it. The harness drives Messages only and checks `--version` before sending anything. A missing binary skips as `requires_instrumentation`.

`client_sessions` runs real sessions with tools, project settings and hooks enabled, each capped at five minutes or `timeouts.request`, whichever is smaller. They need a tool-call parser. `hook` needs Claude Code 2.1.154 or newer, and `compaction` needs `model.max_model_len` declared; both skip rather than fail when unmet. A failing session keeps `session-trace.jsonl` and `session-project.txt` in its evidence.

**Sessions execute model-generated code.** `multi_file_fix` runs `python3 -m unittest` on files the model edited, with llmdv's own interpreter. Writes are confined to the session's project; reads and the executed code are not. Run sessions only in a disposable pod or container.

`client_reuse: true` runs a three-turn case that resumes one session twice and reads native cache usage. It needs `cache_reuse` on `messages`.

## known_failures

A failure the team has filed and traced to a fix the fleet does not carry yet. The case still
runs, its outcome is still `fail`, and `llmdv verify` still exits `1`: the fleet is still broken.
What changes is that the record, the counts, the closing banner, `summary.md`, the CI job
summary and `llmdv compare` say the failure is known and where the fix is, so a reader can tell
it from a new one. This labels a failure; it never removes one.

```yaml
known_failures:
  - check: error_envelope                    # the first failing check, as verdict.txt names it
    cases: ["discovery.model_retrieve*"]     # optional: globs over the case id, or id[variant]
    ingress: litellm                         # optional: one ingress id
    issue: example-org/serving-stack#216
    fix: "LiteLLM 1.104.1"
    until: {x-litellm-version: "1.104.1"}    # optional: the build the fix ships in
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `check` | string | **required** | The name of the first named check recorded not-ok, the key failure groups are built on. `*` and `?` are wild, with at least one literal character: an entry can cover a family of checks, never every check. A failure of any other check is a plain failure, whatever the case. |
| `issue` | string | **required** | Where it is tracked: `owner/repo#N`, `#N` for this repository, or a URL. |
| `cases` | list of string | any case | Globs over the case id (`tools.*`) or `id[variant]` (`openai_chat.request_field[logit_bias]`). Brackets are literal. |
| `ingress` | string or list | any ingress | Declared ingress ids. |
| `fix` | string | none | Where the fix is, for the reader: a PR, a release, a config change. |
| `until` | mapping | none | The build the fix ships in, as signals the run records in `outcome.json`'s `stack` block: `x-litellm-version`, `system_fingerprint`, `server`, a router header. Values are quoted strings (`"1.104.1"`; YAML would read `1.100` as the number 1.1). The fix is expected once every named signal has been seen at or past its value: equal, or a later dotted version, compared after the same literal prefix (`Python/3.13` reads the number after `Python/`). A value with no dotted number, such as a commit fingerprint, matches by equality only. |

What a run reports:

- A failure of the named check, in a case and ingress the entry covers, is recorded with
  `known_failure.status: known`. The most specific entry wins: one naming cases or an ingress
  before one naming neither, then profile order. An entry with neither is accepted with a
  validation warning, because it covers every failure of its check at every layer. The banner's headline reads `268 of 1030 cases failed (41
  known, fix pending)`, the `failed` line and `summary.md` break the known ones out, and a
  `known failures` block lists them by issue. `outcome.json` carries them under
  `verification.known` and marks each entry in `verification.failures`.
- When `until` is set and the case's own responses carried the awaited build, the status is
  `fix_expected` instead: the failure is reported as a plain one, with the note that the fix
  was expected on this build and the signals that said so. An entry nobody checks is how a fix
  gets lost. Only responses from the entry's own ingresses count, so in a cross-layer case the
  gateway's header never speaks for the engine. A response that does not carry the signal,
  such as a 404 from a route the gateway answers itself, or a stream (its recorded body is not
  an object, so `system_fingerprint` is never read from one), cannot be judged and the failure
  stays `known`. Name a signal the failing response carries.
- A pass is never labelled. An entry that labelled no failure in the run is listed in the
  banner, `summary.md` and `outcome.json` (`verification.known.unmatched`): either the fix
  landed and the entry should go, or the selection left its cases out. `llmdv compare` reports
  a fix landing when a case the previous run labelled passes now, and never calls a regression
  known. See [docs/compare.md](../docs/compare.md).
- `verdict.txt` in a failure bundle, `alerts.json` and the dashboard's failure groups carry the
  label too, so a reader who starts anywhere learns the failure is filed.

Editing this block, or a comment, does not change the profile digest. Only declarations do, so a
comparison window is not dropped each time a finding is filed.

## fleet

Rules read from the fleet's configuration, metrics and logs. See [FLEET.md](FLEET.md).
