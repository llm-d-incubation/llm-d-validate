# Image input (`images`)

An image is read, not only accepted: streamed, 2.4 MB, two in order, counted by `count_tokens`; an internal image URL is refused.

- **Cases:** 8 (see [docs/coverage.md](../../../../docs/coverage.md))
- **Enabled by:** `vision`; `count_tokens`, `error_status.internal_image_url`
- **Run only this suite:** `llmdv verify --profile <profile> -m images`
