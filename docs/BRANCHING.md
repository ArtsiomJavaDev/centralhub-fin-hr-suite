# Branching Model

This repository uses a lightweight GitHub Flow model suitable for an internal
desktop payroll tool.

## Long-Lived Branches

| Branch | Purpose |
| --- | --- |
| `main` | Stable, releasable state. Code here should pass CI and be safe to package. |

`develop`, `staging`, and `production` branches are intentionally not required
for now. Add `develop` only if release coordination becomes too noisy on
short-lived branches.

## Short-Lived Branches

Create a branch per focused change:

| Prefix | Use |
| --- | --- |
| `feature/` | New workflow or capability |
| `fix/` | Bug fix |
| `refactor/` | Internal structure change without intended behavior change |
| `test/` | Test coverage only |
| `docs/` | Documentation only |
| `hotfix/` | Urgent fix for the currently deployed version |

Examples:

- `feature/crm-api-pagination`
- `fix/pit-zero-rate`
- `refactor/db-financials`
- `test/checker-golden-cases`

## Pull Request Rules

Every pull request into `main` should include:

- a clear summary,
- test results (`pytest` at minimum),
- a risk level,
- confirmation that no secrets, logs, payroll exports, or real employee data are
  included.

## Recommended GitHub Branch Protection

In GitHub repository settings, protect `main` with:

- Require a pull request before merging.
- Require status checks to pass before merging.
- Require the `CI / Tests / Python 3.12` check.
- Block force pushes.
- Block deletions.

For a solo-maintained private project, requiring one approval is optional. If a
second reviewer exists, enable it.

## Release Tags

When packaging an application build for accounting users, tag the commit on
`main`:

```powershell
git tag v0.2.0
git push origin v0.2.0
```

Keep release notes in `CHANGELOG.md`.

