# Security Policy

CentralHub handles payroll-adjacent data and integrates with
CRM, payroll SQL Server, and secure credential storage. Security issues should be
treated as private by default.

## Sensitive Data

Do not include any of the following in issues, pull requests, screenshots, logs,
fixtures, or commits:

- employee names, PESEL numbers, addresses, contract numbers, or payroll amounts,
- CRM exports, Polish HR and payroll system exports, generated Excel files, import logs, or crash logs,
- server addresses, hostnames, SSH usernames, SSH keys, database credentials,
- API tokens, encrypted blobs from another machine, or `config.ini` contents.

## Reporting

Report security-sensitive findings privately to the repository owner. Do not
open a public issue with exploit details or real operational data.

## Local Secret Handling

Secrets are expected to live outside tracked source code:

- `config.ini` is ignored by git,
- `private.py` is ignored by git,
- credentials should be stored via Windows DPAPI-backed helpers,
- SSH private keys should remain in the user's SSH directory, not in this repo.

## Supported Environment

The production target is Windows with Python 3.11+ / 3.12, ODBC Driver 17 for SQL
Server, and access to approved CRM and payroll system resources.

