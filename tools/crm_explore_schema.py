#!/usr/bin/env python
"""Explore CRM MySQL schema — columns and sample rows for report SQL."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crm.mysql_client import connect_mysql, execute_query
from crm.settings import load_crm_settings
from crm.tunnel import SshTunnel

TABLES = [
    "contracts_zlicen",
    "contracts_dzielo",
    "act_contract_dzielo",
    "employees",
    "customers",
    "process_salaries",
    "salary_requests",
    "bills",
    "reports",
    "work_results",
]


def main() -> int:
    settings = load_crm_settings()
    errors = settings.validate()
    if errors:
        print("Config errors:", "; ".join(errors))
        return 1

    tunnel = SshTunnel(settings)
    tunnel.start()
    try:
        with connect_mysql(settings) as conn:
            with conn.cursor() as cur:
                for table in TABLES:
                    print("\n" + "=" * 70)
                    print(f"TABLE: {table}")
                    print("=" * 70)
                    cur.execute(f"SHOW COLUMNS FROM `{table}`")
                    cols = cur.fetchall()
                    for c in cols:
                        print(f"  {c['Field']:40} {c['Type']:25} {c.get('Null','')}")

                    cur.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
                    n = cur.fetchone()["n"]
                    print(f"  --- rows: {n}")

                    if n > 0:
                        cur.execute(f"SELECT * FROM `{table}` LIMIT 2")
                        rows = cur.fetchall()
                        if rows:
                            print("  --- sample keys:", ", ".join(rows[0].keys())[:120])
    finally:
        tunnel.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
