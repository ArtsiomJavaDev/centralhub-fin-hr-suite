"""CRM REST API client.

Universal endpoint:
    GET /api/data/{table}?page=1&per_page=500&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD

Builds an UDUZ04-like raw DataFrame from bills + contracts + employees/customers
for the existing formatter/import pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
import requests

from crm.settings import CrmApiSettings

ProgressCallback = Callable[[str, int, int], None]

_APP_DIR = Path(__file__).resolve().parent.parent
_IMPORT_FILES_DIR = _APP_DIR / "ImportFiles"

# Tables loaded for one monthly report (order matters for progress messages).
_REPORT_TABLES = (
    "bills",
    "salary_requests",
    "users",
    "contracts_zlicen",
    "contracts_dzielo",
    "employees",
    "customers",
)


class CrmApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class FetchStats:
    bills_total: int
    bills_in_period: int
    salary_requests: int
    users: int
    contracts_zlicen: int
    contracts_dzielo: int
    employees: int
    customers: int
    output_rows: int
    skipped_without_contract: int
    skipped_without_person: int
    skipped_without_pesel: int
    skipped_bad_contract_type: int
    skipped_legal_entity: int
    api_requests: int
    tenant_id: int

    @property
    def bills(self) -> int:
        """Backward-compatible alias."""
        return self.bills_total


class CrmApiClient:
    def __init__(self, settings: CrmApiSettings) -> None:
        self._settings = settings
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {settings.token}",
                "Accept": "application/json",
            }
        )
        self.api_requests = 0

    def test_connection(self) -> tuple[bool, str]:
        errors = self._settings.validate()
        if errors:
            return False, "; ".join(errors)
        try:
            data = self.fetch_table("bills", page_limit=1, extra_params={"per_page": 1})
            tenant = _tenant_label(self._settings.tenant_id)
            return True, f"OK — API ({tenant}), bills sample={len(data)}"
        except Exception as exc:
            return False, str(exc)

    def fetch_table(
        self,
        table: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        page_limit: int | None = None,
        extra_params: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        page = 1
        rows: list[dict[str, Any]] = []
        per_page = self._settings.per_page

        while True:
            params: dict[str, Any] = {
                "page": page,
                "per_page": per_page,
            }
            if date_from:
                params["date_from"] = date_from
            if date_to:
                params["date_to"] = date_to
            if extra_params:
                params.update(extra_params)

            url = f"{self._settings.base_url}/data/{table}"
            response = self._session.get(
                url,
                params=params,
                timeout=self._settings.timeout_seconds,
            )
            self.api_requests += 1

            if response.status_code == 429:
                raise CrmApiError("API rate limit: 429 Too Many Requests")
            if response.status_code >= 400:
                raise CrmApiError(
                    f"API {table}: HTTP {response.status_code}: {response.text[:300]}"
                )

            payload = response.json()
            data = payload.get("data", [])
            if not isinstance(data, list):
                raise CrmApiError(f"API {table}: pole data nie jest listą")
            rows.extend(data)

            meta = payload.get("meta", {}) or {}
            last_page = int(meta.get("last_page") or page)
            if page_limit is not None and page >= page_limit:
                break
            if page >= last_page:
                break
            page += 1

        return rows


def fetch_report_dataframe_api(
    settings: CrmApiSettings,
    year: int,
    month: int,
    *,
    tenant_id: int | None = None,
    progress_cb: ProgressCallback | None = None,
) -> tuple[pd.DataFrame, FetchStats]:
    """Fetch all API tables needed for one UDUZ04-style month report."""
    errors = settings.validate()
    if errors:
        raise CrmApiError("; ".join(errors))

    tid = int(tenant_id if tenant_id is not None else settings.tenant_id)
    effective = replace(settings, tenant_id=tid)

    date_from = f"{year:04d}-{month:02d}-01"
    date_to = _month_end(year, month)
    client = CrmApiClient(effective)

    def _progress(step: str, done: int, total: int) -> None:
        if progress_cb:
            progress_cb(step, done, total)

    table_data: dict[str, list[dict[str, Any]]] = {}
    total_steps = len(_REPORT_TABLES) + 1

    for idx, table in enumerate(_REPORT_TABLES):
        _progress(f"Pobieranie {table}…", idx, total_steps)
        # Do not pass date_from/date_to for bills here. The CRM API date
        # parameters are not based on account_till, so month-scoped requests
        # miss many older/future-created bills. We fetch bills broadly and
        # filter locally by bill.account_till in _build_uduz04_rows.
        rows = client.fetch_table(table)
        # `users` rows do not expose tenant_id. Tenant scoping is already
        # enforced by bills/salary_requests/contracts, so filtering users here
        # would drop every physical contractor.
        if tid and table != "users":
            rows = _filter_tenant(rows, tid)
        table_data[table] = rows

    _progress("Budowanie raportu…", len(_REPORT_TABLES), total_steps)

    raw, stats = _build_uduz04_rows(
        bills=table_data["bills"],
        salary_requests=table_data["salary_requests"],
        users=table_data["users"],
        contracts_z=table_data["contracts_zlicen"],
        contracts_d=table_data["contracts_dzielo"],
        employees=table_data["employees"],
        customers=table_data["customers"],
        year=year,
        month=month,
        tenant_id=tid,
        api_requests=client.api_requests,
    )

    _progress("Gotowe", total_steps, total_steps)
    return raw, stats


def save_api_audit_files(
    raw_df: pd.DataFrame,
    formatted_df: pd.DataFrame,
    year: int,
    month: int,
    tenant_id: int,
) -> tuple[str, str]:
    """Persist API raw + formatted snapshots under ImportFiles/."""
    _IMPORT_FILES_DIR.mkdir(parents=True, exist_ok=True)
    ts = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    tenant_suffix = f"t{tenant_id}" if tenant_id else "all"
    raw_path = _IMPORT_FILES_DIR / f"api_raw_{year:04d}{month:02d}_{tenant_suffix}_{ts}.xlsx"
    fmt_path = _IMPORT_FILES_DIR / f"api_formatted_{year:04d}{month:02d}_{tenant_suffix}_{ts}.xlsx"
    raw_df.to_excel(str(raw_path), index=False, header=False)
    from crm.formatter import df_to_export

    df_to_export(formatted_df).to_excel(str(fmt_path), index=False)
    return str(raw_path), str(fmt_path)


def _build_uduz04_rows(
    *,
    bills: list[dict[str, Any]],
    salary_requests: list[dict[str, Any]],
    users: list[dict[str, Any]],
    contracts_z: list[dict[str, Any]],
    contracts_d: list[dict[str, Any]],
    employees: list[dict[str, Any]],
    customers: list[dict[str, Any]],
    year: int,
    month: int,
    tenant_id: int,
    api_requests: int,
) -> tuple[pd.DataFrame, FetchStats]:
    sr_by_id = _by_id(salary_requests)
    users_by_id = _by_id(users)
    z_by_id = _by_id(contracts_z)
    d_by_id = _by_id(contracts_d)
    emp_by_id = _by_id(employees)
    cust_by_id = _by_id(customers)

    out_rows: list[list[Any]] = []
    bills_in_period = 0
    skipped_without_contract = 0
    skipped_without_person = 0
    skipped_without_pesel = 0
    skipped_bad_contract_type = 0
    skipped_legal_entity = 0

    for bill in bills:
        salary_request = sr_by_id.get(bill.get("salary_request_id"))
        payment_date = _first_nonempty(
            salary_request.get("paid_at") if salary_request else None,
            bill.get("account_till"),
            bill.get("account_from"),
        )
        if not _date_in_month(payment_date, year, month):
            continue
        bills_in_period += 1

        contract_type = str(bill.get("contract_type") or "")
        is_zlecenie = "Zlicen" in contract_type
        is_dzielo = "Dzielo" in contract_type
        if not (is_zlecenie or is_dzielo):
            skipped_bad_contract_type += 1
            continue

        contract = (
            z_by_id.get(bill.get("contract_id"))
            if is_zlecenie
            else d_by_id.get(bill.get("contract_id"))
        )
        if not contract:
            skipped_without_contract += 1
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
            skipped_without_person += 1
            continue

        # Legal entities (CRM customers with type=legal_entity) cannot be
        # PRACOWNIK in WaPro — their `tax_number` is a NIP/VAT number, not a
        # PESEL. Without this filter their 11-digit tax_numbers (e.g. French
        # FR59931696785) leak through _normalize_pesel as fake PESELs.
        if str(person.get("type") or "").strip().lower() == "legal_entity":
            skipped_legal_entity += 1
            continue

        # PESEL sources: pesel_number (employees), pesel (rare alias),
        # tax_number (individual customers only). Legal entities are
        # already filtered above so tax_number here is safe.
        pesel = _normalize_pesel(
            _first_nonempty(
                person.get("pesel_number"),
                person.get("pesel"),
                person.get("tax_number") if str(person.get("type") or "").lower() != "legal_entity" else None,
            )
        )
        if not pesel or not _is_valid_pesel(pesel):
            skipped_without_pesel += 1
            continue

        first_name = _first_nonempty(person.get("name"), person.get("first_name"))
        last_name = _first_nonempty(person.get("surname"), person.get("last_name"))
        company = _first_nonempty(person.get("company_name"))
        worker_name = " ".join(p for p in (first_name, last_name) if p).strip() or str(company)

        brutto = _money(bill.get("brutto_amount"))
        netto = _money(bill.get("netto_amount"))

        out_rows.append(
            [
                None,
                _first_nonempty(contract.get("number"), ""),
                _first_nonempty(bill.get("bill_number"), ""),
                "Umowa Zlecenie" if is_zlecenie else "Umowa o Dzieło",
                worker_name,
                pesel,
                netto,
                brutto,
                _kup_display(bill.get("kup")),
                _first_nonempty(contract.get("start_date"), bill.get("account_from")),
                round(brutto - netto, 2),
                payment_date,
                _money(bill.get("ppk_kwota") or bill.get("ppk") or 0),
                # Pos 13: PIT rate from API (vat field).
                _money(bill.get("vat")),
                # Pos 14: is_student flag — overrides ZUS-exempt heuristic in formatter.
                _bool_flag(bill.get("is_student")),
                # Pos 15: zus_chorobowe flag — when True, replace 0% with 2.45%.
                _bool_flag(bill.get("zus_chorobowe")),
                # Pos 16: calculate_type — 'netto_from_brutto' or 'brutto_from_netto'.
                str(bill.get("calculate_type") or ""),
                # Pos 17: zus_emerytalne flag — informational (UZ default = True).
                _bool_flag(bill.get("zus_emerytalne")),
                # Pos 18: zus_zdrowotne flag — informational (UZ default = True).
                _bool_flag(bill.get("zus_zdrowotne")),
                # Pos 19: bill_id — traceability back to CRM.
                bill.get("id"),
            ]
        )

    stats = FetchStats(
        bills_total=len(bills),
        bills_in_period=bills_in_period,
        salary_requests=len(salary_requests),
        users=len(users),
        contracts_zlicen=len(contracts_z),
        contracts_dzielo=len(contracts_d),
        employees=len(employees),
        customers=len(customers),
        output_rows=len(out_rows),
        skipped_without_contract=skipped_without_contract,
        skipped_without_person=skipped_without_person,
        skipped_without_pesel=skipped_without_pesel,
        skipped_bad_contract_type=skipped_bad_contract_type,
        skipped_legal_entity=skipped_legal_entity,
        api_requests=api_requests,
        tenant_id=tenant_id,
    )
    return pd.DataFrame(out_rows), stats


def _tenant_label(tenant_id: int) -> str:
    if tenant_id == 1:
        return "FBA"
    if tenant_id == 2:
        return "FBA Payroll"
    return "FBA + FBA Payroll"


def _filter_tenant(rows: list[dict[str, Any]], tenant_id: int) -> list[dict[str, Any]]:
    return [r for r in rows if int(r.get("tenant_id") or 0) == tenant_id]


def _by_id(rows: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    return {r.get("id"): r for r in rows if r.get("id") is not None}


def _resolve_person(
    contract: dict[str, Any],
    emp_by_id: dict[Any, dict[str, Any]],
    cust_by_id: dict[Any, dict[str, Any]],
) -> dict[str, Any] | None:
    employee_id = contract.get("employee_id")
    if employee_id in emp_by_id:
        return emp_by_id[employee_id]
    client_id = contract.get("client_id")
    if client_id in cust_by_id:
        return cust_by_id[client_id]
    return None


def _resolve_bill_person(
    *,
    bill: dict[str, Any],
    contract: dict[str, Any],
    salary_requests_by_id: dict[Any, dict[str, Any]],
    users_by_id: dict[Any, dict[str, Any]],
    employees_by_id: dict[Any, dict[str, Any]],
    customers_by_id: dict[Any, dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve the real physical person behind a bill.

    For most external contractors the bill points to salary_requests, and the
    salary request creator is the CRM `users` row containing PESEL, birth date,
    passport and address. The contract's client_id often points to `customers`,
    where legal entities have NIP/VAT in tax_number rather than PESEL.

    Fallback to the old contract employee/customer route for legacy rows.
    """
    salary_request = salary_requests_by_id.get(bill.get("salary_request_id"))
    user_id = salary_request.get("created_by") if salary_request else None
    if user_id in users_by_id:
        user = users_by_id[user_id]
        # In many rows created_by is the real contractor and `users` carries
        # PESEL/passport/address. In other rows created_by is only a CRM
        # operator/account manager (often no PESEL). Do not treat those as the
        # contractor; fall back to the contract person instead.
        user_pesel = _normalize_pesel(user.get("pesel_number") or user.get("pesel"))
        if user_pesel and _is_valid_pesel(user_pesel):
            return user
    return _resolve_person(contract, employees_by_id, customers_by_id)


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() != "nan":
            return value
    return ""


