# Contributing to llm-d-validate

## Governance Structure

`llm-d-validate` adopts the following hierarchical technical governance structure:

- A community of **contributors** who file issues and submit pull requests
- A body of **core maintainers** who own `llm-d-validate` overall and drive its development

All contributions are expected to follow `llm-d-validate` design principles and best practices, as enforced by core maintainers. While high-quality pull requests are appreciated and encouraged, all maintainers reserve the right to prioritize their own work over code reviews at-will, hence contributors should not expect their work to be reviewed promptly.

Contributors can maximize the chances of their work being accepted by maintainers by meeting a high quality bar before sending a PR to maintainers.

### Core maintainers

The core maintainers lead the development of `llm-d-validate` and define the functional validation strategy for deployed `llm-d` stacks. Their responsibilities include:

- Proposing, implementing and reviewing test cases, surfaces, ingress kinds, infra-profile fields and the run evidence `llm-d-validate` records
- Keeping verdicts trustworthy: a case passes only when the stack behaves as the API defines, and every skip states why it skipped
- Enforcing code quality standards and adherence to core design principles

The core maintainers should publicly articulate their decision-making, and share the reasoning behind their decisions, vetoes, and dispute resolution.

The list of core maintainers can be found in the [OWNERS](OWNERS) file.

### Decision Making

#### Uncontroversial Changes

We are committed to accepting functional bug fixes that meet our quality standards and include minimized offline tests to avoid future regressions. A fix to a case that passes or fails wrongly should show the case failing against an injected defect, and passing without it. New cases for behaviour an API already defines generally fall under the same category, with the caveat that they may be rejected if the trade-off between usefulness and complexity is deemed unfavorable by core maintainers. Design changes that neither fix known functional issues nor add coverage are automatically considered controversial.

#### Controversial Changes

More controversial design changes (e.g., changing what an existing case asserts, breaking changes to the infra-profile schema, the run directory layout, exit codes or verdict semantics) are evaluated on a case-by-case basis under the subjective judgment of core maintainers.

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
