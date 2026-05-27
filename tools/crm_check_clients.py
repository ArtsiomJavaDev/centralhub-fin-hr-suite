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
        cur.execute("SHOW COLUMNS FROM customers")
        print("customers columns:", [c["Field"] for c in cur.fetchall()])
        cur.execute("""
            SELECT COUNT(*) n FROM bills b
            JOIN contracts_dzielo cd ON cd.id=b.contract_id AND b.contract_type LIKE '%%Dzielo%%'
            WHERE cd.client_id IS NOT NULL AND YEAR(b.account_till)=2026 AND MONTH(b.account_till)=4
        """)
        print("dzielo bills with client_id:", cur.fetchone())
        cur.execute("""
            SELECT b.bill_number, cd.client_id, c.first_name, c.last_name, c.tax_number, c.type
            FROM bills b
            JOIN contracts_dzielo cd ON cd.id=b.contract_id AND b.contract_type LIKE '%%Dzielo%%'
            LEFT JOIN customers c ON c.id = cd.client_id
            WHERE YEAR(b.account_till)=2026 AND MONTH(b.account_till)=4
            LIMIT 8
        """)
        for r in cur.fetchall():
            print(r)
t.stop()
