# Discovery (`discovery`)

`/v1/models` lists the model; unknown models get no fallback; retrieval failures retain JSON, text or empty-body evidence.

- **Cases:** 7 (see [docs/coverage.md](../../../../docs/coverage.md))
- **Enabled by:** any surface; `model_retrieval`, `model_info`
- **Run only this suite:** `llmdv verify --profile <profile> -m discovery`
