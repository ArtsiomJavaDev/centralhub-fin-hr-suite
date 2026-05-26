#!/usr/bin/env python

"""Configure and test CRM API access.



Usage:

  python tools/crm_api_configure.py --set-token

  python tools/crm_api_configure.py --set-token TOKEN

  python tools/crm_api_configure.py --set-tenant 0|1|2

  python tools/crm_api_configure.py --test

  python tools/crm_api_configure.py --sample bills

  python tools/crm_api_configure.py --report 2026 4

  python tools/crm_api_configure.py --report 2026 5 --tenant 1

"""

from __future__ import annotations



import argparse

import getpass

import sys

from pathlib import Path



ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT))



from crm.api_client import CrmApiClient, fetch_report_dataframe_api

from crm.formatter import format_crm_report

from crm.settings import load_crm_api_settings, save_crm_api_token, save_crm_api_tenant_id





def _print_stats(stats) -> None:

    print(

        f"  bills (API total):     {stats.bills_total}\n"

        f"  bills in period:       {stats.bills_in_period}\n"

        f"  output rows:           {stats.output_rows}\n"

        f"  API requests:          {stats.api_requests}\n"

        f"  tenant_id:             {stats.tenant_id}\n"

        f"  skipped no contract:   {stats.skipped_without_contract}\n"

        f"  skipped no person:     {stats.skipped_without_person}\n"

        f"  skipped no PESEL:      {stats.skipped_without_pesel}\n"

        f"  skipped bad type:      {stats.skipped_bad_contract_type}\n"

        f"  skipped legal entity:  {stats.skipped_legal_entity}"

    )





def main() -> int:

    parser = argparse.ArgumentParser(description="CRM API configuration and smoke tests")

    parser.add_argument(

        "--set-token",

        nargs="?",

        const="__PROMPT__",

        metavar="TOKEN",

        help="Save CRM API Bearer token (encrypted). Without value — secure prompt.",

    )

    parser.add_argument(

        "--set-tenant",

        type=int,

        choices=[0, 1, 2],

        metavar="ID",

        help="Save tenant filter: 0=all, 1=FBA, 2=FBA Payroll",

    )

    parser.add_argument("--test", action="store_true", help="Test API token with bills sample")

    parser.add_argument("--sample", metavar="TABLE", help="Fetch first 2 rows from table (no token in output)")

    parser.add_argument("--report", nargs=2, metavar=("YEAR", "MONTH"), help="Build UDUZ04 raw report + format")

    parser.add_argument(

        "--tenant",

        type=int,

        choices=[0, 1, 2],

        help="Tenant override for --test/--report (default: config.ini)",

    )

    args = parser.parse_args()



    if args.set_token is not None:

        if args.set_token == "__PROMPT__":

            token = getpass.getpass("CRM API token: ")

        else:

            token = args.set_token

            print("(Token z argumentu — w historii PowerShell; lepiej użyć samego --set-token.)")

        if not token:

            print("Пустой токен — отмена.")

            return 1

        save_crm_api_token(token)

        print("OK: API token сохранён в config.ini (зашифрован DPAPI).")

        return 0



    if args.set_tenant is not None:

        save_crm_api_tenant_id(args.set_tenant)

        labels = {0: "Wszystko", 1: "FBA", 2: "FBA Payroll"}

        print(f"OK: tenant_id={args.set_tenant} ({labels[args.set_tenant]})")

        return 0



    settings = load_crm_api_settings()

    tenant_id = args.tenant if args.tenant is not None else settings.tenant_id



    if args.test:

        from dataclasses import replace



        ok, msg = CrmApiClient(replace(settings, tenant_id=tenant_id)).test_connection()

        print(msg)

        return 0 if ok else 1



    if args.sample:

        rows = CrmApiClient(settings).fetch_table(

            args.sample,

            page_limit=1,

            extra_params={"per_page": 2},

        )

        print(f"{args.sample}: {len(rows)} row(s)")

        for row in rows:

            safe = {k: v for k, v in row.items() if k not in ("token", "password")}

            print(safe)

        return 0



    if args.report:

        year = int(args.report[0])

        month = int(args.report[1])

        raw, stats = fetch_report_dataframe_api(

            settings, year, month, tenant_id=tenant_id

        )

        formatted, fmt = format_crm_report(raw)

        print(f"Report {month:02d}/{year}, tenant={tenant_id}")

        _print_stats(stats)

        print(

            f"Formatted: total={fmt.total_rows}, UZ={fmt.uz_count}, UD={fmt.ud_count}"

        )

        if not formatted.empty:

            print(formatted.head(5).to_string())

        return 0



    parser.print_help()

    return 0





if __name__ == "__main__":

    raise SystemExit(main())

