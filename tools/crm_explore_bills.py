#!/usr/bin/env python
"""Sample bills + joins to map UDUZ04 fields."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crm.mysql_client import connect_mysql
from crm.settings import load_crm_settings
from crm.tunnel import SshTunnel

QUERIES = [
    ("contract_type values", "SELECT contract_type, COUNT(*) c FROM bills GROUP BY contract_type ORDER BY c DESC"),
    (
        "sample bills 2026",
        """
        SELECT b.id, b.bill_number, b.contract_type, b.contract_id,
               b.account_from, b.account_till, b.netto_amount, b.brutto_amount,
               b.vat, b.kup, b.is_student,
               b.zus_emerytalne, b.zus_chorobowe, b.zus_zdrowotne
        FROM bills b
        WHERE YEAR(b.account_till) = 2026 AND MONTH(b.account_till) = 4
        LIMIT 5
        """,
    ),
    (
        "bills + zlecenie + employee",
        """
        SELECT b.bill_number, b.contract_type, b.netto_amount, b.brutto_amount, b.kup, b.vat,
               b.account_from, b.account_till,
               cz.number AS umowa_number, cz.start_date AS umowa_start,
               e.name, e.surname, e.pesel_number
        FROM bills b
        JOIN contracts_zlicen cz ON cz.id = b.contract_id AND b.contract_type LIKE '%zlec%'
        JOIN employees e ON e.id = cz.employee_id
        WHERE b.deleted_at IS NULL OR 1=1
        LIMIT 3
        """,
    ),
]


def main() -> int:
    settings = load_crm_settings()
    tunnel = SshTunnel(settings)
    tunnel.start()
    try:
        with connect_mysql(settings) as conn:
            with conn.cursor() as cur:
                for title, sql in QUERIES:
                    print("\n" + "=" * 60)
                    print(title)
                    print("=" * 60)
                    try:
                        cur.execute(sql)
                        rows = cur.fetchall()
                        for r in rows:
                            print(r)
                    except Exception as exc:
                        print("ERROR:", exc)
    finally:
        tunnel.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
