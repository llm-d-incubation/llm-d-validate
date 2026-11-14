# Contributing to llm-d-validate

## Governance Structure

`llm-d-validate` is developed by:

- A community of **contributors** who file issues, share ideas and submit pull requests
- A group of **core maintainers** who guide `llm-d-validate` overall and help drive its development

We ask that contributions follow the `llm-d-validate` design principles and best practices, and the core maintainers are here to help with that. We truly appreciate every pull request. Maintainers balance reviews with their own project work, so a review may sometimes take a little while; thank you for your patience.

The best way to help a pull request move quickly is to keep it focused, well tested and clearly described.

### Core maintainers

The core maintainers lead the development of `llm-d-validate` and shape the functional validation strategy for deployed `llm-d` stacks. Their responsibilities include:

- Proposing, implementing and reviewing test cases, surfaces, ingress kinds, infra-profile fields and the run evidence `llm-d-validate` records
- Keeping verdicts trustworthy: a case passes only when the stack behaves as the API defines, and every skip states why it skipped
- Supporting code quality and the project's core design principles

The core maintainers aim to make decisions in the open and to share the reasoning behind them, including how disagreements are resolved.

The list of core maintainers can be found in the [OWNERS](OWNERS) file.

### How Changes Are Reviewed

#### Straightforward Changes

Bug fixes that come with focused offline tests are always welcome. A fix to a case that passes or fails incorrectly is easiest to review when it shows the case failing against an injected defect, and passing without it. New cases for behaviour an API already defines are welcome too; maintainers will weigh how much a change adds against the complexity it brings. Design changes beyond fixes and new coverage are best discussed first, as described below.

#### Changes That Benefit From Discussion

Some changes affect how people already use `llm-d-validate`, for example changing what an existing case asserts, or changes to the infra-profile schema, the run directory layout, exit codes or verdict semantics. For these, please open an issue first so the community and maintainers can talk it through together. Maintainers consider each one on its own merits and will share their reasoning.

## Submitting a Pull Request

We welcome contributions to any aspect of `llm-d-validate`! If you have a bug fix, feature request, or improvement, please submit a pull request (PR) to the repository. For a larger change, open an issue first to discuss it.

For every Pull Request submitted, ensure the following steps have been done:

1. [Sign your commits](https://docs.github.com/en/authentication/managing-commit-signature-verification/signing-commits), and add a DCO sign-off to each one with `git commit -s`. See [PR_SIGNOFF.md](PR_SIGNOFF.md) for details.
2. Make sure the offline tests and linters pass **before you push** the contents of your PR:

   ```sh
   python -m pytest tests/ -q
   ruff check src/ tests/ && ruff format --check src/ tests/
   ```

3. Never commit credentials, hostnames, namespaces or other cluster detail. Keep machine-specific profiles in a gitignored `*.local.yaml` copy.

## Community and Communication

- **Developer Slack:** [Join our developer Slack workspace](https://llm-d.ai/slack) to connect with the core maintainers and other contributors, ask questions, and participate in discussions.
- **Weekly Meetings:** Project updates, ongoing work discussions, and Q&A will be covered in our weekly project meetings. Please join by [adding the shared calendar](https://red.ht/llm-d-public-calendar). You can also [join our Google Group](https://groups.google.com/g/llm-d-contributors) for access to shared content.
- **Code:** Hosted in the [llm-d](https://github.com/llm-d) GitHub organization
- **Issues:** Project-scoped bugs or issues should be reported in this repo or in [llm-d/llm-d](https://github.com/llm-d/llm-d)
- **Mailing List:** [llm-d-contributors@googlegroups.com](mailto:llm-d-contributors@googlegroups.com)

This project adheres to the [Code of Conduct](CODE_OF_CONDUCT.md). See [SECURITY.md](SECURITY.md) for our vulnerability disclosure process.

## License

By contributing, you agree that your contributions are licensed under the [Apache 2.0 license](LICENSE).
