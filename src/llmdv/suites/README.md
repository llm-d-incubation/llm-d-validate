# suites

The functional tests. Each package is one suite, and its name is also its pytest marker
(`llmdv verify -m <suite>`); a case whose `case_id` prefix names another suite carries that
marker too. What each suite checks, the profile keys that enable it, its case count and its
status are in [docs/coverage.md](../../../docs/coverage.md#coverage).

Each suite has its own README:
[`anthropic_messages`](anthropic_messages/),
[`cache`](cache/),
[`contract`](contract/),
[`discovery`](discovery/),
[`evolution`](evolution/),
[`field_options`](field_options/),
[`fleet`](fleet/),
[`images`](images/),
[`long_context`](long_context/),
[`openai_chat`](openai_chat/),
[`openai_completions`](openai_completions/),
[`openai_responses`](openai_responses/),
[`real_clients`](real_clients/),
[`render`](render/),
[`scenarios`](scenarios/),
[`streaming`](streaming/),
[`tools`](tools/),
[`transport`](transport/),
[`unhappy`](unhappy/).

`conftest.py` holds fixtures every suite shares.
