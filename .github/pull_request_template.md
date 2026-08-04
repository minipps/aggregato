## Summary

<!-- What changed, and why? Link the issue this PR addresses. -->

## Acquisition and privacy

<!-- Required for provider/data-source changes. State which acquisition surfaces you evaluated and
     why each higher surface was insufficient. Confirm the change only accesses the operator's
     own data and does not bypass access controls, paywalls, CAPTCHAs, or anti-bot measures. -->

## Testing

- [ ] New behaviour has a deterministic test that failed before this change
- [ ] Fixtures are recorded, redacted, and contain no credentials (if applicable)
- [ ] `uv run ruff format --check . && uv run ruff check . && uv run mypy`
- [ ] `uv run lint-imports`
- [ ] `uv run pytest`, including `tests/conformance/` with any provider registered
- [ ] `cd frontend && npm run type-check && npm run test:unit` (if frontend changes)

## Data and dependencies

- [ ] `config_model` fields are documented and secrets are write-only (if applicable)
- [ ] Migrations preserve existing rows, disable foreign keys around SQLite table rebuilds, and
      do not edit an already-applied revision (if applicable)
- [ ] New dependencies are justified against the standard library, native platform features, and
      already-installed packages (if applicable)

## Review notes

<!-- Call out risks, compatibility concerns, follow-up work, or anything reviewers should focus on. -->
