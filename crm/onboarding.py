"""Auto-onboarding of missing employees from CRM into WAPRO PRACOWNIK.

Flow
----
1. Check which PESELs from the formatted CRM report are not in WAPRO PRACOWNIK
   (see crm.checker.check_pesels_in_db).
2. For each missing PESEL, look up the person in CRM tables (employees first,
   then customers).
3. Validate mandatory data: PESEL, name, surname, date_of_birth, passport.
4. Address is optional and only included if it's a Polish address; otherwise
   the employee is created without ADRESY_PRACOWNIKA entry.
5. Urząd skarbowy is resolved from CRM `authority_agency` code (3-digit value
   padded to 4 digits to match WAPRO URZEDY.KOD_US).
6. Status RODZAJ_PRACOWNIKA = 2 — "Osoba z zewnątrz" (matches all 965 existing
   employees in the current WAPRO database).

The resulting rows are fed to ``DatabaseService.execute_employee_import`` which
already knows how to allocate IDs and create the related URZEDY_PRACOWNIKA link.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from crm.api_client import CrmApiClient
from crm.settings import CrmApiSettings


# ─── Address detection ───────────────────────────────────────────────────────

_POLISH_POSTAL_RE = re.compile(r"^\d{2}-\d{3}$")
# Country values that CRM uses for Poland (we also accept missing country if
# the postal code matches the Polish XX-XXX pattern).
_POLISH_COUNTRIES: frozenset[str] = frozenset({
    "polska", "poland", "rzeczpospolita polska", "pl",
})


def _is_polish_address(country: str | None, postal_code: str | None) -> bool:
    if country and country.strip().lower() in _POLISH_COUNTRIES:
        return True
    if postal_code and _POLISH_POSTAL_RE.match(postal_code.strip()):
        return True
    return False


def _split_address(raw: str | None) -> tuple[str, str, str]:
    """Split a free-form address string into (street, house_no, flat_no).

    CRM employees use 'Street Number/Apartment' format. CRM customers expose
    `street` / `building_number` / `apartment` separately and call this with
    only the street to avoid mangling it.
    """
    if not raw:
        return ("", "", "")
    text = str(raw).strip()
    if not text:
        return ("", "", "")

    # If the trailing token looks like a number (with optional letter and / for flat),
    # peel it off as house_no [+ flat_no].
    parts = text.rsplit(" ", 1)
    if len(parts) != 2:
        return (text, "", "")
    street_part, tail = parts[0].strip(), parts[1].strip()
    if not re.search(r"\d", tail):
        return (text, "", "")

    if "/" in tail:
        house, flat = tail.split("/", 1)
        return (street_part, house.strip(), flat.strip())
    return (street_part, tail, "")


# ─── Urząd skarbowy code normalization ───────────────────────────────────────

def _normalize_urzad_code(value: Any) -> str:
    """Polish urząd code is 4-digit. CRM strips leading zero (e.g. '840').

    Returns the value padded to 4 digits with leading zeros, or '' if not a
    valid numeric token.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return ""
    return digits.zfill(4)


# ─── Date / pesel helpers ────────────────────────────────────────────────────

