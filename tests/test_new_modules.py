"""Tests for newly introduced CRM modules.

Covers:
  - crm/api_client.py  helper functions
  - crm/formatter.py   PIT-rate inference + ZUS-rate resolver
  - crm/onboarding.py  address/date/urzad helpers + candidate building
  - importer/utils.py  _safe_clarion_days tz-aware fix
"""
from __future__ import annotations

import pandas as pd
import pytest

# ── module-level imports (avoids class-level binding to self) ────────────────
from crm.api_client import (
    _bool_flag,
    _date_in_month,
    _is_valid_pesel,
    _normalize_pesel,
    _resolve_bill_person,
)
from crm.formatter import (
    _resolve_uz_rates,
    _SPECIAL_CHOROBOWE_PESELS,
    format_crm_report,
    infer_pit_rate_from_podatek,
)
from crm.onboarding import (
    _build_from_employee,
    _is_polish_address,
    _normalize_urzad_code,
    _parse_iso_date,
    _split_address,
    build_employee_import_rows,
)
from crm.reconciliation import (
    CrmBillCheck,
    RachunkiReconciliation,
    PayrollRachunek,
)
from importer.utils import _CLARION_BASE, _safe_clarion_days


# ─────────────────────────────────────────────────────────────────────────────
# crm/api_client.py  helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestIsValidPesel:
    def test_valid_1900s(self):
        assert _is_valid_pesel("90010112345") is True

    def test_valid_2000s(self):
        assert _is_valid_pesel("01211512345") is True

    def test_wrong_length_10(self):
        assert _is_valid_pesel("9001011234") is False

    def test_wrong_length_12(self):
        assert _is_valid_pesel("900101123456") is False

    def test_invalid_month_00(self):
        assert _is_valid_pesel("90000112345") is False

    def test_invalid_month_13(self):
        assert _is_valid_pesel("90130112345") is False

    def test_valid_month_21_2000s(self):
        assert _is_valid_pesel("00210112345") is True

    def test_invalid_month_00_all_zeros(self):
        # "00000000000" has month=00 which is not a valid PESEL month encoding
        assert _is_valid_pesel("00000000000") is False

    def test_day_00_invalid(self):
        assert _is_valid_pesel("90010012345") is False

    def test_not_digits(self):
        assert _is_valid_pesel("9001011234X") is False


class TestBoolFlag:
    def test_true_bool(self):
        assert _bool_flag(True) == 1

    def test_false_bool(self):
        assert _bool_flag(False) == 0

    def test_string_true(self):
        assert _bool_flag("true") == 1

    def test_string_yes(self):
        assert _bool_flag("yes") == 1

    def test_string_false(self):
        assert _bool_flag("false") == 0

    def test_string_no(self):
        assert _bool_flag("no") == 0

    def test_integer_1(self):
        assert _bool_flag(1) == 1

    def test_integer_0(self):
        assert _bool_flag(0) == 0

    def test_none(self):
        assert _bool_flag(None) is None

    def test_null_string(self):
        assert _bool_flag("null") is None

    def test_empty_string(self):
        assert _bool_flag("") is None


class TestNormalizePeselApiClient:
    def test_int_input(self):
        assert _normalize_pesel(90010112345) == "90010112345"

    def test_float_input(self):
        assert _normalize_pesel(90010112345.0) == "90010112345"

    def test_short_strips_to_empty(self):
        assert _normalize_pesel("1234567890") == ""

    def test_jwt_like_returns_empty(self):
        assert _normalize_pesel("eyJhbGciOiJIUzI1NiJ9") == ""

    def test_none_returns_empty(self):
        assert _normalize_pesel(None) == ""


class TestDateInMonth:
    def test_same_month(self):
        assert _date_in_month("2026-03-15", 2026, 3) is True

    def test_wrong_month(self):
        assert _date_in_month("2026-04-15", 2026, 3) is False

    def test_wrong_year(self):
        assert _date_in_month("2025-03-15", 2026, 3) is False

    def test_iso_with_time(self):
        assert _date_in_month("2026-03-01T00:00:00Z", 2026, 3) is True

    def test_none_returns_false(self):
        assert _date_in_month(None, 2026, 3) is False

    def test_empty_string(self):
        assert _date_in_month("", 2026, 3) is False


