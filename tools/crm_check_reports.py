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
            "SELECT type, format, COUNT(*) c FROM reports "
            "GROUP BY type, format ORDER BY c DESC LIMIT 20"
        )
        print("report types:")
        for r in cur.fetchall():
            print(r)
        cur.execute(
            "SELECT id, type, format, start_date, end_date, LEFT(file_path,80) fp "
            "FROM reports WHERE start_date >= '2026-01-01' ORDER BY id DESC LIMIT 8"
        )
        print("\nrecent reports:")
        for r in cur.fetchall():
            print(r)
t.stop()
