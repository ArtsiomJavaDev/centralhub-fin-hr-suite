#!/usr/bin/env python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crm.settings import load_crm_settings
from crm.tunnel import SshTunnel
from crm.mysql_client import connect_mysql

s = load_crm_settings()
t = SshTunnel(s)
t.start()
with connect_mysql(s) as conn:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
              COUNT(*) total,
              SUM(e.pesel_number IS NOT NULL AND e.pesel_number <> '') with_pesel
            FROM bills b
            LEFT JOIN contracts_zlicen cz ON b.contract_id=cz.id AND b.contract_type LIKE '%%Zlicen%%'
            LEFT JOIN contracts_dzielo cd ON b.contract_id=cd.id AND b.contract_type LIKE '%%Dzielo%%'
            LEFT JOIN employees e ON e.id = COALESCE(cz.employee_id, cd.employee_id)
            WHERE YEAR(b.account_till)=2026 AND MONTH(b.account_till)=4
        """)
        print("join via contract.employee_id:", cur.fetchone())
        cur.execute("""
            SELECT cz.employee_id, cz.client_id, cz.is_for_employee, e.pesel_number
            FROM contracts_zlicen cz
            LEFT JOIN employees e ON e.id=cz.employee_id
            LIMIT 5
        """)
        print("contracts_zlicen sample:")
        for r in cur.fetchall():
            print(r)
        cur.execute("""
            SELECT b.id, b.bill_number, b.contract_id, b.contract_type,
                   cz.employee_id, cd.employee_id
            FROM bills b
            LEFT JOIN contracts_zlicen cz ON b.contract_id=cz.id AND b.contract_type LIKE '%%Zlicen%%'
            LEFT JOIN contracts_dzielo cd ON b.contract_id=cd.id AND b.contract_type LIKE '%%Dzielo%%'
            WHERE YEAR(b.account_till)=2026 AND MONTH(b.account_till)=4
              AND COALESCE(cz.employee_id, cd.employee_id) IS NOT NULL
            LIMIT 5
        """)
        print("bills with employee_id on contract:")
        for r in cur.fetchall():
            print(r)
t.stop()
