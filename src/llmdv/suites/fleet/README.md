# Fleet invariants (`fleet`)

Config reads, Prometheus bounds, forbidden log lines.

- **Cases:** 10 (see [docs/coverage.md](../../../../docs/coverage.md))
- **Enabled by:** `fleet.config`, `fleet.metrics`, `ingresses[].logs`
- **Run only this suite:** `llmdv verify --profile <profile> -m fleet`
