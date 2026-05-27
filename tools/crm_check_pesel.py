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
        cur.execute(
            "SELECT COUNT(*) t, SUM(pesel_number LIKE 'eyJ%%') enc "
            "FROM employees WHERE pesel_number IS NOT NULL AND LENGTH(pesel_number) > 0"
        )
        print(cur.fetchone())
        cur.execute(
            "SELECT id, pesel_number, name, surname FROM employees "
            "WHERE pesel_number IS NOT NULL AND pesel_number NOT LIKE 'eyJ%%' LIMIT 10"
        )
        for r in cur.fetchall():
            print(r)
t.stop()
