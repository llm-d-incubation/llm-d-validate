# Unhappy paths (`unhappy`)

Bad input, credentials and error envelopes get a clean 4xx.

- **Cases:** 28 (see [docs/coverage.md](../../../../docs/coverage.md))
- **Enabled by:** every surface; `error_status`, `unstructured_errors`
- **Run only this suite:** `llmdv verify --profile <profile> -m unhappy`
