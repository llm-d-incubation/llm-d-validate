# Field options (`field_options`)

Structured output, JSON mode (with reasoning beside the answer where it is on by default), reasoning usage, observable vLLM options, the effort marker the template renders for each requested effort.

- **Cases:** 8 (see [docs/coverage.md](../../../../docs/coverage.md))
- **Enabled by:** `structured_output`, `json_object`, `model.reasoning`, `reasoning_tokens`, `vllm_options`, `effort_rendering`
- **Run only this suite:** `llmdv verify --profile <profile> -m field_options`
