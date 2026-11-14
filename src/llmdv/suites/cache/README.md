# Prefix cache (`cache`)

Realised prefix reuse across turns, salts, tools, clients.

- **Cases:** 33 (see [docs/coverage.md](../../../../docs/coverage.md))
- **Enabled by:** `cache_reuse`; `vllm_options: [cache_salt]`
- **Run only this suite:** `llmdv verify --profile <profile> -m cache`
