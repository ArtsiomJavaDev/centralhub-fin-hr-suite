# Contributing

CentralHub / ImporterWaPro is a private payroll automation tool. Treat all CRM,
WaPro, employee, PESEL, payroll, log, and export data as sensitive.

## Development Flow

1. Create a short-lived branch from `main`:

   ```powershell
   git switch -c feature/short-description
   ```

2. Keep changes focused. Prefer small pull requests that can be reviewed in one
   pass.
3. Add or update tests when changing:
   - tax / ZUS calculations,
   - import mappings,
   - CRM formatting,
   - rollback or database write behavior.
4. Run the test suite before opening a pull request:

   ```powershell
   pytest
   ```

5. Open a pull request into `main`. Do not commit secrets, real data, generated
   Excel files, or logs.

## Branch Naming

Use one of these prefixes:

- `feature/...` for new capabilities or planned improvements.
- `fix/...` for defects.
- `refactor/...` for internal code structure changes.
- `test/...` for test-only work.
- `docs/...` for documentation-only work.
- `hotfix/...` for urgent production fixes.

Examples:

- `feature/crm-api-retry`
- `fix/pit-zero-verification`
- `refactor/db-financials`
- `test/tax-calc-2026-golden-cases`

## Commit Style

Use concise imperative messages:

- `Fix PIT zero-rate verification`
- `Add golden tests for 2026 tax calculations`
- `Extract PESEL utilities`

## Security Rules

Never commit:

- `config.ini`, `private.py`, `_secrets.py` with real values,
- API tokens, passwords, SSH keys, hostnames, or production IPs,
- PESEL values, payroll exports, CRM dumps, logs, or screenshots with personal data.

Use anonymized fixtures in tests.

