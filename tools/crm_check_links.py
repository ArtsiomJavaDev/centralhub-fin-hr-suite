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
        cur.execute("SHOW COLUMNS FROM users")
        print("users:", [c["Field"] for c in cur.fetchall()])
        cur.execute("""
            SELECT u.id, u.name, u.email, e.id eid, e.pesel_number
            FROM users u
            LEFT JOIN employees e ON e.user_id = u.id
            WHERE e.pesel_number IS NOT NULL AND e.pesel_number <> ''
            LIMIT 5
        """)
        print("users+employees:")
        for r in cur.fetchall():
            print(r)
        cur.execute("""
            SELECT COUNT(*) n FROM bills b
            JOIN contracts_dzielo cd ON cd.id=b.contract_id AND b.contract_type LIKE '%%Dzielo%%'
            JOIN customers c ON c.id=cd.client_id
            WHERE YEAR(b.account_till)=2026 AND MONTH(b.account_till)=4
        """)
        print("bills+client:", cur.fetchone())
t.stop()