class TestResolveBillPerson:
    def _user(self, user_id, pesel):
        return {"id": user_id, "pesel_number": pesel, "name": "John", "surname": "Doe"}

    def _salary_request(self, sr_id, created_by):
        return {"id": sr_id, "created_by": created_by, "paid_at": "2026-03-31"}

    def _contract(self, employee_id=None, client_id=None):
        return {"id": 1, "employee_id": employee_id, "client_id": client_id}

    def test_user_with_valid_pesel_wins(self):
        user = self._user(10, "90010112345")
        sr = self._salary_request(5, 10)
        bill = {"id": 1, "salary_request_id": 5}
        contract = self._contract(employee_id=99)
        emp = {"id": 99, "pesel_number": "80010112345", "name": "Old", "surname": "Employee"}
        result = _resolve_bill_person(
            bill=bill, contract=contract,
            salary_requests_by_id={5: sr}, users_by_id={10: user},
            employees_by_id={99: emp}, customers_by_id={},
        )
        assert result is user

    def test_user_without_pesel_falls_back_to_employee(self):
        user = self._user(10, "")
        sr = self._salary_request(5, 10)
        bill = {"id": 1, "salary_request_id": 5}
        emp = {"id": 99, "pesel_number": "80010112345", "name": "Old", "surname": "Employee"}
        contract = self._contract(employee_id=99)
        result = _resolve_bill_person(
            bill=bill, contract=contract,
            salary_requests_by_id={5: sr}, users_by_id={10: user},
            employees_by_id={99: emp}, customers_by_id={},
        )
        assert result is emp

    def test_no_salary_request_falls_back_to_employee(self):
        bill = {"id": 1, "salary_request_id": None}
        emp = {"id": 99, "pesel_number": "80010112345"}
        contract = self._contract(employee_id=99)
        result = _resolve_bill_person(
            bill=bill, contract=contract,
            salary_requests_by_id={}, users_by_id={},
            employees_by_id={99: emp}, customers_by_id={},
        )
        assert result is emp

    def test_no_person_returns_none(self):
        bill = {"id": 1, "salary_request_id": None}
        contract = self._contract()
        result = _resolve_bill_person(
            bill=bill, contract=contract,
            salary_requests_by_id={}, users_by_id={},
            employees_by_id={}, customers_by_id={},
        )
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# crm/formatter.py  PIT-rate inference + ZUS resolver
# ─────────────────────────────────────────────────────────────────────────────

class TestInferPitRateFromPodatek:
    def test_podatek_zero_returns_0(self):
        assert infer_pit_rate_from_podatek(0.0, 1000.0) == 0.0

    def test_podatek_12pct_brutto_1000_kup50(self):
        # brutto=1000, kup=50%, pit=12% → base=500, podatek=60
        assert infer_pit_rate_from_podatek(60.0, 1000.0, "50%") == 12.0

    def test_podatek_32pct_brutto_1000_kup50(self):
        # brutto=1000, kup=50%, pit=32% → base=500, podatek=160
        assert infer_pit_rate_from_podatek(160.0, 1000.0, "50%") == 32.0

    def test_podatek_12pct_brutto_2000_kup20(self):
        # brutto=2000, kup=20% → base=1600, pit=192
        assert infer_pit_rate_from_podatek(192.0, 2000.0, "20%") == 12.0

    def test_podatek_none_returns_default(self):
        assert infer_pit_rate_from_podatek(None, 1000.0) == 12.0

    def test_brutto_zero_returns_default(self):
        assert infer_pit_rate_from_podatek(100.0, 0.0) == 12.0


