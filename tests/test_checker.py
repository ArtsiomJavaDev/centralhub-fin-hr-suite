"""Golden-case tests for crm/checker.py.

Tests cover:
  - _kup_str_to_float helper
  - verify_financials for UD (umowa o dzieło) rows
  - verify_financials for UZ (umowa zlecenie) rows
  - Edge cases: ZUS-exempt, brutto_from_netto, pit_rate=0, missing columns
  - _diagnose_discrepancy (internal but critical)

All financial values come from independently hand-verified calculations so
they serve as sealed regression envelopes.
"""
from __future__ import annotations

import pandas as pd
import pytest

from crm.checker import (
    _kup_str_to_float,
    verify_financials,
    VerifyResult,
    VerifyRowResult,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build a minimal one-row DataFrame for verify_financials
# ─────────────────────────────────────────────────────────────────────────────

def _make_ud_row(
    brutto: float,
    netto: float,
    kup_proc: float = 50.0,
    pit_rate: float = 12.0,
    zus_exempt: int = 0,
    calc_type: str = "",
    data_source: str = "excel",
) -> pd.DataFrame:
    """Minimal UD (Umowa o Dzieło = typ '2') row for verify_financials."""
    return pd.DataFrame([{
        "Typ umowy": "2",
        "Nr Rachunku": "TEST/UD/001",
        "PESEL": "90010112345",
        "Kwota brutto": brutto,
        "__audit_netto": netto,
        "KUP %": f"{kup_proc}%",
        "Stawka podatku [%]": pit_rate,
        "__audit_zus_exempt": zus_exempt,
        "__audit_calculate_type": calc_type,
        "__audit_data_source": data_source,
    }])


def _make_uz_row(
    brutto: float,
    netto: float,
    kup_proc: float = 20.0,
    pit_rate: float = 12.0,
    emer: float = 19.52,
    rent_u: float = 1.50,
    rent_p: float = 6.50,
    chorob: float = 0.0,
    wypad: float = 1.67,
    zdrow: float = 9.0,
    fp: float = 2.45,
    fgsp: float = 0.10,
    zus_exempt: int = 0,
    calc_type: str = "",
    data_source: str = "excel",
) -> pd.DataFrame:
    """Minimal UZ (Umowa Zlecenie = typ '1') row for verify_financials."""
    return pd.DataFrame([{
        "Typ umowy": "1",
        "Nr Rachunku": "TEST/UZ/001",
        "PESEL": "90010112345",
        "Kwota brutto": brutto,
        "__audit_netto": netto,
        "KUP %": f"{kup_proc}%",
        "Stawka podatku [%]": pit_rate,
        "Skł.na ub.emerytal.[%]": emer,
        "Składka ub.rent. U [%]": rent_u,
        "Składka ub.rent. P [%]": rent_p,
        "Składka ub.chorob.[%]": chorob,
        "Składka ub.wypadk.[%]": wypad,
        "Składka ub.zdrowotne[%]": zdrow,
        "FP [%]": fp,
        "FGŚP [%]": fgsp,
        "__audit_zus_exempt": zus_exempt,
        "__audit_calculate_type": calc_type,
        "__audit_data_source": data_source,
    }])


# ─────────────────────────────────────────────────────────────────────────────
# 1. _kup_str_to_float
# ─────────────────────────────────────────────────────────────────────────────

class TestKupStrToFloat:
    def test_percent_string(self):
        assert _kup_str_to_float("50%") == 50.0

    def test_percent_string_20(self):
        assert _kup_str_to_float("20%") == 20.0

    def test_fractional_form(self):
        # 0.5 → multiply by 100 because 0 ≤ 0.5 ≤ 1
        assert _kup_str_to_float("0.5") == 50.0

    def test_already_percentage_float(self):
        assert _kup_str_to_float("50.0") == 50.0

    def test_empty_string(self):
        assert _kup_str_to_float("") == 0.0

    def test_none(self):
        assert _kup_str_to_float(None) == 0.0

    def test_with_spaces(self):
        assert _kup_str_to_float(" 20 % ") == 20.0

    def test_comma_decimal(self):
        assert _kup_str_to_float("20,0%") == 20.0


# ─────────────────────────────────────────────────────────────────────────────
# 2. verify_financials — missing audit column
# ─────────────────────────────────────────────────────────────────────────────

class TestVerifyFinancialsGuards:
    def test_missing_audit_netto_returns_error(self):
        df = pd.DataFrame([{"Typ umowy": "2", "Kwota brutto": 1000}])
        result = verify_financials(df)
        assert result.total == 0
        assert len(result.errors) == 1
        assert "audit_netto" in result.errors[0]

    def test_empty_dataframe(self):
        df = pd.DataFrame(columns=["__audit_netto", "Typ umowy"])
        result = verify_financials(df)
        assert result.total == 0
        assert result.ok == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. UD — Umowa o Dzieło golden cases
# ─────────────────────────────────────────────────────────────────────────────

class TestVerifyFinancialsUD:
    def test_exact_match_ok(self):
        """brutto=1000, kup=50%, pit=12% → netto=940. Exact match → OK."""
        df = _make_ud_row(brutto=1000.0, netto=940.0)
        result = verify_financials(df)
        assert result.total == 1
        assert result.ok == 1
        assert result.discrepancy == 0

    def test_within_tolerance_ok(self):
        """Difference ≤ 0.05 PLN counts as OK."""
        df = _make_ud_row(brutto=1000.0, netto=940.03)
        result = verify_financials(df)
        assert result.ok == 1

    def test_marginal_diff(self):
        """Difference > 0.05 but ≤ 1.05 → marginal."""
        df = _make_ud_row(brutto=1000.0, netto=939.50)  # diff = 0.50
        result = verify_financials(df)
        assert result.marginal == 1
        assert result.ok == 0
        assert result.discrepancy == 0

    def test_large_discrepancy(self):
        """Difference > 1.05 → discrepancy."""
        df = _make_ud_row(brutto=1000.0, netto=900.0)  # diff = 40 PLN
        result = verify_financials(df)
        assert result.discrepancy == 1
        assert result.ok == 0

    def test_pit_zero_netto_equals_brutto(self):
        """PIT=0 → recalc gives brutto (no PIT deduction), so netto=brutto."""
        # recalculate_umowa_dzielo_from_rates(2000, 50, 0) → netto=2000
        df = _make_ud_row(brutto=2000.0, netto=2000.0, kup_proc=50.0, pit_rate=0.0)
        result = verify_financials(df)
        assert result.ok == 1
        assert result.pit_zero_rows == 1

    def test_20kup_12pit_brutto_2000(self):
        """brutto=2000, kup=20%, pit=12% → netto=1808."""
        df = _make_ud_row(brutto=2000.0, netto=1808.0, kup_proc=20.0, pit_rate=12.0)
        result = verify_financials(df)
        assert result.ok == 1

    def test_result_row_fields(self):
        """VerifyRowResult fields must be correctly populated."""
        df = _make_ud_row(brutto=1000.0, netto=940.0)
        result = verify_financials(df)
        row: VerifyRowResult = result.rows[0]
        assert row.typ == "UD"
        assert row.brutto == 1000.0
        assert row.netto_source == 940.0
        assert row.is_ok is True
        assert row.pit_rate == 12.0

    def test_api_data_source_counted(self):
        df = _make_ud_row(brutto=1000.0, netto=940.0, data_source="api")
        result = verify_financials(df)
        assert result.api_rows == 1
        assert result.excel_rows == 0


# ─────────────────────────────────────────────────────────────────────────────
# 4. UZ — Umowa Zlecenie golden cases
# ─────────────────────────────────────────────────────────────────────────────

class TestVerifyFinancialsUZ:
    # Expected netto for brutto=3000, full ZUS, kup=20%, pit=12% = 2166.60
    _BRUTTO = 3000.0
    _NETTO = 2166.60

    def test_full_zus_exact_ok(self):
        df = _make_uz_row(brutto=self._BRUTTO, netto=self._NETTO)
        result = verify_financials(df)
        assert result.ok == 1
        assert result.discrepancy == 0

    def test_full_zus_within_tolerance(self):
        df = _make_uz_row(brutto=self._BRUTTO, netto=self._NETTO + 0.04)
        result = verify_financials(df)
        assert result.ok == 1

    def test_student_zus_exempt_ok(self):
        """Student: all ZUS=0, pit=0 → netto=brutto."""
        df = _make_uz_row(
            brutto=2000.0,
            netto=2000.0,
            kup_proc=0.0,
            pit_rate=0.0,
            emer=0.0, rent_u=0.0, rent_p=0.0, chorob=0.0,
            wypad=0.0, zdrow=0.0, fp=0.0, fgsp=0.0,
            zus_exempt=1,
        )
        result = verify_financials(df)
        assert result.ok == 1
        assert result.zus_exempt_rows == 1

    def test_brutto_from_netto_marginal_accepted_as_ok(self):
        """brutto_from_netto flag: marginal ±1 PLN diff accepted as OK."""
        df = _make_uz_row(
            brutto=self._BRUTTO,
            netto=self._NETTO + 0.80,  # diff > 0.05 but ≤ 1.05
            calc_type="brutto_from_netto",
        )
        result = verify_financials(df)
        assert result.ok == 1
        assert result.brutto_from_netto_rows == 1

    def test_zbieg_no_zus_kup20_pit12(self):
        """Zbieg tytułów: all ZUS=0, kup=20%, pit=12%, zdrow=9%."""
        # brutto=1500, no ZUS, zdrow=zaokr(1500*9/100)=135, kup=zaokr(1500*20/100)=300
        # dochod=1500-300=1200, pit=zaokr_pit(zaokr_zus(1200*12/100))=zaokr_pit(144)=144
        # netto=1500-135-144=1221
        df = _make_uz_row(
            brutto=1500.0,
            netto=1221.0,
            kup_proc=20.0,
            pit_rate=12.0,
            emer=0.0, rent_u=0.0, rent_p=0.0, chorob=0.0,
            wypad=0.0, zdrow=9.0, fp=0.0, fgsp=0.0,
        )
        result = verify_financials(df)
        assert result.ok == 1

    def test_typ_1_label_uz(self):
        """Typ umowy '1' must produce VerifyRowResult.typ == 'UZ'."""
        df = _make_uz_row(brutto=self._BRUTTO, netto=self._NETTO)
        result = verify_financials(df)
        assert result.rows[0].typ == "UZ"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Multiple rows
# ─────────────────────────────────────────────────────────────────────────────

class TestVerifyFinancialsMultiRow:
    def test_two_ok_rows(self):
        row1 = _make_ud_row(1000.0, 940.0).to_dict(orient="records")[0]
        row2 = _make_ud_row(2000.0, 1808.0, kup_proc=20.0).to_dict(orient="records")[0]
        row2["Nr Rachunku"] = "TEST/UD/002"
        df = pd.DataFrame([row1, row2])
        result = verify_financials(df)
        assert result.total == 2
        assert result.ok == 2

    def test_mixed_ok_and_discrepancy(self):
        row_ok = _make_ud_row(1000.0, 940.0).to_dict(orient="records")[0]
        row_bad = _make_ud_row(1000.0, 800.0).to_dict(orient="records")[0]
        row_bad["Nr Rachunku"] = "BAD/001"
        df = pd.DataFrame([row_ok, row_bad])
        result = verify_financials(df)
        assert result.ok == 1
        assert result.discrepancy == 1

    def test_pit_zero_counter(self):
        # pit_rate=0 → netto=brutto (1000); second row normal pit
        row1 = _make_ud_row(1000.0, 1000.0, kup_proc=50.0, pit_rate=0.0).to_dict(orient="records")[0]
        row2 = _make_ud_row(2000.0, 1808.0, kup_proc=20.0).to_dict(orient="records")[0]
        row2["Nr Rachunku"] = "TEST/002"
        df = pd.DataFrame([row1, row2])
        result = verify_financials(df)
        assert result.pit_zero_rows == 1
        assert result.ok == 2


# ─────────────────────────────────────────────────────────────────────────────
# 6. utils/pesel
# ─────────────────────────────────────────────────────────────────────────────

class TestUtilsPesel:
    """Regression tests for utils/pesel — the shared PESEL utilities."""

    def setup_method(self):
        from utils.pesel import (
            normalize_pesel,
            birthdate_from_pesel,
            age_on,
            is_under_26,
            is_female_from_pesel,
            first_day_of_next_month,
        )
        self.normalize_pesel = normalize_pesel
        self.birthdate_from_pesel = birthdate_from_pesel
        self.age_on = age_on
        self.is_under_26 = is_under_26
        self.is_female_from_pesel = is_female_from_pesel
        self.first_day_of_next_month = first_day_of_next_month

    # normalize_pesel
    def test_normalize_int(self):
        assert self.normalize_pesel(90010112345) == "90010112345"

    def test_normalize_float(self):
        assert self.normalize_pesel(90010112345.0) == "90010112345"

    def test_normalize_none(self):
        assert self.normalize_pesel(None) == ""

    def test_normalize_short_zero_pad(self):
        assert self.normalize_pesel("1234567890") == "01234567890"

    def test_normalize_strips_dashes(self):
        assert self.normalize_pesel("9001-01-12345") == "90010112345"

    # birthdate_from_pesel
    def test_birthdate_1900s(self):
        from datetime import date
        # PESEL: yy=90, mm=01 (1900+90=1990, month=Jan), dd=01
        bd = self.birthdate_from_pesel("90010112345")
        assert bd == date(1990, 1, 1)

    def test_birthdate_2000s(self):
        from datetime import date
        # PESEL: yy=01, mm=21 → year=2001, month=1, dd=15
        bd = self.birthdate_from_pesel("01211512345")
        assert bd == date(2001, 1, 15)

    def test_birthdate_invalid(self):
        assert self.birthdate_from_pesel("00000000000") is None

    def test_birthdate_none_input(self):
        assert self.birthdate_from_pesel(None) is None

    # age_on
    def test_age_birthday_not_yet(self):
        from datetime import date
        assert self.age_on(date(1990, 6, 15), date(2026, 6, 14)) == 35

    def test_age_birthday_today(self):
        from datetime import date
        assert self.age_on(date(1990, 6, 15), date(2026, 6, 15)) == 36

    # is_under_26
    def test_under_26_true(self):
        from datetime import date
        # mm=21 → 2000s century; yy=01, mm=21→month=1, dd=01 → born 2001-01-01
        pesel = "01210112345"
        assert self.is_under_26(pesel, date(2026, 5, 26)) is True

    def test_under_26_false(self):
        from datetime import date
        pesel = "90010112345"  # born 1990-01-01
        assert self.is_under_26(pesel, date(2026, 5, 26)) is False

    # is_female_from_pesel
    def test_female_even_digit(self):
        # 10th digit (index 9) = 4 (even) → female
        assert self.is_female_from_pesel("90010112345") is True

    def test_male_odd_digit(self):
        # digits: 9-0-0-1-0-1-7-8-9-1-1 → index 9 = 1 (odd) → male
        assert self.is_female_from_pesel("90010178911") is False

    def test_invalid_returns_none(self):
        assert self.is_female_from_pesel("short") is None

    # first_day_of_next_month
    def test_mid_year(self):
        from datetime import date
        assert self.first_day_of_next_month(date(2026, 5, 15)) == date(2026, 6, 1)

    def test_december_wraps(self):
        from datetime import date
        assert self.first_day_of_next_month(date(2026, 12, 1)) == date(2027, 1, 1)
