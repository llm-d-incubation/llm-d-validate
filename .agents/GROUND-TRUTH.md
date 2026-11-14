# Ground truth

Behaviours of serving-stack components that are **measured, not assumed**, and what each one
means for a test. Every entry here was wrong in a first draft somewhere, or cost a benchmark run
to find.

Read this before writing an assertion. The recurring failure mode in this codebase has been an
assertion that is plausible, passes review, and is wrong about the stack.

Keep deployment identifiers out of this file. Describe shapes and mechanisms, not addresses.

---

## The engine

**`finish_reason: "abort"` arrives on HTTP 200.** A vLLM replica that has left `RUNNING`
rejects work through `_reject_add_in_shutdown`. Anything counting non-200s scores a dropped
request as a success. *Assert the finish reason, never the status alone.* This is why
[`assertions/common.py`](../src/llmdv/assertions/common.py) checks `abort` explicitly and every
surface inherits it.

**Without `--reasoning-parser`, reasoning arrives inline.** It comes as a literal `<think>`
block inside `content`, and the separate `reasoning_content` field is `null`. A model that
reasons by default can spend its entire budget there, so `content` is empty on a perfectly
healthy response. *Assert "content OR the declared reasoning field", never content alone.* The
profile must declare which shape applies; asserting the wrong one fails a working deployment.

**The same shape, from the client's side, can be a defect.** The entry above is why
`assert_has_output` counts reasoning as output -- otherwise a healthy deployment fails. But a
model given an output budget smaller than its reasoning appetite spends the whole budget
thinking, stops at `finish_reason: "length"` and returns **no answer at all**, and the client got
nothing usable. Status, shape and content checks all pass. The discriminator is the
**combination**: `length` (or `max_tokens`) **and** empty answer text **and** non-empty
reasoning. Any one alone is ordinary; together they mean the budget ran out before the model
began answering.

**A `<think>` tag is split across frames when streaming.** Split inline reasoning after
concatenating the stream, not per frame.