def _normalize_pesel(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw.startswith("eyJ"):
        return ""
    if raw.endswith(".0") and raw[:-2].isdigit():
        raw = raw[:-2]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits if len(digits) == 11 else ""


# Polish PESEL month encoding for century:
#   01-12 → 1900s | 21-32 → 2000s | 41-52 → 2100s
#   61-72 → 1800s | 81-92 → 2200s
_VALID_PESEL_MONTHS: frozenset[int] = frozenset(
    list(range(1, 13)) + list(range(21, 33)) + list(range(41, 53))
    + list(range(61, 73)) + list(range(81, 93))
)


def _is_valid_pesel(pesel: str) -> bool:
    """Cheap structural validation: PESEL must encode a real month."""
    if len(pesel) != 11 or not pesel.isdigit():
        return False
    try:
        month = int(pesel[2:4])
        day = int(pesel[4:6])
    except ValueError:
        return False
    if month not in _VALID_PESEL_MONTHS:
        return False
    if not (1 <= day <= 31):
        return False
    return True


def _bool_flag(value: Any) -> int | None:
    """Normalize API booleans to 1/0/None (None when CRM didn't set the flag)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    text = str(value).strip().lower()
    if text in ("", "null", "none", "nan"):
        return None
    if text in ("1", "true", "t", "yes", "y"):
        return 1
    if text in ("0", "false", "f", "no", "n"):
        return 0
    return None


def _money(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _kup_display(value: Any) -> str:
    if value is None or value == "":
        return "0%"
    text = str(value).strip()
    return text if text.endswith("%") else f"{text}%"


def _date_in_month(value: Any, year: int, month: int) -> bool:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return False
    return int(parsed.year) == int(year) and int(parsed.month) == int(month)


def _month_end(year: int, month: int) -> str:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    end = pd.Timestamp(next_month) - pd.Timedelta(days=1)
    return end.strftime("%Y-%m-%d")