def _parse_iso_date(value: Any) -> str:
    """ISO datetime → 'DD/MM/YYYY' (format expected by _parse_excel_date_to_clarion)."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text.split("+")[0], fmt)
            return dt.strftime("%d/%m/%Y")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return ""


def _normalize_pesel(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw.startswith("eyJ"):
        return ""
    if raw.endswith(".0") and raw[:-2].isdigit():
        raw = raw[:-2]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits if len(digits) == 11 else ""


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() != "nan":
            return text
    return ""


# ─── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass
class OnboardingCandidate:
    """One missing PESEL with the data we collected from CRM."""
    pesel: str
    source: str = ""               # 'employees' | 'customers' | ''
    crm_id: Optional[int] = None
    name: str = ""
    surname: str = ""
    full_name_label: str = ""      # for UI display only
    date_of_birth: str = ""        # 'DD/MM/YYYY' (Clarion-friendly)
    passport_number: str = ""
    id_card_no: str = ""           # currently unused — CRM doesn't expose dowód
    phone: str = ""
    email: str = ""
    # Address (only populated when is_polish_address=True)
    is_polish_address: bool = False
    city: str = ""
    postal_code: str = ""
    street: str = ""
    house_no: str = ""
    flat_no: str = ""
    country: str = ""
    # Urząd skarbowy
    urzad_code: str = ""           # 4-digit, padded
    # Blockers (mandatory data missing — cannot be onboarded automatically)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def can_onboard(self) -> bool:
        return not self.blockers


@dataclass
class OnboardingPlan:
    """Aggregate result of building onboarding candidates."""
    candidates: list[OnboardingCandidate] = field(default_factory=list)
    not_found_pesels: list[str] = field(default_factory=list)  # PESEL not in CRM at all
    api_requests: int = 0

    @property
    def can_onboard(self) -> list[OnboardingCandidate]:
        return [c for c in self.candidates if c.can_onboard]

    @property
    def blocked(self) -> list[OnboardingCandidate]:
        return [c for c in self.candidates if not c.can_onboard]


# ─── Building candidates ─────────────────────────────────────────────────────

def _index_employees_by_pesel(rows: Iterable[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for row in rows:
        pesel = _normalize_pesel(_first_nonempty(row.get("pesel_number"), row.get("pesel")))
        if pesel:
            index[pesel] = row
    return index


def _index_customers_by_pesel(rows: Iterable[dict]) -> dict[str, dict]:
    """Customers use `tax_number` for individuals. Index only when it parses
    as a valid 11-digit PESEL.
    """
    index: dict[str, dict] = {}
    for row in rows:
        pesel = _normalize_pesel(_first_nonempty(row.get("tax_number"), row.get("pesel")))
        if pesel:
            index[pesel] = row
    return index


def _build_from_employee(pesel: str, emp: dict) -> OnboardingCandidate:
    name = _first_nonempty(emp.get("name"), emp.get("first_name"))
    surname = _first_nonempty(emp.get("surname"), emp.get("last_name"))
    passport = _first_nonempty(emp.get("passport_number"), emp.get("passport"))
    dob = _parse_iso_date(emp.get("date_of_birth"))
    city = _first_nonempty(emp.get("city"))
    postal_code = _first_nonempty(emp.get("postal_code"))
    address_raw = _first_nonempty(emp.get("address"))
    country = _first_nonempty(emp.get("country"))
    urzad_code = _normalize_urzad_code(emp.get("authority_agency"))
    phone = _first_nonempty(emp.get("phone"))
    email = _first_nonempty(emp.get("email"))

    is_polish = _is_polish_address(country or None, postal_code or None)
    street, house_no, flat_no = ("", "", "")
    if is_polish and address_raw:
        street, house_no, flat_no = _split_address(address_raw)

    cand = OnboardingCandidate(
        pesel=pesel,
        source="employees",
        crm_id=emp.get("id"),
        name=name,
        surname=surname,
        full_name_label=f"{surname} {name}".strip(),
        date_of_birth=dob,
        passport_number=passport,
        phone=phone,
        email=email,
        is_polish_address=is_polish,
        city=city if is_polish else "",
        postal_code=postal_code if is_polish else "",
        street=street,
        house_no=house_no,
        flat_no=flat_no,
        country="Polska" if is_polish else "",
        urzad_code=urzad_code,
    )
    _attach_blockers(cand)
    if not is_polish:
        cand.warnings.append(
            f"Adres niepolski (postal={postal_code or '-'}, kraj={country or '-'}) "
            "— PRACOWNIK utworzony bez ADRESY_PRACOWNIKA."
        )
    if not urzad_code:
        cand.warnings.append("Brak authority_agency w CRM — bez powiązania z urzędem.")
    return cand


def _build_from_customer(pesel: str, cust: dict) -> OnboardingCandidate:
    """Customers usually lack DOB and passport — most will be 'blocked'."""
    name = _first_nonempty(cust.get("first_name"))
    surname = _first_nonempty(cust.get("last_name"))
    company = _first_nonempty(cust.get("company_name"))
    if not (name or surname) and company:
        # Legal entity — cannot be onboarded as PRACOWNIK
        cand = OnboardingCandidate(
            pesel=pesel,
            source="customers",
            crm_id=cust.get("id"),
            full_name_label=company,
        )
        cand.blockers.append("CRM customer to legal_entity (firma) — nie kandyduje na PRACOWNIK.")
        return cand

    postal_code = _first_nonempty(cust.get("postal_code"))
    country = _first_nonempty(cust.get("country"))
    is_polish = _is_polish_address(country or None, postal_code or None)

    cand = OnboardingCandidate(
        pesel=pesel,
        source="customers",
        crm_id=cust.get("id"),
        name=name,
        surname=surname,
        full_name_label=f"{surname} {name}".strip(),
        date_of_birth="",            # customers don't expose DOB
        passport_number="",          # customers don't expose passport
        is_polish_address=is_polish,
        city=_first_nonempty(cust.get("city")) if is_polish else "",
        postal_code=postal_code if is_polish else "",
        street=_first_nonempty(cust.get("street")) if is_polish else "",
        house_no=_first_nonempty(cust.get("building_number")) if is_polish else "",
        flat_no=_first_nonempty(cust.get("apartment")) if is_polish else "",
        country="Polska" if is_polish else "",
    )
    _attach_blockers(cand)
    if not is_polish:
        cand.warnings.append(
            f"Adres niepolski (postal={postal_code or '-'}, kraj={country or '-'})."
        )
    cand.warnings.append(
        "CRM customer nie zawiera daty urodzenia ani paszportu — "
        "uzupełnij dane w WaPro ręcznie przed pierwszą umową."
    )
    return cand


def _attach_blockers(cand: OnboardingCandidate) -> None:
    """Validate mandatory data. Anything missing → cannot auto-onboard."""
    if not cand.pesel:
        cand.blockers.append("Brak PESEL.")
    if not cand.name:
        cand.blockers.append("Brak imienia.")
    if not cand.surname:
        cand.blockers.append("Brak nazwiska.")
    if not cand.date_of_birth:
        cand.blockers.append("Brak daty urodzenia (CRM date_of_birth puste).")
    if not cand.passport_number:
        cand.blockers.append("Brak numeru paszportu (CRM passport_number puste).")


def collect_onboarding_candidates(
    missing_rows: Iterable[dict],
    settings: CrmApiSettings,
    *,
    tenant_id: int | None = None,
) -> OnboardingPlan:
    """Fetch CRM employees + customers and build onboarding candidates.

    Parameters
    ----------
    missing_rows
        Rows from ``CheckPeselResult.missing_rows`` — dicts with at least
        ``PESEL``, ``Pracownik``, ``Nr Rachunku``, ``Typ`` keys.
    settings
        CRM API settings (token, base_url etc.).
    tenant_id
        Optional override for tenant filter (1 / 2 / 0). When ``None`` uses
        ``settings.tenant_id``.

    Returns
    -------
    OnboardingPlan
        Candidates (some can be auto-onboarded, some are blocked) and a list
        of PESELs that were not found in CRM at all.
    """
    plan = OnboardingPlan()
    missing_list = list(missing_rows)
    if not missing_list:
        return plan

    client = CrmApiClient(settings)
    employees = client.fetch_table("employees")
    customers = client.fetch_table("customers")
    plan.api_requests = client.api_requests

    if tenant_id:
        employees = [r for r in employees if int(r.get("tenant_id") or 0) == tenant_id]
        customers = [r for r in customers if int(r.get("tenant_id") or 0) == tenant_id]

    emp_index = _index_employees_by_pesel(employees)
    cust_index = _index_customers_by_pesel(customers)

    seen: set[str] = set()
    for entry in missing_list:
        pesel = _normalize_pesel(entry.get("PESEL"))
        if not pesel or pesel in seen:
            continue
        seen.add(pesel)

        if pesel in emp_index:
            plan.candidates.append(_build_from_employee(pesel, emp_index[pesel]))
        elif pesel in cust_index:
            plan.candidates.append(_build_from_customer(pesel, cust_index[pesel]))
        else:
            plan.not_found_pesels.append(pesel)

    return plan


# ─── Conversion to execute_employee_import rows ──────────────────────────────

def build_employee_import_rows(
    candidates: Iterable[OnboardingCandidate],
    *,
    data_od: int,
) -> list[dict]:
    """Convert onboard-ready candidates to rows for execute_employee_import.

    Caller is expected to filter ``candidates`` to ones where ``can_onboard``
    is true (use ``OnboardingPlan.can_onboard``). The returned rows include a
    ``skip_address`` flag for entries without a Polish address.
    """
    rows: list[dict] = []
    for cand in candidates:
        full_name = f"{cand.surname} {cand.name}".strip()
        row: dict[str, Any] = {
            "pesel": cand.pesel,
            "full_name": full_name,
            "birth_date": cand.date_of_birth,
            "id_card_no": cand.id_card_no,
            "passport_no": cand.passport_number,
            "phone": cand.phone,
            "data_od": data_od,
            # Urzad — used by execute_employee_import to resolve or create URZEDY row
            "urzad_code": cand.urzad_code,
            "urzad_code_from_reference": cand.urzad_code,
            "urzad_name": "",
            "urzad_name_from_reference": "",
            # Address (only used when skip_address is False)
            "voivodeship": "",
            "powiat": "",
            "gmina": "",
            "city": cand.city,
            "postal_code": cand.postal_code,
            "post_office": cand.city,  # poczta — fall back to city when not specified
            "street": cand.street,
            "house_no": cand.house_no,
            "flat_no": cand.flat_no,
            "country": cand.country,
            # New flag respected by execute_employee_import
            "skip_address": not cand.is_polish_address,
            # Metadata (helps logging / undo)
            "__onboarding_source": cand.source,
            "__onboarding_crm_id": cand.crm_id,
        }
        rows.append(row)
    return rows
