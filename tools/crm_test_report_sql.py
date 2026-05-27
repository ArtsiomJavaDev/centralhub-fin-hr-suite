#!/usr/bin/env python
"""Test the UDUZ04 report SQL against CRM."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crm.mysql_client import execute_query, fetch_report_dataframe
from crm.settings import load_crm_settings
from crm.formatter import format_crm_report

REPORT_SQL = """
SELECT
    NULL AS lp,
    COALESCE(cz.number, cd.number) AS numer_umowy,
    b.bill_number AS numer_rachunku,
    CASE
        WHEN b.contract_type LIKE '%%Zlicen%%' THEN 'Umowa Zlecenie'
        WHEN b.contract_type LIKE '%%Dzielo%%' THEN 'Umowa o Dzieło'
    END AS typ,
    TRIM(CONCAT(COALESCE(e.name, ''), ' ', COALESCE(e.surname, ''))) AS pracownik,
    e.pesel_number AS pesel,
    b.netto_amount AS kwota_netto,
    b.brutto_amount AS kwota_brutto,
    CONCAT(b.kup, '%%') AS kup,
    COALESCE(cz.start_date, cd.start_date) AS data_zawarcia,
    ROUND(b.brutto_amount - b.netto_amount, 2) AS podatek,
    b.account_till AS data_wyplaty,
    NULL AS ppk
FROM bills b
LEFT JOIN contracts_zlicen cz
    ON b.contract_id = cz.id AND b.contract_type LIKE '%%Zlicen%%'
LEFT JOIN contracts_dzielo cd
    ON b.contract_id = cd.id AND b.contract_type LIKE '%%Dzielo%%'
LEFT JOIN employees e
    ON e.id = COALESCE(cz.employee_id, cd.employee_id)
WHERE YEAR(b.account_till) = %(year)s
  AND MONTH(b.account_till) = %(month)s
  AND b.brutto_amount > 0
ORDER BY b.bill_number
"""


def main() -> int:
    settings = load_crm_settings()
    # Temporarily override report_sql
    from dataclasses import replace
    settings = replace(settings, report_sql=REPORT_SQL.strip())

    print("Rows for 2026-04:")
    df = execute_query(settings, settings.report_sql, {"year": 2026, "month": 4})
    print(f"  count={len(df)}")
    print(df.head(3).to_string())

    print("\nFormat pipeline:")
    raw = fetch_report_dataframe(settings, 2026, 4)
    formatted, result = format_crm_report(raw)
    print(f"  formatted={result.total_rows} UZ={result.uz_count} UD={result.ud_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