**Stop strings are not guaranteed to be dormant inside reasoning.** Matching a client stop
string in reasoning is not a serving defect. OpenAI's
[Responses API](https://developers.openai.com/api/reference/python/resources/responses/methods/create)
does not expose `stop`, and its
[Chat API](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)
documents stop as unsupported on reasoning models such as o3 and o4-mini. vLLM proposals
[#52486](https://github.com/vllm-project/vllm/pull/52486) and
[#38533](https://github.com/vllm-project/vllm/pull/38533), discussed in
[#52393](https://github.com/vllm-project/vllm/issues/52393), remain unmerged as of 2026-10-15.
llmdv checks stopping in answer text, not reasoning-channel isolation. A prompt that does not
quote the stop string still cannot guarantee the model will never reason with that string.

**Greedy sampling permits only one choice in vLLM.** Chat requests with `temperature: 0`
and `n: 2` fail with `400: n must be 1 when using greedy sampling, got 2.` even on a deployment
that supports multiple choices. *Use an explicit nonzero temperature for `n > 1`*, in both
streaming and non-streaming probes, and assert each choice's structure and healthy completion
without requiring exact text or distinct answers.

**A non-strict tool's arguments are not held to its schema.** vLLM pins a tool's argument
schema only when that tool sets `strict: true`, or when the server runs
`--tool-strict-level parameter`; the `auto` and `function` levels constrain the call markup and
name only. OpenAI promises the same: schema adherence is a `strict` feature. So
`tool_choice: "required"` on a prompt with nothing to fill in can return `get_weather({})` for a
tool whose schema requires `city`, and that is the model's answer, not a serving defect. The
profile declares the level (`model.tools.strict_level`); the cases assert schema-valid arguments
only for strict tools, or for every tool under `parameter`.

**Responses `max_tool_calls` caps built-in tools, not function calls.** The
[OpenAI parameter definition](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
limits calls to built-in tools such as web search. A function-tool request carrying
`max_tool_calls: 1` may return both requested function calls; rejecting that response is a
test defect. Validate the calls normally, without using this field as their count bound.
The fake tool engine must follow the same rule. Built-in-tool cap enforcement needs a separate
probe that actually executes built-in tools.

**Reasoning and tool parsers can be per-role.** In a disaggregated deployment the reasoning and
tool-call parsers are commonly set on the decode container only; prefill has neither. So two
ingresses of the same deployment can legitimately disagree. Declare the difference through
per-ingress `model.reasoning` and `model.tools` overrides; omitted fields inherit the profile
defaults, while `parser: null` clears an inherited parser. *Do not read a prefill/decode
difference as a gateway defect.*

**Chunk frames carry vendor fields.** Expect `prompt_token_ids`, `token_ids`, `prompt_text` and
similar. *Tolerate unknown fields; reject only a missing sentinel, a stall, or an unparseable
frame.*

**`stream_options.include_usage` adds exactly one frame**, with `choices: []` and populated
usage, arriving **after** the finish-reason frame and before `[DONE]`. Without the flag there
are zero usage frames. Both halves are contract: a stack that emits it unconditionally breaks a
client that treats a choice-less frame as the end of content; one that emits it before the
finish reason is never seen by a client that stops there.

**Unknown top-level request fields are ignored, not refused.** vLLM accepts a body carrying a
field it does not define, returns 200 and generates normally. Nothing reads the field. *Do not
hard-code a refusal in a case*: it asserts whatever the profile declares. The client-visible
consequence is a setting that silently does nothing. The profile still declares the correct
refusal; a success status under `error_status` is declared only once the failure has been
triaged and accepted.

**`/v1/messages` and `/v1/responses` are served by the engine.** As of vLLM v0.30.0, both
answer 200, at the engine and through a gateway in front of it. *Do not assume an
OpenAI-compatible server only speaks `/v1/chat/completions`* — the Anthropic Messages and
Responses surfaces are reachable, and the suite tests them as surfaces of their own.

**Streams terminate with `data: [DONE]`.** It carries no JSON. *Treat it as a sentinel, not
data, and stop reading there* — anything after it is not part of the response and consuming it
can hang on a connection the server holds open.

**Cached-token counts move in whole KV blocks, and the last prompt token is never read from
cache.** The engine reuses full blocks only, and recomputes the final prompt token to sample
from it, so a fully warm repeat of a P-token prompt reads `floor((P - 1) / B) * B` tokens for a
block size of B. Every reading is a whole multiple of the block size. *A ratio cannot be exact.*
A warm repeat falls short of its prompt by anything from one token to a block, depending on
where the prompt ends inside its last block, so a threshold that passes every warm repeat also
passes some that read a block short. Declare `cache_reuse.block_size` and the cache cases assert
the floor itself.

**With a speculative-decoding draft, a hit can read one block below that floor, by design.**
vLLM recomputes the draft model's KV for the last chunk of a hit, because draft KV there can be
stale; where the draft layers are tagged as such (vllm-project/vllm#61455), the cache withholds
that chunk and every warm read is exactly one block short. A build that does not recognise its
draft layers reads the full floor with the same draft. *Declare `cache_reuse.withheld_blocks`*
rather than reading a block short as a cache defect; a read of nothing is still a miss.

## The gateway

**An empty `{}` body cannot probe whether a route exists.** A gateway may validate the request
body *before* resolving the route, so an absent path answers `400` to `{}` and `404` to a
shape-valid body. A probe reading "not 404 means present" reports absent surfaces as served.
*Probe with a minimally valid body* — see `probe_request` in
[`surfaces/base.py`](../src/llmdv/surfaces/base.py). A valid body alone is not proof of route
resolution: an EPP may still answer an unknown route with 400. Only a 2xx answer proves the
surface is served; contract probes use that rule for both declarations.

**A gateway may mint its own `x-request-id`** rather than propagating the client's. Where it
does, *correlate pod logs by timestamp, not by request id*, and declare `x-request-id` as
`ignored`, not `echoed`, at that layer.

**Render endpoints are served inconsistently across layers.** The same path can be absent at
the engine and absent-but-differently-rejected at the gateway. Probe each layer; do not
generalise from one.

**An error loses its envelope when the endpoint picker is the one rejecting.** Requests that fail
*body extraction* at the llm-d EPP come back as plain text with `content-type: text/plain` —
`inference error: BadRequest - error extracting request body: ...` — and no client SDK can parse
them. Everything the EPP forwards keeps a normal envelope, including a bad `max_tokens`, a prompt
over the context length and an unknown model.

So the split is **which component rejected the request**, not engine versus gateway. Reading it as
"the gateway mangles errors" is wrong and will mislead the next person. As of llm-d-router
v0.11.0, the body is built unconditionally in `pkg/common/error/error.go`
(`ir.Body = []byte(err.Error())`), with no configuration knob. *Do not expect a flag to fix it.* The
profile declares the envelope as correct; the classes go under `unstructured_errors` only after
the failure has been triaged.

**A gateway load-balances across replicas.** Direct access reaches one replica; the gateway
may reach any. A difference between the two is not necessarily the gateway — record which
endpoint served the request (`served_by`) before attributing anything.

**Only Chat returns prompt logprobs through a LiteLLM gateway.** Chat `prompt_logprobs: 0`
returns one entry per prompt position, the first null and each other holding the prompt token
itself with its `decoded_token`, so the rendered prompt can be rebuilt; it is how
`effort_rendering` reads the effort the model was given. Responses and Messages accept the field
and answer 200 with no `prompt_logprobs` at all, and Responses `enable_response_messages`
returns `input_messages: null`. *Declare `effort_rendering` on Chat only* behind LiteLLM; the
other two need another instrument, such as a render route. See
[`profiles/REFERENCE.md`](../profiles/REFERENCE.md#effort_rendering).

## The proxy layer

**One header can have a different disposition at each layer.** The same request header can be
`ignored` at the engine, `consumed` at the gateway and `rewritten` at a proxy that replaces the
client's value with its own. *Only the first is client-observable*; the others need an
instrumented backend or a load experiment, and must skip rather than pass.

**A client can poison its own prefix through a forwarded header.** Some client versions put a
per-request value at the head of their system prompt -- a billing header carrying a hex field
that changes every call. A proxy that forwards it verbatim lands it first in the rendered
prompt, and the reusable prefix ends a couple of hundred tokens in. Every response is correct;
the only symptom is cross-turn cache reuse collapsing to near zero where it should be almost
total. *Header forwarding is not neutral: a header that reaches the prompt is part of the
prefix.* Note the behaviour is client-**version**-dependent, so one pinned client proves nothing
about the others.

**Readiness and liveness differ in LiteLLM.** With custom auth, `/health/readiness` passes
through authentication and can only ever `401` for an unauthenticated probe, while
`/health/liveliness` is auth-exempt. *Declare the auth-exempt path as `health_path`.*

**A proxy version can change the response shape.** A proxy release can drop streaming reasoning
deltas outright, so requests that spent their budget reasoning stream zero content. That
presents as a high error rate under load, not as a protocol error. *Pin SDK and proxy versions,
and assert output presence rather than trusting a 200.*

## The cluster

**Pod names rot.** A pod carries a generated suffix that changes on every reschedule. *Declare
a label selector and resolve it at run time*; never commit a pod name.

**Prefill and decode serve different ports.** In a disaggregated deployment they are separate
ports on separate pods. Declare them as separate ingresses if you want both tested.

**A Namespace cannot be read under a namespaced Role.** Namespaces are cluster-scoped, so a
Role — what a scheduled job typically gets — can never grant `get namespace`, even when every
read llmdv needs is allowed. *Check access with a namespaced read* (llmdv lists pods with a field
selector that matches nothing).

**A Service is the bridge from an ingress to its pods.** Log collection resolves a Service to
pods through the Service's own selector.

**`kubectl logs` looks up the Pod before reading `pods/log`.** A missing Pod returns NotFound
without ever checking log permission. *Probe the log subresource directly with `get --raw`*;
only its response can distinguish an allowed log read from a Role that grants pods alone.

## Vantage point

**A proxy hop silently changes results.** Over a VPN or a corporate HTTP proxy, requests can
share one hop which becomes what you measure. *Declare `setup.proxy: bypass` and let preflight
fail when an HTTP proxy would capture a localhost request.*

**A dead port-forward is not a deployment failure.** It is the most common cause of a red run
from a laptop. *Preflight catches it and exits 3, separately from test failures at 1.*

**A kubeconfig can hold the wrong context.** A profile naming a kubeconfig that lacks the
declared context degrades selector resolution silently to an unresolved label. If
`llmdv profile show --setup` prints `-l <selector>` instead of `pod/<name>`, that is the symptom.

---

## Where this suite is blind

Known gaps, recorded so they are not rediscovered as surprises. Each is a consequence of a
correct decision, which is what makes them easy to miss.

**A loosening creates a blind spot, and the blind spot belongs next to the loosening.**
`assert_has_output` counts reasoning as output so a parser-less deployment is not failed for an
empty `content`. `assert_healthy_finish` accepts `length`, because it is a legitimate terminal
reason. Both are right. Together they return **pass** for a response carrying reasoning, no
answer and `finish_reason: length`. When you loosen an assertion to stop a false positive, write
down what the looser version can no longer see.

**Budget sufficiency is checked only where a budget is declared.** The answer-budget cases look
for an answer at the profile's `model.answer_max_tokens`. Without that declaration, whether the
deployment's budget leaves room to answer goes unchecked.

**No composed case judges reuse across a growing conversation.** The cache cases judge a repeat
against its first request, the render cases compare renders, and the real-client case follows
one client's continuation. None of them sees a prefix that grows turn by turn or moves between
APIs. This is the class that gets found in benchmarking instead, because every individual
response is correct.

**A composed request cannot find what a real client sends.** Every case builds its own body with
httpx or a pinned SDK, so a header or prompt preamble a client binary injects is unreachable by
construction -- and client behaviour varies by version.

---

## Tooling pitfalls

Not about the stack, but each has cost time.

- **pytest's faulthandler calls `sys.stderr.fileno()` and does not catch `OSError`.** A
  stdout/stderr replacement must return a real descriptor or the session aborts with
  `INTERNALERROR`.
- **An editable install leaves a discoverable `egg-info`**, so pytest auto-loads the plugin
  from its entry point. Passing `-p llmdv.plugin` as well aborts with "Plugin already registered
  under a different name". Check before loading it.
- **Padding a coloured string misaligns columns.** `%-9s` counts escape sequences as visible
  width. Pad before colouring.
- **Inline JSON in a shell probe is how a probe lies.** Quoting inside a double-quoted string, a
  loop variable or a shell function mangles the body; the server answers 400 because it could not
  parse what arrived, and that 400 reads as the behaviour under test. A probe built this way can
  "show" that unknown request fields are rejected, when vLLM in fact ignores them and returns
  200. *Write the body to a file and send `-d @body.json`.* When an ad-hoc probe and a recorded
  test exchange disagree, the recorded exchange wins — it captured the exact bytes sent.
- **`curl ... | head` truncates the capture.** `head` exits, SIGPIPE kills curl, and you get a
  partial stream that looks like a server bug. Redirect to a file first.
