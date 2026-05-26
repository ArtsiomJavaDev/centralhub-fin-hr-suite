# Changelog

All notable changes to CentralHub should be documented here.

The project follows a pragmatic versioning model for internal desktop releases:

- `MAJOR` for incompatible database/import behavior changes,
- `MINOR` for new workflows or visible capabilities,
- `PATCH` for fixes, refactors, and test-only improvements.

## Unreleased

### Added

- Golden-case pytest coverage for 2026 tax calculations and CRM financial checks.
- Shared PESEL/date utility module.
- CI workflow and repository hygiene files.

### Changed

- Refactored pure column utilities out of `main.py`.
- Refactored pure financial calculations out of `db/service.py`.
- Pinned runtime dependency versions in `requirements.txt`.

### Fixed

- Preserve explicit `0%` PIT rates in CRM financial verification instead of
  falling back to the default `12%`.

