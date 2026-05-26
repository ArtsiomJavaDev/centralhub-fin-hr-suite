#!/usr/bin/env python
"""CRM MySQL setup helper — save credentials and test SSH tunnel.

Usage:
  python tools/crm_configure.py --set-user USER
  python tools/crm_configure.py --set-password
  python tools/crm_configure.py --set-password YOUR_PASSWORD
  python tools/crm_configure.py --test
  python tools/crm_configure.py --list-tables
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crm.mysql_client import list_tables, test_connection
from crm.settings import load_crm_settings, save_crm_mysql_password, save_crm_mysql_user


def main() -> int:
    parser = argparse.ArgumentParser(description="CRM MySQL / SSH configuration")
    parser.add_argument("--set-user", metavar="USER", help="Save MySQL username to config.ini")
    parser.add_argument(
        "--set-password",
        nargs="?",
        const="__PROMPT__",
        metavar="PASSWORD",
        help="Save MySQL password (encrypted). Without value — secure prompt.",
    )
    parser.add_argument("--test", action="store_true", help="Test SSH tunnel + MySQL")
    parser.add_argument("--list-tables", action="store_true", help="List tables in the configured MySQL database")
    args = parser.parse_args()

    if args.set_user:
        save_crm_mysql_user(args.set_user)
        print(f"OK: mysql_user = {args.set_user}")
        return 0

    if args.set_password is not None:
        if args.set_password == "__PROMPT__":
            pwd = getpass.getpass("MySQL password (from 1Password): ")
        else:
            pwd = args.set_password
            print("(Hasło z argumentu — w historii PowerShell; lepiej użyć samego --set-password bez hasła.)")
        if not pwd:
            print("Пустой пароль — отмена.")
            return 1
        save_crm_mysql_password(pwd)
        print("OK: пароль сохранён в config.ini (зашифрован DPAPI).")
        return 0

    settings = load_crm_settings()

    if args.test:
        ok, msg = test_connection(settings)
        print(msg)
        return 0 if ok else 1

    if args.list_tables:
        errors = settings.validate()
        if errors:
            print("Ошибка конфигурации:", "; ".join(errors))
            return 1
        print("Подключение и список таблиц…")
        try:
            tables = list_tables(settings)
        except Exception as exc:
            print(f"Błąd: {exc}")
            return 1
        print(f"Tabele ({len(tables)}):")
        for t in tables:
            print(f"  - {t}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