class TestResolveUzRates:
    def test_student_api_flag_returns_all_zero(self):
        warnings: list[str] = []
        marker, rates = _resolve_uz_rates(
            pesel="90010112345", pracownik="Jan Kowalski",
            brutto=2000.0, netto_src=2000.0,
            api_is_student=True, api_zus_chor=None,
            data_source="api", warnings=warnings,
        )
        assert marker == 1
        assert all(v == 0.0 for v in rates.values())
        assert len(warnings) == 1

    def test_no_student_standard_rates(self):
        warnings: list[str] = []
        marker, rates = _resolve_uz_rates(
            pesel="90010112345", pracownik="Jan Kowalski",
            brutto=2000.0, netto_src=1000.0,
            api_is_student=False, api_zus_chor=False,
            data_source="api", warnings=warnings,
        )
        assert marker == 0
        assert rates["Skł.na ub.emerytal.[%]"] == 19.52
        assert rates["Składka ub.chorob.[%]"] == 0.0

    def test_zus_chorobowe_api_flag_sets_2_45(self):
        warnings: list[str] = []
        marker, rates = _resolve_uz_rates(
            pesel="90010112345", pracownik="Test",
            brutto=2000.0, netto_src=1000.0,
            api_is_student=False, api_zus_chor=True,
            data_source="api", warnings=warnings,
        )
        assert rates["Składka ub.chorob.[%]"] == 2.45

    def test_excel_netto_approx_brutto_returns_zero(self):
        warnings: list[str] = []
        marker, rates = _resolve_uz_rates(
            pesel="90010112345", pracownik="Test",
            brutto=2000.0, netto_src=2000.50,
            api_is_student=None, api_zus_chor=None,
            data_source="excel", warnings=warnings,
        )
        assert marker == 1
        assert all(v == 0.0 for v in rates.values())

    def test_special_chorobowe_pesel_excel_fallback(self):
        pesel = next(iter(_SPECIAL_CHOROBOWE_PESELS))
        warnings: list[str] = []
        marker, rates = _resolve_uz_rates(
            pesel=pesel, pracownik="Special",
            brutto=2000.0, netto_src=1000.0,
            api_is_student=None, api_zus_chor=None,
            data_source="excel", warnings=warnings,
        )
        assert rates["Składka ub.chorob.[%]"] == 2.45


# ─────────────────────────────────────────────────────────────────────────────
# crm/formatter.py  format_crm_report integration
# ─────────────────────────────────────────────────────────────────────────────

def _make_raw_row(
    brutto: float = 1000.0,
    netto: float = 940.0,
    kup: str = "50%",
    podatek: float = 60.0,
    typ: str = "Umowa o Dzieło",
    pesel: str = "90010112345",
    nr_rachunku: str = "TEST/001",
    payment_date: str = "2026-03-31",
) -> pd.DataFrame:
    return pd.DataFrame([[
        None, "UMW/001", nr_rachunku, typ, "Jan Kowalski",
        pesel, netto, brutto, kup, "01/01/2026", podatek,
        payment_date, 0.0,
    ]])


