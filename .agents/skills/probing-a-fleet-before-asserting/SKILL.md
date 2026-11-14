---
name: probing-a-fleet-before-asserting
description: Use when writing or changing an llmdv assertion about a response from deployed serving infrastructure (vLLM, an llm-d router or EPP, LiteLLM), when a live case fails and you are tempted to adjust the assertion, or when you are about to rely on a spec or SDK for a status, field or stream shape you have not seen.
---

# Probing a fleet before asserting

The recurring failure here is an assertion that is plausible, reads correctly in review, and is wrong about the stack. Sending one request catches it; reasoning about the request does not.

**Never write an assertion about a field you have not seen.** Not the status, not the field name, not whether it is populated, not where it appears in a stream.

## Procedure

1. **Reach the layer directly.** For a layer behind a port-forward, `llmdv profile show <profile> --setup` prints the forwards with pods resolved live. For a gateway or a router route, export the profile's `base_url_env` and `auth.key_env` variables instead (`--setup` then prints "no setup required"); the testing-a-model-deployment skill shows the credential mapping.

2. **Send the real request and keep the whole response.** Body in a file, output to a file:
   ```sh
   cat > body.json <<'JSON'
   {"model":"<model>","messages":[{"role":"user","content":"hi"}],"max_tokens":16}
   JSON
   curl -s --max-time 120 "$<BASE_URL_ENV>/v1/chat/completions" \
     -H "Authorization: Bearer $<KEY_ENV>" \
     -H 'Content-Type: application/json' -d @body.json -D headers.txt > probe.json
   ```
   Drop the `Authorization` line for a router reached directly; it authenticates nothing. The best capture is often a recorded one: `llmdv verify --profile <profile> --evidence all -k <case>` writes every exchange to `exchanges.jsonl`.

3. **Probe every declared layer, not one.** `vllm`, `llmd_router` and `litellm` ingresses disagree on error status, on which paths exist, on which headers come back, and on whether a response is transformed.

4. **For a stream, capture the raw frames.** Count them, find which carry a finish reason, check for the terminal sentinel, and run it with and without any option you will assert on. The absence case is as much a contract as the presence case.

5. **Write the capture into a fixture** under `tests/fixtures/`. A real capture holds the parser to what a server sends, vendor fields included; a hand-written one tests your assumptions. Strip internal hostnames and Service names first (`x-litellm-model-api-base`, `x-envoy-decorator-operation`, backend headers): a fixture is a committed file.

6. **Write the assertion**, recording what it observed through `journal.record` so the verdict carries its evidence.

7. **Prove it can fail.** Add the matching defect to the fake fleet (see extending-llmdv for which file) and assert the case fails against it.

On a fleet declared `shared`, keep probes to well-formed requests: malformed bodies, huge prompts and disconnects belong to the disruptive families the policy excludes.

## When a case fails

Find out which side is wrong before changing anything. The behaviour may also be legitimate and differ by layer. Do not loosen the assertion until it passes everywhere: keep the case failing for the team to triage, and declare a deviation only after triage, as writing-an-infra-profile describes.

## What to check

- Is the terminal condition a status, a field, or a sentinel frame?
- Is the field populated, or is the content inline in another field?
- Does the layer add or strip anything?
- Does an absent route answer 404, or does body validation answer 400 first? Only a 2xx proves a route exists: an EPP can answer an unknown route with 400.
- Does the response differ between layers?

## Red flags

| Thought | Reality |
|---|---|
| "The OpenAI spec says this field is present" | The spec is not the fleet. Probe it. |
| "It worked in the non-streaming case" | Streaming has its own grammar and its own failures. |
| "The test fails, so loosen the assertion" | Find out which side is wrong first. Loosening is how a suite stops catching things. |
| "My probe already told me what it returns" | A probe is an instrument and instruments lie. When an ad-hoc curl and a recorded test exchange disagree, the recorded exchange wins. |
| "I'll assert the status, that's enough" | A draining replica returns a good status with an abort reason. |
| "I loosened it so a healthy fleet stops failing" | Now write down what the looser assertion can no longer see. Two sound loosenings can combine into a blind spot. |
| "Every response was correct, so the stack is fine" | Some defects live between responses: a prefix that moves, reuse that collapses. |
| "This fleet has it, so fleets have it" | Capability varies per fleet and per role. Declare it in the profile and gate the case. |
