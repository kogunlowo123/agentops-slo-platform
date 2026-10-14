# Contributing

## Setup

```bash
uv sync
make check
```

## Requirements for every change

- `make check` passes: ruff, black, mypy `--strict`, bandit, contract drift check, tests
  with at least 90% branch coverage.
- New behaviour has tests. Security controls need adversarial cases in `tests/security`;
  dependency failure handling needs a case in `tests/chaos`.
- Changes to request or response models are followed by
  `uv run python -m agentops_slo_platform.contracts` and the regenerated `schemas/` and
  `docs/api/` files are committed.
- Public API changes are recorded in `CHANGELOG.md`.
- Architecturally significant decisions get an ADR in `docs/architecture/adr/`.
- No credentials, customer data, prompts or PHI in code, tests or fixtures. Use synthetic
  telemetry only.

## Commit messages

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
Breaking changes use `!` and a `BREAKING CHANGE:` footer.

## Review

Changes to `src/agentops_slo_platform/security`, `policies/`, `deployments/` or
`.github/` need approval from a code owner (see `CODEOWNERS`).