class TestFormatCrmReport:
    def test_ud_row_creates_correct_type_code(self):
        df, result = format_crm_report(_make_raw_row())
        assert result.ud_count == 1
        assert result.uz_count == 0
        assert df.iloc[0]["Typ umowy"] == "2"

    def test_uz_row_creates_correct_type_code(self):
        df, result = format_crm_report(
            _make_raw_row(typ="Umowa Zlecenie", netto=2166.60, brutto=3000.0, podatek=100.0)
        )
        assert result.uz_count == 1
        assert df.iloc[0]["Typ umowy"] == "1"

    def test_ud_pit_inferred_12(self):
        df, _ = format_crm_report(_make_raw_row(brutto=1000.0, netto=940.0, podatek=60.0))
        assert df.iloc[0]["Stawka podatku [%]"] == 12.0

    def test_ud_pit_inferred_32(self):
        df, _ = format_crm_report(_make_raw_row(brutto=1000.0, netto=840.0, podatek=160.0))
        assert df.iloc[0]["Stawka podatku [%]"] == 32.0

    def test_api_row_uses_vat_field(self):
        row = pd.DataFrame([[
            None, "UMW/001", "TEST/002", "Umowa o Dzieło", "Jan Kowalski",
            "90010112345", 940.0, 1000.0, "50%", "01/01/2026", 0.0,
            "2026-03-31", 0.0,
            32.0,    # pos13 = vat = 32%
            None, None, "", None, None, None,
        ]])
        df, result = format_crm_report(row)
        assert df.iloc[0]["Stawka podatku [%]"] == 32.0
        assert df.iloc[0]["__audit_data_source"] == "api"

    def test_api_unexpected_vat_defaults_to_12(self):
        row = pd.DataFrame([[
            None, "UMW/001", "TEST/003", "Umowa o Dzieło", "Jan Kowalski",
            "90010112345", 940.0, 1000.0, "50%", "01/01/2026", 0.0,
            "2026-03-31", 0.0,
            99.0,    # unexpected vat
            None, None, "", None, None, None,
        ]])
        df, result = format_crm_report(row)
        assert df.iloc[0]["Stawka podatku [%]"] == 12.0
        assert any("nieoczekiwane" in w for w in result.warnings)

    def test_no_pesel_row_is_skipped(self):
        df, result = format_crm_report(_make_raw_row(pesel=""))
        assert result.total_rows == 0
        assert result.skipped_rows == 1

    def test_student_api_flag_zeros_zus(self):
        row = pd.DataFrame([[
            None, "UMW/001", "TEST/004", "Umowa Zlecenie", "Jan Kowalski",
            "90010112345", 2000.0, 2000.0, "20%", "01/01/2026", 0.0,
            "2026-03-31", 0.0,
            0.0,   # vat=0
            1,     # is_student=True
            None, "", None, None, None,
        ]])
        df, result = format_crm_report(row)
        assert df.iloc[0]["Skł.na ub.emerytal.[%]"] == 0.0
        assert df.iloc[0]["__audit_is_student"] == 1
        assert df.iloc[0]["__audit_zus_exempt"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# crm/onboarding.py  helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestIsPolishAddress:
    def test_country_polska(self):
        assert _is_polish_address("Polska", None) is True

    def test_country_poland_en(self):
        assert _is_polish_address("Poland", None) is True

    def test_country_pl_code(self):
        assert _is_polish_address("PL", None) is True

    def test_country_case_insensitive(self):
        assert _is_polish_address("POLSKA", None) is True

    def test_postal_code_polish_format(self):
        assert _is_polish_address(None, "01-234") is True

    def test_postal_code_non_polish(self):
        assert _is_polish_address(None, "12345") is False

    def test_foreign_country_non_polish_postal(self):
        assert _is_polish_address("Ukraine", "12345") is False

    def test_no_info(self):
        assert _is_polish_address(None, None) is False

    def test_empty_strings(self):
        assert _is_polish_address("", "") is False


class TestSplitAddress:
    def test_simple_number(self):
        street, house, flat = _split_address("ul. Długa 5")
        assert street == "ul. Długa"
        assert house == "5"
        assert flat == ""

    def test_number_with_flat(self):
        street, house, flat = _split_address("ul. Krótka 3/7")
        assert street == "ul. Krótka"
        assert house == "3"
        assert flat == "7"

    def test_number_with_letter(self):
        _, house, _ = _split_address("ul. Nowa 12A")
        assert house == "12A"

    def test_no_number(self):
        street, house, flat = _split_address("al. Jerozolimskie")
        assert street == "al. Jerozolimskie"
        assert house == ""

    def test_none_input(self):
        assert _split_address(None) == ("", "", "")

    def test_empty_input(self):
        assert _split_address("") == ("", "", "")


class TestNormalizeUrzadCode:
    def test_three_digit_pads_to_four(self):
        assert _normalize_urzad_code("840") == "0840"

    def test_already_four_digits(self):
        assert _normalize_urzad_code("0840") == "0840"

    def test_one_digit_pads(self):
        assert _normalize_urzad_code("5") == "0005"

    def test_none_returns_empty(self):
        assert _normalize_urzad_code(None) == ""

    def test_empty_returns_empty(self):
        assert _normalize_urzad_code("") == ""

    def test_non_numeric_returns_empty(self):
        assert _normalize_urzad_code("ABC") == ""

    def test_float_string(self):
        assert _normalize_urzad_code("840.0") == "0840"


class TestParseIsoDate:
    def test_iso_datetime_with_z(self):
        assert _parse_iso_date("1990-01-01T00:00:00Z") == "01/01/1990"

    def test_iso_datetime_with_offset(self):
        assert _parse_iso_date("1990-06-15T12:00:00+02:00") == "15/06/1990"

    def test_plain_date(self):
        assert _parse_iso_date("2000-12-31") == "31/12/2000"

    def test_none_returns_empty(self):
        assert _parse_iso_date(None) == ""

    def test_empty_returns_empty(self):
        assert _parse_iso_date("") == ""

    def test_nan_returns_empty(self):
        assert _parse_iso_date("nan") == ""


class TestBuildOnboardingCandidate:
    def _make_employee(self, **kwargs):
        defaults = {
            "id": 1,
            "name": "Piotr",
            "surname": "Nowak",
            "pesel_number": "90010112345",
            "date_of_birth": "1990-01-01T00:00:00Z",
            "passport_number": "AB1234567",
            "city": "Warszawa",
            "postal_code": "00-001",
            "address": "ul. Testowa 5/3",
            "country": "Polska",
            "authority_agency": "840",
        }
        defaults.update(kwargs)
        return defaults

    def test_full_employee_no_blockers(self):
        emp = self._make_employee()
        cand = _build_from_employee("90010112345", emp)
        assert cand.can_onboard is True
        assert cand.urzad_code == "0840"
        assert cand.city == "Warszawa"
        assert cand.house_no == "5"
        assert cand.flat_no == "3"
        assert cand.date_of_birth == "01/01/1990"

    def test_missing_dob_creates_blocker(self):
        emp = self._make_employee(date_of_birth=None)
        cand = _build_from_employee("90010112345", emp)
        assert cand.can_onboard is False
        assert any("urodzenia" in b for b in cand.blockers)

    def test_missing_passport_creates_blocker(self):
        emp = self._make_employee(passport_number=None)
        cand = _build_from_employee("90010112345", emp)
        assert cand.can_onboard is False
        assert any("paszportu" in b for b in cand.blockers)

    def test_non_polish_address_no_address_fields(self):
        emp = self._make_employee(country="Ukraine", postal_code="12345", city="Kyiv")
        cand = _build_from_employee("90010112345", emp)
        assert cand.is_polish_address is False
        assert cand.city == ""
        assert any("niepolski" in w for w in cand.warnings)

    def test_skip_address_flag_in_import_rows(self):
        emp = self._make_employee(country="Ukraine", postal_code="12345", city="Kyiv")
        cand = _build_from_employee("90010112345", emp)
        rows = build_employee_import_rows([cand], data_od=82000)
        assert rows[0]["skip_address"] is True

    def test_polish_address_no_skip(self):
        emp = self._make_employee()
        cand = _build_from_employee("90010112345", emp)
        rows = build_employee_import_rows([cand], data_od=82000)
        assert rows[0]["skip_address"] is False


# ─────────────────────────────────────────────────────────────────────────────
# importer/utils.py  _safe_clarion_days tz fix
# ─────────────────────────────────────────────────────────────────────────────

class TestSafeClarionDays:
    def test_naive_timestamp(self):
        ts = pd.Timestamp("2026-01-01")
        result = _safe_clarion_days(ts)
        assert isinstance(result, int)
        assert result > 0

    def test_utc_aware_equals_naive(self):
        naive = _safe_clarion_days(pd.Timestamp("2026-01-01"))
        aware = _safe_clarion_days(pd.Timestamp("2026-01-01", tz="UTC"))
        assert naive == aware

    def test_positive_offset_keeps_local_date(self):
        # +02:00 timezone: local time 2026-03-15 → same Clarion day as naive
        ts_aware = pd.Timestamp("2026-03-15 02:00:00+02:00")
        ts_naive = pd.Timestamp("2026-03-15")
        assert _safe_clarion_days(ts_aware) == _safe_clarion_days(ts_naive)

    def test_clarion_epoch_offset(self):
        ts = _CLARION_BASE + pd.Timedelta(days=100)
        assert _safe_clarion_days(ts) == 100


# ─────────────────────────────────────────────────────────────────────────────
# crm/reconciliation.py  dataclass properties
# ─────────────────────────────────────────────────────────────────────────────

def _ok_bill(nr: str) -> CrmBillCheck:
    return CrmBillCheck(nr_rachunku=nr, bill_id=1, status="ok")


def _blocked_bill(nr: str, reason: str = "no_pesel") -> CrmBillCheck:
    return CrmBillCheck(nr_rachunku=nr, bill_id=2, status="blocked", reason=reason)


def _empty_report() -> RachunkiReconciliation:
    return RachunkiReconciliation(year=2026, month=3, tenant_id=1)


class TestRachunkiReconciliationProperties:
    def test_hard_errors_counts_only_ok_missing(self):
        r = _empty_report()
        r.crm_missing_in_payroll = [_ok_bill("NR/001"), _ok_bill("NR/002"), _blocked_bill("NR/003")]
        assert r.hard_errors == 2

    def test_crm_missing_importable(self):
        r = _empty_report()
        r.crm_missing_in_payroll = [_ok_bill("NR/001"), _blocked_bill("NR/002")]
        assert len(r.crm_missing_importable) == 1

    def test_crm_missing_blocked(self):
        r = _empty_report()
        r.crm_missing_in_payroll = [_ok_bill("NR/001"), _blocked_bill("NR/002"), _blocked_bill("NR/003")]
        assert len(r.crm_missing_blocked) == 2

    def test_explanatory_differences(self):
        r = _empty_report()
        payroll = PayrollRachunek(nr_rachunku="NR/X", data_wyplaty=None)
        r.date_mismatch = [(_ok_bill("NR/X"), payroll)]
        r.payroll_month_exists_in_crm_other_date = [payroll]
        assert r.explanatory_differences == 2
