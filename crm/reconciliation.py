"""Reconcile CRM bills with payroll system rachunki by NUMER_RACHUNKU.

The automation imports contracts for a selected CRM payment month, but payroll system
payment dates may be edited manually. Because of that the primary invariant is:

    every CRM paid bill must have the same NUMER_RACHUNKU in payroll system somewhere

Month equality is diagnostic only. A bill found in payroll system with a different
DATA_WYPLATY is reported as a date mismatch, not as missing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import text

from crm.api_client import (
    CrmApiClient,
    _by_id,
    _date_in_month,
    _filter_tenant,
    _first_nonempty,
    _is_valid_pesel,
    _normalize_pesel,
    _resolve_bill_person,
)
from crm.settings import CrmApiSettings
from db.service import DatabaseService


@dataclass(frozen=True)
class CrmBillCheck:
    nr_rachunku: str
    bill_id: int | None
    status: str
    reason: str = ""
    payment_date: str = ""
    pesel: str = ""
    worker_name: str = ""


@dataclass(frozen=True)
class PayrollRachunek:
    nr_rachunku: str
    data_wyplaty: date | None
    pesel: str = ""
    worker_name: str = ""


@dataclass
class RachunkiReconciliation:
    year: int
    month: int
    tenant_id: int
    crm_paid_total: int = 0
    crm_importable_total: int = 0
    payroll_month_total: int = 0
    matched_any_date: int = 0
    matched_same_month: int = 0
    date_mismatch: list[tuple[CrmBillCheck, PayrollRachunek]] = field(default_factory=list)
    crm_missing_in_payroll: list[CrmBillCheck] = field(default_factory=list)
    payroll_month_not_in_crm_paid: list[PayrollRachunek] = field(default_factory=list)
    payroll_month_exists_in_crm_other_date: list[PayrollRachunek] = field(default_factory=list)
    crm_blocked: list[CrmBillCheck] = field(default_factory=list)
    api_requests: int = 0

    @property
    def hard_errors(self) -> int:
        return len([item for item in self.crm_missing_in_payroll if item.status == "ok"])

    @property
    def crm_missing_importable(self) -> list[CrmBillCheck]:
        return [item for item in self.crm_missing_in_payroll if item.status == "ok"]

    @property
    def crm_missing_blocked(self) -> list[CrmBillCheck]:
        return [item for item in self.crm_missing_in_payroll if item.status != "ok"]

    @property
    def explanatory_differences(self) -> int:
        return len(self.date_mismatch) + len(self.payroll_month_exists_in_crm_other_date)


def reconcile_rachunki(
    *,
    settings: CrmApiSettings,
    db_service: DatabaseService,
    year: int,
    month: int,
    tenant_id: int,
) -> RachunkiReconciliation:
    """Compare CRM paid bills with payroll system rachunki.

    This function is read-only for both CRM and payroll system.
    """
    tables, api_requests = _fetch_tables(settings, tenant_id)
    crm_checks = _build_crm_bill_checks(tables, year, month)
    crm_paid_by_nr = {c.nr_rachunku: c for c in crm_checks if c.nr_rachunku}
    crm_paid_nrs = set(crm_paid_by_nr)
    all_crm_bill_nrs = {
        str(row.get("bill_number") or "").strip()
        for row in tables["bills"]
        if str(row.get("bill_number") or "").strip()
    }

    payroll_month = _load_payroll_month_rachunki(db_service, year, month)
    payroll_month_by_nr = {r.nr_rachunku: r for r in payroll_month}
    payroll_any_by_nr = _load_payroll_rachunki_by_numbers(db_service, crm_paid_nrs)

    report = RachunkiReconciliation(
        year=year,
        month=month,
        tenant_id=tenant_id,
        crm_paid_total=len(crm_paid_nrs),
        crm_importable_total=sum(1 for c in crm_checks if c.status == "ok"),
        payroll_month_total=len(payroll_month_by_nr),
        api_requests=api_requests,
    )
    report.crm_blocked = [c for c in crm_checks if c.status != "ok"]

    for nr, crm_bill in crm_paid_by_nr.items():
        payroll_row = payroll_any_by_nr.get(nr)
        if payroll_row is None:
            report.crm_missing_in_payroll.append(crm_bill)
            continue
        report.matched_any_date += 1
        if nr in payroll_month_by_nr:
            report.matched_same_month += 1
        else:
            report.date_mismatch.append((crm_bill, payroll_row))

    for nr, payroll_row in payroll_month_by_nr.items():
        if nr in crm_paid_nrs:
            continue
        if nr in all_crm_bill_nrs:
            report.payroll_month_exists_in_crm_other_date.append(payroll_row)
        else:
            report.payroll_month_not_in_crm_paid.append(payroll_row)

    return report


def _fetch_tables(settings: CrmApiSettings, tenant_id: int) -> tuple[dict[str, list[dict]], int]:
    client = CrmApiClient(settings)
    table_names = (
        "bills",
        "salary_requests",
        "users",
        "contracts_zlicen",
        "contracts_dzielo",
        "employees",
        "customers",
    )
    data: dict[str, list[dict]] = {}
    for table in table_names:
        rows = client.fetch_table(table)
        if tenant_id and table != "users":
            rows = _filter_tenant(rows, tenant_id)
        data[table] = rows
    return data, client.api_requests


def _build_crm_bill_checks(
    tables: dict[str, list[dict]],
    year: int,
    month: int,
) -> list[CrmBillCheck]:
    sr_by_id = _by_id(tables["salary_requests"])
    users_by_id = _by_id(tables["users"])
    z_by_id = _by_id(tables["contracts_zlicen"])
    d_by_id = _by_id(tables["contracts_dzielo"])
    emp_by_id = _by_id(tables["employees"])
    cust_by_id = _by_id(tables["customers"])
    checks: list[CrmBillCheck] = []

    for bill in tables["bills"]:
        salary_request = sr_by_id.get(bill.get("salary_request_id"))
        payment_date = _first_nonempty(
            salary_request.get("paid_at") if salary_request else None,
            bill.get("account_till"),
            bill.get("account_from"),
        )
        if not _date_in_month(payment_date, year, month):
            continue

        nr = str(bill.get("bill_number") or "").strip()
        base = {
            "nr_rachunku": nr,
            "bill_id": bill.get("id"),
            "payment_date": str(payment_date or ""),
        }
        contract_type = str(bill.get("contract_type") or "")
        is_zlecenie = "Zlicen" in contract_type
        is_dzielo = "Dzielo" in contract_type
        if not (is_zlecenie or is_dzielo):
            checks.append(CrmBillCheck(**base, status="blocked", reason="bad_contract_type"))
            continue

        contract = z_by_id.get(bill.get("contract_id")) if is_zlecenie else d_by_id.get(bill.get("contract_id"))
        if not contract:
            checks.append(CrmBillCheck(**base, status="blocked", reason="no_contract"))
            continue

        person = _resolve_bill_person(
            bill=bill,
            contract=contract,
            salary_requests_by_id=sr_by_id,
            users_by_id=users_by_id,
            employees_by_id=emp_by_id,
            customers_by_id=cust_by_id,
        )
        if person is None:
            checks.append(CrmBillCheck(**base, status="blocked", reason="no_person"))
            continue
        if str(person.get("type") or "").strip().lower() == "legal_entity":
            checks.append(CrmBillCheck(**base, status="blocked", reason="legal_entity"))
            continue

        pesel = _normalize_pesel(
            _first_nonempty(
                person.get("pesel_number"),
                person.get("pesel"),
                person.get("tax_number"),
            )
        )
        first_name = _first_nonempty(person.get("name"), person.get("first_name"))
        last_name = _first_nonempty(person.get("surname"), person.get("last_name"))
        worker_name = " ".join(p for p in (first_name, last_name) if p).strip()
        if not pesel or not _is_valid_pesel(pesel):
            checks.append(
                CrmBillCheck(
                    **base,
                    status="blocked",
                    reason="no_valid_pesel",
                    worker_name=worker_name,
                    pesel=pesel,
                )
            )
            continue
        checks.append(
            CrmBillCheck(
                **base,
                status="ok",
                pesel=pesel,
                worker_name=worker_name,
            )
        )
    return checks


def _load_payroll_month_rachunki(
    db_service: DatabaseService,
    year: int,
    month: int,
) -> list[PayrollRachunek]:
    sql = text(
        """
        SELECT LTRIM(RTRIM(ISNULL(u.NUMER_RACHUNKU, ''))) AS nr,
               CONVERT(date, DATEADD(day, u.DATA_WYPLATY, CAST('1800-12-28' AS date))) AS data_wyplaty,
               LTRIM(RTRIM(ISNULL(p.PESEL, ''))) AS pesel,
               LTRIM(RTRIM(ISNULL(p.NAZWISKO, ''))) + ' ' + LTRIM(RTRIM(ISNULL(p.IMIE_1, ''))) AS worker_name
        FROM GANG_UMOWY_CYWILNO_PRAWNE u
        JOIN PRACOWNIK p ON p.ID_PRACOWNIKA = u.ID_NADRZEDNEGO
        WHERE p.ID_FIRMY = 1
          AND YEAR(DATEADD(day, u.DATA_WYPLATY, CAST('1800-12-28' AS date))) = :year
          AND MONTH(DATEADD(day, u.DATA_WYPLATY, CAST('1800-12-28' AS date))) = :month
          AND LTRIM(RTRIM(ISNULL(u.NUMER_RACHUNKU, ''))) <> ''
        """
    )
    with db_service.engine.connect() as conn:
        rows = conn.execute(sql, {"year": int(year), "month": int(month)}).all()
    return [
        PayrollRachunek(
            nr_rachunku=str(row[0]).strip(),
            data_wyplaty=row[1],
            pesel=str(row[2] or "").strip(),
            worker_name=str(row[3] or "").strip(),
        )
        for row in rows
    ]


def _load_payroll_rachunki_by_numbers(
    db_service: DatabaseService,
    nr_rachunki: Iterable[str],
) -> dict[str, PayrollRachunek]:
    clean = sorted({str(nr).strip() for nr in nr_rachunki if str(nr).strip()})
    result: dict[str, PayrollRachunek] = {}
    if not clean:
        return result
    with db_service.engine.connect() as conn:
        for chunk_start in range(0, len(clean), 500):
            chunk = clean[chunk_start:chunk_start + 500]
            params = {f"r{i}": value for i, value in enumerate(chunk)}
            placeholders = ", ".join(f":r{i}" for i in range(len(chunk)))
            sql = text(
                f"""
                SELECT LTRIM(RTRIM(ISNULL(u.NUMER_RACHUNKU, ''))) AS nr,
                       CONVERT(date, DATEADD(day, u.DATA_WYPLATY, CAST('1800-12-28' AS date))) AS data_wyplaty,
                       LTRIM(RTRIM(ISNULL(p.PESEL, ''))) AS pesel,
                       LTRIM(RTRIM(ISNULL(p.NAZWISKO, ''))) + ' ' + LTRIM(RTRIM(ISNULL(p.IMIE_1, ''))) AS worker_name
                FROM GANG_UMOWY_CYWILNO_PRAWNE u
                JOIN PRACOWNIK p ON p.ID_PRACOWNIKA = u.ID_NADRZEDNEGO
                WHERE LTRIM(RTRIM(ISNULL(u.NUMER_RACHUNKU, ''))) IN ({placeholders})
                ORDER BY u.DATA_WYPLATY DESC
                """
            )
            for row in conn.execute(sql, params).all():
                nr = str(row[0]).strip()
                result.setdefault(
                    nr,
                    PayrollRachunek(
                        nr_rachunku=nr,
                        data_wyplaty=row[1],
                        pesel=str(row[2] or "").strip(),
                        worker_name=str(row[3] or "").strip(),
                    ),
                )
    return result
