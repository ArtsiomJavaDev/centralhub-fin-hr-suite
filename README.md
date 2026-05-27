<div align="center">

# ⚡ CentralHub — Polish HR & Payroll Automation

### The payroll data bridge that saves **$30,000 / year**

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://python.org)
[![PyQt6](https://img.shields.io/badge/GUI-PyQt6-41CD52?logo=qt&logoColor=white)](https://pypi.org/project/PyQt6/)
[![SQL Server](https://img.shields.io/badge/DB-SQL%20Server-CC2927?logo=microsoftsqlserver&logoColor=white)](https://www.microsoft.com/sql-server)
[![MySQL](https://img.shields.io/badge/CRM%20DB-MySQL-4479A1?logo=mysql&logoColor=white)](https://mysql.com)
[![AWS](https://img.shields.io/badge/Tunnel-AWS%20RDS-FF9900?logo=amazonaws&logoColor=white)](https://aws.amazon.com/rds/)
[![License](https://img.shields.io/badge/License-Private-red)](.)

</div>

---

## The Problem We Solved

Before CentralHub, payroll processing looked like this:

> **Accountant opens Polish HR and payroll system → manually re-types every contract, employee address, and ZUS rate from CRM → prays nothing was mistyped → sends to ZUS → gets rejection → finds the typo → repeats.**

With hundreds of employees on civil-law contracts (umowa o dzieło / umowa zlecenie) each month, this manual pipeline cost the company **a full-time position's worth of hours** — roughly **$30,000 per year** in labor.

The root of the problem was data living in two completely separate systems:

| System | What it holds | Who maintains it |
|--------|--------------|-----------------|
| **CRM** (internal) | Employee records, contracts, billing, PESEL, addresses | The whole team — data is verified collaboratively |
| **Polish HR and payroll system** (SQL Server) | Payroll engine — ZUS declarations, PIT, salary payments | Accountants only |

Data flowed from CRM to Polish HR and payroll system through **copy-paste and manual re-entry**. Every month. For every employee.

**CentralHub killed that workflow entirely.**

---

## What CentralHub Does

CentralHub is a desktop application that acts as an intelligent bridge between the company CRM and the Polish HR and payroll database. It pulls verified data from the CRM — where the whole team has already done the accuracy work — formats it to the payroll system's exact schema, validates every number, and imports it in seconds.

```
CRM (MySQL/API)  ──────►  CentralHub  ──────►  Polish HR and payroll system (SQL Server)
  Verified by the team        ↕                  Payroll engine
                         Validates &
                         recalculates
                         every PLN
```

The key insight: **the team already verifies the data in CRM**. CentralHub just makes sure that verified data lands in the target payroll system correctly — no re-entry, no mistyping, no month-end panic.

---

## Key Features

### 🔄 Full Automation Pipeline
One-click flow: connect to CRM → fetch monthly report → verify → import to a Polish HR and payroll system. Handles hundreds of contracts in the time it used to take to process one.

### 🧮 Financial Verification Engine
Before touching the payroll DB, CentralHub independently recalculates every contract using **2026 Polish tax law** (ZUS, PIT, zdrowotne, FP, FGŚP) and compares the result against CRM source values:
- Difference ≤ 0.05 PLN → **OK**
- Difference ≤ 1.05 PLN → **Marginal** (known payroll-system/CRM rounding boundary)
- Larger difference → **Discrepancy flagged** with auto-diagnosed cause

This catches data issues *before* they become ZUS rejections.

### 📋 Multi-Profile Excel Import
Import from Excel with full validation across 8 profiles:

| Profile | What it imports |
|---------|----------------|
| **Employees** | Full employee records with addresses & tax office |
| **Employee Addresses** | Address updates by PESEL |
| **Relocations** | Address + Urząd Skarbowy changes |
| **Umowa Zlecenie** | Service contracts with full ZUS breakdown |
| **Umowa o Dzieło** | Copyright contracts (no ZUS) |
| **Mixed Contracts** | UZ + UD in a single file |
| **Insurance** | Mandatory insurance obligations |
| **Tax Office Link** | Urząd Skarbowy assignment by employee ID |

### 🛡️ Pre-Import Safeguards
- **PESEL batch lookup** — verifies every PESEL exists in Polish HR and payroll system before importing
- **Duplicate detection** — blocks re-importing a period already in the DB
- **Dry-run mode** — full simulation with per-row status table, zero DB writes
- **Rollback** — undo any import, either from the current session or from history

### 🔐 Secure Credential Storage
All passwords and API tokens are encrypted with **Windows DPAPI** and stored in `config.ini`. No plaintext secrets anywhere in the codebase.

### 🌐 CRM Integration (SSH Tunnel + REST API)
Connects to the CRM MySQL database through an **SSH tunnel to AWS RDS**, or fetches data directly from the **CRM REST API** — whichever source has the freshest data. Supports both data sources simultaneously, merging them into a single import batch.

### 📊 Live DB Overview
Browse the payroll database without opening SQL Server Management Studio — view employee lists, contract stats, and import history directly in the app.

### 🗂️ PPK Support
Automatically calculates and attaches PPK (Employee Capital Plan) contributions to contracts using paired-row merge logic.

---

## How Much It Saves

| Cost item | Before CentralHub | After CentralHub |
|-----------|------------------|-----------------|
| Monthly contract entry (hours) | ~40 hrs/month | < 5 min/month |
| ZUS rejection rate | High (manual typos) | Near-zero |
| Dedicated data-entry labor | ~1 FTE | 0 |
| **Annual labor cost** | **~$30,000** | **~$0** |

The savings come from a simple architectural decision: **let the team verify data once in CRM, then automate the rest**. CentralHub is the automation.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| GUI | PyQt6 |
| Payroll DB | Microsoft SQL Server (via pyodbc / SQLAlchemy) |
| CRM DB | MySQL on AWS RDS (via pymysql + SSH tunnel) |
| CRM API | REST/JSON (Bearer token, pagination) |
| Data processing | pandas, openpyxl |
| Credential security | Windows DPAPI (`CryptProtectData`) |
| Tax engine | Custom 2026 PL tax law implementation |

---

## Getting Started

### Prerequisites

- Windows 10/11
- Python 3.11+
- ODBC Driver 17 for SQL Server
- OpenSSH Client (Windows optional feature)
- Access to payroll SQL Server and CRM credentials

### Setup

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_ORG/CentralHub.git
cd CentralHub

# 2. Create and activate virtual environment
python -m venv .venv
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
copy config.ini.example config.ini
# Edit config.ini with your server details
```

### Configure Credentials

Passwords and tokens are never stored in plaintext. Use the setup tools:

```bash
# Set CRM MySQL password (encrypts with DPAPI)
python tools/crm_configure.py

# Set CRM API token (encrypts with DPAPI)
python tools/crm_api_configure.py --set-token
```

payroll database password is set through the app's Settings tab on first launch.

### Run

```bash
python main.py
```

### Development Workflow

```bash
# Run regression tests
pytest
```

Development changes should be made on short-lived branches such as
`feature/...`, `fix/...`, `refactor/...`, or `test/...`, then merged through a
pull request into `main`. See [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`docs/BRANCHING.md`](docs/BRANCHING.md) for the repository workflow.

---

## Project Structure

```
CentralHub/
├── main.py                  # App entry point & main window
├── secrets_store.py         # DPAPI encrypt/decrypt
├── config.ini.example       # Config template (copy → config.ini)
│
├── crm/                     # CRM integration layer
│   ├── api_client.py        # REST API client
│   ├── mysql_client.py      # MySQL/SSH tunnel client
│   ├── formatter.py         # CRM → payroll system data transformation
│   ├── checker.py           # PESEL lookup & financial verification
│   ├── settings.py          # CRM config loader
│   └── tunnel.py            # SSH tunnel manager
│
├── db/                      # payroll SQL Server layer
│   ├── service.py           # All DB operations (CRUD, import, rollback)
│   ├── tax_calc_2026.py     # 2026 Polish ZUS/PIT calculations
│   └── config_loader.py     # DB config
│
├── importer/                # Excel import engine
│   ├── profiles.py          # Import profile definitions
│   ├── checkin.py           # Pre-import row validation
│   ├── mapping.py           # Column mapping
│   └── umowy_ppk_pairs.py   # PPK contribution pairing
│
├── ui/                      # PyQt6 UI components
│   ├── automatyzacja_tab.py # Full automation pipeline tab
│   ├── db_overview_tab.py   # DB browser tab
│   ├── umowy_export_tab.py  # Contract export tab
│   ├── theme.py             # App stylesheet
│   └── i18n.py              # PL/RU translations
│
└── tools/                   # CLI configuration & diagnostic tools
    ├── crm_configure.py
    ├── crm_api_configure.py
    └── crm_check_*.py
```

---

## Security Notes

- `config.ini` is **git-ignored** — it contains encrypted credentials and server endpoints
- All secrets use **Windows DPAPI** — encrypted blobs are tied to the OS user account
- SSH private key is stored outside the project (default: `~/.ssh/id_ed25519`)
- No plaintext passwords, tokens, or server addresses anywhere in the committed code

---

<div align="center">

*Built to replace a $30,000/year manual process with a single click.*

</div>
