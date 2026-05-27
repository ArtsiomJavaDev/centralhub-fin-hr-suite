#!/usr/bin/env python
"""Full automation simulation for one CRM month — NO writes to payroll database.

Runs the same steps as Automatyzacja tab:
  1. API fetch → format
  2. PESEL check (read-only)
  3. Financial verification
  4. Onboarding plan (read-only, no PRACOWNIK insert)
  5. Dry-run check-in (no import)

Usage:
  .venv\\Scripts\\python.exe tools/simulate_automation_march.py
  .venv\\Scripts\\python.exe tools/simulate_automation_march.py --year 2026 --month 3
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crm.api_client import fetch_report_dataframe_api
from crm.checker import check_pesels_in_db, verify_financials
from crm.formatter import df_to_export, format_crm_report
from crm.onboarding import collect_onboarding_candidates
from crm.reconciliation import reconcile_rachunki
from crm.settings import load_crm_api_settings
from db.config_loader import load_db_config
from db.service import DatabaseService
from crm.formatter import AUTO_MAPPING
from importer.checkin import check_in
from importer.mapping import map_columns
from importer.profiles import UMOWY_MIXED_IMPORT_PROFILE
from importer.types import RowStatus
from importer.utils import _to_clarion_date


def _banner(title: str) -> None:
    print(f"\n{'=' * 60}\n  {title}\n{'=' * 60}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate CRM automation (no DB writes)")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--month", type=int, default=3, help="Month 1-12 (default: March)")
    parser.add_argument("--tenant", type=int, default=1, help="1=FBA, 2=Payroll, 0=all")
    args = parser.parse_args()

    year, month, tenant = args.year, args.month, args.tenant
    month_name = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][month]

    _banner(f"SIMULATION {month_name} {year}  tenant={tenant}  (NO DB WRITES)")

    settings = load_crm_api_settings()
    db_cfg = load_db_config()

    # ── Step 1: API + format ─────────────────────────────────────────────────
    _banner("① API fetch + format")
    try:
        raw, stats = fetch_report_dataframe_api(
            settings, year, month, tenant_id=tenant if tenant else None
        )
        formatted, fmt = format_crm_report(raw)
    except Exception as exc:
        print(f"FAIL: API/format — {exc}")
        return 1

    print(f"  API bills in period:     {stats.bills_in_period}")
    print(f"  Output rows:             {stats.output_rows}")
    print(f"  Skipped no contract:     {stats.skipped_without_contract}")
    print(f"  Skipped no person:       {stats.skipped_without_person}")
    print(f"  Skipped no PESEL:        {stats.skipped_without_pesel}")
    print(f"  Skipped bad type:        {stats.skipped_bad_contract_type}")
    print(f"  Skipped legal entity:    {stats.skipped_legal_entity}")
    print(f"  Formatted:               UD={fmt.ud_count}, UZ={fmt.uz_count}, total={fmt.total_rows}")
    if fmt.warnings:
        print(f"  Formatter warnings ({len(fmt.warnings)}):")
        for w in fmt.warnings[:8]:
            print(f"    - {w}")
        if len(fmt.warnings) > 8:
            print(f"    ... +{len(fmt.warnings) - 8} more")

    if formatted.empty:
        print("\n  No rows for this period — nothing more to simulate.")
        return 0

    if "Stawka podatku [%]" in formatted.columns:
        pit_dist = formatted["Stawka podatku [%]"].value_counts().to_dict()
        print(f"  PIT rates:               {pit_dist}")

    # ── Step 1b: CRM/payroll system rachunki reconciliation ──────────────────────────
    _banner("①b Rachunki reconciliation (CRM ↔ payroll system, read-only)")
    try:
        svc = DatabaseService(db_cfg)
        ok_conn, conn_msg = svc.test_connection()
        if not ok_conn:
            print(f"FAIL: DB connection — {conn_msg}")
            return 1
        rachunki_report = reconcile_rachunki(
            settings=settings,
            db_service=svc,
            year=year,
            month=month,
            tenant_id=tenant if tenant else 0,
        )
    except Exception as exc:
        print(f"FAIL: rachunki reconciliation — {exc}")
        return 1

    print(f"  CRM paid bills:          {rachunki_report.crm_paid_total}")
    print(f"  CRM importable bills:    {rachunki_report.crm_importable_total}")
    print(f"  payroll system month rachunki:    {rachunki_report.payroll_month_total}")
    print(f"  CRM found in payroll system any date: {rachunki_report.matched_any_date}")
    print(f"  Same month matches:      {rachunki_report.matched_same_month}")
    print(f"  Date mismatches:         {len(rachunki_report.date_mismatch)}")
    print(f"  CRM missing in payroll system:    {len(rachunki_report.crm_missing_in_payroll)}")
    print(f"    importable missing:    {len(rachunki_report.crm_missing_importable)}")
    print(f"    blocked missing:       {len(rachunki_report.crm_missing_blocked)}")
    print(
        f"  payroll system month not in CRM paid month: "
        f"{len(rachunki_report.payroll_month_not_in_crm_paid)}"
    )
    print(
        f"  payroll system month exists in CRM other date: "
        f"{len(rachunki_report.payroll_month_exists_in_crm_other_date)}"
    )
    if rachunki_report.crm_missing_in_payroll:
        print("  Sample CRM missing in payroll system:")
        for item in rachunki_report.crm_missing_in_payroll[:8]:
            print(
                f"    [{item.status}/{item.reason or 'ok'}] "
                f"{item.nr_rachunku} | {item.worker_name} | PESEL={item.pesel}"
            )
    if rachunki_report.date_mismatch:
        print("  Sample date mismatches:")
        for crm_bill, payroll_bill in rachunki_report.date_mismatch[:8]:
            print(
                f"    {crm_bill.nr_rachunku}: CRM paid={crm_bill.payment_date} "
                f"payroll system DATA_WYPLATY={payroll_bill.data_wyplaty}"
            )

  # ── Step 2: PESEL check ──────────────────────────────────────────────────
    _banner("② PESEL check (read-only)")
    try:
        svc = DatabaseService(db_cfg)
        ok_conn, conn_msg = svc.test_connection()
        if not ok_conn:
            print(f"FAIL: DB connection — {conn_msg}")
            return 1
        print(f"  DB: {conn_msg}")
        pesel_result = check_pesels_in_db(formatted, svc)
    except Exception as exc:
        print(f"FAIL: PESEL check — {exc}")
        return 1

    print(f"  Found in payroll system:           {pesel_result.found}/{pesel_result.total}")
    print(f"  Missing:                  {len(pesel_result.missing)}")
    if pesel_result.missing_rows:
        for mr in pesel_result.missing_rows[:10]:
            print(f"    MISSING: PESEL={mr.get('PESEL')} | {mr.get('Pracownik')} | {mr.get('Nr Rachunku')}")
        if len(pesel_result.missing_rows) > 10:
            print(f"    ... +{len(pesel_result.missing_rows) - 10} more")

  # ── Step 2b: Onboarding plan (no insert) ─────────────────────────────────
    if pesel_result.missing_rows:
        _banner("②b Onboarding plan (read-only, NO insert)")
        try:
            plan = collect_onboarding_candidates(
                pesel_result.missing_rows, settings,
                tenant_id=tenant if tenant else None,
            )
            print(f"  Can auto-onboard:         {len(plan.can_onboard)}")
            print(f"  Blocked:                  {len(plan.blocked)}")
            print(f"  Not in CRM:               {len(plan.not_found_pesels)}")
            for cand in plan.can_onboard[:5]:
                print(f"    READY: {cand.pesel} {cand.full_name_label} [{cand.source}]")
            for cand in plan.blocked[:5]:
                print(f"    BLOCKED: {cand.pesel} {cand.full_name_label or '-'}: {'; '.join(cand.blockers)}")
        except Exception as exc:
            print(f"  WARN: onboarding plan failed — {exc}")

  # ── Step 3: Financial verification ───────────────────────────────────────
    _banner("③ Financial verification")
    try:
        vr = verify_financials(formatted)
    except Exception as exc:
        print(f"FAIL: verify — {exc}")
        return 1

    print(f"  Total rows:               {vr.total}")
    print(f"  OK:                       {vr.ok}")
    print(f"  Marginal:                 {vr.marginal}")
    print(f"  Discrepancy:              {vr.discrepancy}")
    if vr.api_rows or vr.excel_rows:
        print(f"  Source:                   API={vr.api_rows}, Excel={vr.excel_rows}")
    if vr.zus_exempt_rows:
        print(f"  ZUS-exempt:               {vr.zus_exempt_rows}")
    if vr.pit_zero_rows:
        print(f"  PIT=0:                    {vr.pit_zero_rows}")

    problems = [r for r in vr.rows if not r.is_ok or r.is_marginal]
    if problems:
        print(f"  Non-OK rows ({len(problems)}):")
        for r in problems[:12]:
            tag = "MARG" if r.is_marginal else "DISC"
            print(f"    [{tag}] {r.nr_rachunku} PESEL={r.pesel} {r.typ} diff={r.diff:.2f} — {r.note[:80]}")

  # ── Step 4: Dry-run check-in ─────────────────────────────────────────────
    _banner("④ Dry-run check-in (NO import)")
    try:
        data_od = _to_clarion_date(f"01/{month:02d}/{year:04d}") or 0
        df_clean = df_to_export(formatted)
        mapped_df = map_columns(
            df_clean, AUTO_MAPPING, UMOWY_MIXED_IMPORT_PROFILE,
            employee_lookup_mode="pesel",
        )
        checkin = check_in(
            mapped_df,
            db_service=svc,
            dry_run=True,
            data_od=int(data_od),
            profile=UMOWY_MIXED_IMPORT_PROFILE,
            employee_lookup_mode="pesel",
        )
    except Exception as exc:
        print(f"FAIL: dry-run — {exc}")
        import traceback
        traceback.print_exc()
        return 1

    warnings_n = sum(1 for r in checkin.rows if r.status == RowStatus.WARNING)
    print(f"  Importable rows:          {len(checkin.importable_rows)}")
    print(f"  Check-in errors:          {checkin.errors}")
    print(f"  Warnings:                 {warnings_n}")
    if checkin.errors > 0:
        err_rows = [r for r in checkin.rows if r.status == RowStatus.ERROR]
        for r in err_rows[:10]:
            print(f"    ERR row {r.index}: {r.message}")

  # ── Summary ──────────────────────────────────────────────────────────────
    _banner("SUMMARY")
    passed = (
        stats.output_rows > 0
        and vr.discrepancy == 0
        and checkin.errors == 0
    )
    issues = []
    if stats.output_rows == 0:
        issues.append("no data rows from API")
    if vr.discrepancy > 0:
        issues.append(f"{vr.discrepancy} financial discrepancies")
    if checkin.errors > 0:
        issues.append(f"{checkin.errors} check-in errors")
    if rachunki_report.hard_errors > 0:
        issues.append(f"{rachunki_report.hard_errors} CRM rachunki missing in payroll system")
    if len(pesel_result.missing) > 0:
        issues.append(f"{len(pesel_result.missing)} PESEL missing in payroll system (import would skip those umowy)")

    if passed and not issues:
        print("  RESULT: PASS — pipeline OK for this month (simulation only, nothing written).")
    elif passed and issues:
        print("  RESULT: PASS WITH NOTES — pipeline runs; review notes below:")
        for i in issues:
            print(f"    - {i}")
    else:
        print("  RESULT: FAIL — fix issues before real import:")
        for i in issues:
            print(f"    - {i}")

    return 0 if (stats.output_rows == 0 or (vr.discrepancy == 0 and checkin.errors == 0)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
