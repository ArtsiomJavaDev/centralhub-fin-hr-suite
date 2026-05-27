"""Golden-case tests for db/tax_calc_2026.py.

Each test is derived from a real payroll calculation and represents a
"sealed envelope" — if the tax engine is changed, these must fail and
force a deliberate review of the change.

Test naming convention:
  test_<function>__<scenario>
"""
from __future__ import annotations

from decimal import Decimal
from datetime import date

import pytest

from db.tax_calc_2026 import (
    # Rounding helpers
    zaokr_zus,
    zaokr_pit,
    # ZUS składki
    oblicz_skladki_zus_pracownik,
    oblicz_baze_proporcjonalna,
    # Umowy cywilnoprawne
    oblicz_umowe_zlecenie,
    oblicz_umowe_o_dzielo,
    # Recalculate from stored rates (verification path)
    recalculate_umowa_dzielo_from_rates,
    recalculate_umowa_zlecenie_from_rates,
    # Utils
    czy_pelny_zus_dla_zlecenia,
    czy_student_wolny_od_zus,
    min_placa_na_date,
    MINIMALNA_PLACA_MIESIECZNA,
    StawkiZUS,
)

D = Decimal


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 1. Zaokrąglenia
# ─────────────────────────────────────────────────────────────────────────────

class TestZaokrZus:
    def test_round_half_up(self):
        assert zaokr_zus(D("150.505")) == D("150.51")

    def test_round_half_up_down_side(self):
        assert zaokr_zus(D("150.504")) == D("150.50")

    def test_exact(self):
        assert zaokr_zus(D("100.00")) == D("100.00")


class TestZaokrPit:
    def test_half_down_on_half(self):
        # HALF_DOWN: .5 rounds toward zero → 153.5 → 153
        assert zaokr_pit(D("153.5")) == D("153")

    def test_above_half(self):
        assert zaokr_pit(D("153.51")) == D("154")

    def test_below_half(self):
        assert zaokr_pit(D("153.49")) == D("153")


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 2. Umowa o Dzieło
# ─────────────────────────────────────────────────────────────────────────────

class TestObliczUmoweODzielo:
    """No ZUS/health; only KUP and PIT."""

    def test_standard_50kup_12pit(self):
        wynik = oblicz_umowe_o_dzielo(D("1000"), kup_proc=D("50"), stawka_pit_proc=D("12"))
        # kup = 1000 * 50% = 500
        # dochod = 1000 - 500 = 500  → podstawa = zaokr_pit(500) = 500
        # podatek_raw = zaokr_zus(500 * 12 / 100) = zaokr_zus(60) = 60.00
        # zaliczka = zaokr_pit(60) = 60
        # netto = 1000 - 60 = 940
        assert wynik.kup_kwota == D("500.00")
        assert wynik.podstawa_opodatkowania == D("500")
        assert wynik.zaliczka_pit == D("60")
        assert wynik.netto == D("940.00")
        # No ZUS at all
        assert wynik.emerytalne_ubezp == D("0")
        assert wynik.zdrowotna == D("0")

    def test_standard_20kup_12pit(self):
        wynik = oblicz_umowe_o_dzielo(D("2000"), kup_proc=D("20"), stawka_pit_proc=D("12"))
        # kup = 2000 * 20% = 400
        # dochod = 2000 - 400 = 1600 → podstawa = 1600
        # podatek_raw = zaokr_zus(1600 * 12% / 100) = zaokr_zus(192) = 192.00
        # zaliczka = zaokr_pit(192) = 192
        # netto = 2000 - 192 = 1808
        assert wynik.kup_kwota == D("400.00")
        assert wynik.zaliczka_pit == D("192")
        assert wynik.netto == D("1808.00")

    def test_pit_zero(self):
        wynik = oblicz_umowe_o_dzielo(D("1500"), kup_proc=D("50"), stawka_pit_proc=D("0"))
        assert wynik.zaliczka_pit == D("0")
        assert wynik.netto == D("1500.00")

    def test_half_down_rounding_pit(self):
        # brutto=1000, kup=20% → kup_kwota=200, dochod=800, podstawa=800
        # podatek_raw = zaokr_zus(800 * 12 / 100) = zaokr_zus(96) = 96.00
        # zaliczka = zaokr_pit(96) = 96
        wynik = oblicz_umowe_o_dzielo(D("1000"), kup_proc=D("20"), stawka_pit_proc=D("12"))
        assert wynik.zaliczka_pit == D("96")

    def test_wysokie_brutto_50kup_12pit(self):
        wynik = oblicz_umowe_o_dzielo(D("10000"), kup_proc=D("50"), stawka_pit_proc=D("12"))
        assert wynik.kup_kwota == D("5000.00")
        assert wynik.zaliczka_pit == D("600")
        assert wynik.netto == D("9400.00")


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 3. Umowa Zlecenie — pełny ZUS
# ─────────────────────────────────────────────────────────────────────────────

class TestObliczUmoweZlecenie:
    """Full ZUS case (emerytalne + rentowe active, chorobowe=False by default)."""

    def test_standard_brutto_3000(self):
        wynik = oblicz_umowe_zlecenie(
            D("3000"),
            kup_proc=D("20"),
            stawka_pit_proc=D("12"),
        )
        # Emer_u = zaokr_zus(3000 * 9.76 / 100) = zaokr_zus(292.80) = 292.80
        # Rent_u = zaokr_zus(3000 * 1.50 / 100) = zaokr_zus(45.00) = 45.00
        # Razem_u_raw = 292.80 + 45.00 = 337.80
        # Podstawa_zdrowia = 3000 - 337.80 = 2662.20
        # kup = zaokr_zus(2662.20 * 20 / 100) = zaokr_zus(532.44) = 532.44
        # dochod_raw = 2662.20 - 532.44 = 2129.76
        # podstawa_op = zaokr_pit(2129.76) = 2130
        # podatek_raw = zaokr_zus(2130 * 12 / 100) = zaokr_zus(255.60) = 255.60
        # zaliczka = zaokr_pit(255.60) = 256
        # zdrowotna = zaokr_zus(2662.20 * 9 / 100) = zaokr_zus(239.598) = 239.60
        # netto = zaokr_zus(3000 - 337.80 - 239.60 - 256) = zaokr_zus(2166.60)
        assert wynik.emerytalne_ubezp == D("292.80")
        assert wynik.rentowe_ubezp == D("45.00")
        assert wynik.chorobowe_ubezp == D("0")
        assert wynik.kup_kwota == D("532.44")
        assert wynik.zaliczka_pit == D("256")
        assert wynik.zdrowotna == D("239.60")
        assert wynik.netto == D("2166.60")

    def test_student_case_all_zus_zero(self):
        """Student: all ZUS=0, PIT=0 → netto = brutto."""
        wynik = oblicz_umowe_zlecenie(
            D("2000"),
            kup_proc=D("20"),
            stawka_pit_proc=D("0"),
            emerytalne_aktywne=False,
            rentowe_aktywne=False,
            zdrowotne_aktywne=False,
        )
        assert wynik.emerytalne_ubezp == D("0")
        assert wynik.rentowe_ubezp == D("0")
        assert wynik.zdrowotna == D("0")
        assert wynik.zaliczka_pit == D("0")
        assert wynik.netto == D("2000.00")

    def test_zbieg_tytulow_no_zus(self):
        """Zbieg tytułów: emerytalne/rentowe=False (etat ≥ min. płaca)."""
        wynik = oblicz_umowe_zlecenie(
            D("1500"),
            kup_proc=D("20"),
            stawka_pit_proc=D("12"),
            emerytalne_aktywne=False,
            rentowe_aktywne=False,
        )
        assert wynik.emerytalne_ubezp == D("0")
        assert wynik.rentowe_ubezp == D("0")
        # Zdrowotna naliczana od pełnego brutto (brak składek ZUS)
        assert wynik.zdrowotna == zaokr_zus(D("1500") * D("9") / D("100"))


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 4. recalculate_umowa_dzielo_from_rates
# ─────────────────────────────────────────────────────────────────────────────

class TestRecalculateUmowaDzielo:
    def test_matches_oblicz_umowe_o_dzielo(self):
        """recalculate_* must match oblicz_* for the same inputs."""
        oblicz = oblicz_umowe_o_dzielo(D("2000"), kup_proc=D("50"), stawka_pit_proc=D("12"))
        recalc = recalculate_umowa_dzielo_from_rates(
            brutto=2000.0, kup_proc=50.0, stawka_podatku_proc=12.0
        )
        assert recalc.kup_kwota == oblicz.kup_kwota
        assert recalc.kwota_podatku == oblicz.zaliczka_pit
        # netto may differ by at most 0.01 due to payroll system x.99 display rule
        assert abs(float(recalc.kwota_do_wyplaty) - float(oblicz.netto)) <= 0.01

    def test_brutto_1000_kup50_pit12(self):
        r = recalculate_umowa_dzielo_from_rates(1000.0, 50.0, 12.0)
        assert r.kup_kwota == D("500.00")
        assert r.kwota_podatku == D("60")
        assert r.kwota_do_wyplaty == D("940.00")

    def test_brutto_1000_kup20_pit12(self):
        r = recalculate_umowa_dzielo_from_rates(1000.0, 20.0, 12.0)
        # kup=200, dochod=800, pit=96, netto=904
        assert r.kup_kwota == D("200.00")
        assert r.kwota_podatku == D("96")
        assert r.kwota_do_wyplaty == D("904.00")

    def test_zero_pit(self):
        r = recalculate_umowa_dzielo_from_rates(3000.0, 50.0, 0.0)
        assert r.kwota_podatku == D("0")
        assert r.kwota_do_wyplaty == D("3000.00")

    def test_all_zus_fields_zero(self):
        """UD has no ZUS — all ZUS fields must be Decimal(0)."""
        r = recalculate_umowa_dzielo_from_rates(5000.0, 50.0, 12.0)
        assert r.emerytalne_zleceniobiorca == D("0")
        assert r.zdrowotne_zleceniobiorca == D("0")
        assert r.chorobowe_zleceniobiorca == D("0")


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 5. recalculate_umowa_zlecenie_from_rates
# ─────────────────────────────────────────────────────────────────────────────

class TestRecalculateUmowaZlecenie:
    """Full-ZUS zlecenie, standard 2026 rates."""

    # Reusable standard-rate kwargs
    _FULL_ZUS = dict(
        kup_proc=20.0,
        stawka_podatku_proc=12.0,
        emerytalne_proc=19.52,
        rentowe_u_proc=1.50,
        rentowe_p_proc=6.50,
        chorobowe_proc=0.0,
        wypadkowe_proc=1.67,
        zdrowotne_proc=9.0,
        fp_proc=2.45,
        fgsp_proc=0.10,
    )

    def test_brutto_3000_full_zus(self):
        r = recalculate_umowa_zlecenie_from_rates(brutto=3000.0, **self._FULL_ZUS)
        # Should match oblicz_umowe_zlecenie result within 1 gr
        oblicz = oblicz_umowe_zlecenie(D("3000"), kup_proc=D("20"), stawka_pit_proc=D("12"))
        assert abs(float(r.kwota_do_wyplaty) - float(oblicz.netto)) <= 0.01

    def test_student_all_rates_zero(self):
        r = recalculate_umowa_zlecenie_from_rates(
            brutto=2000.0,
            kup_proc=0.0,
            stawka_podatku_proc=0.0,
            emerytalne_proc=0.0,
            rentowe_u_proc=0.0,
            rentowe_p_proc=0.0,
            chorobowe_proc=0.0,
            wypadkowe_proc=0.0,
            zdrowotne_proc=0.0,
            fp_proc=0.0,
            fgsp_proc=0.0,
        )
        assert r.kwota_do_wyplaty == D("2000.00")
        assert r.emerytalne_zleceniobiorca == D("0")
        assert r.zdrowotne_zleceniobiorca == D("0")

    def test_zbieg_no_emerytal_rental(self):
        """Zbieg tytułów: emerytalne/rentowe=0, tylko zdrowotna i PIT."""
        r = recalculate_umowa_zlecenie_from_rates(
            brutto=1500.0,
            kup_proc=20.0,
            stawka_podatku_proc=12.0,
            emerytalne_proc=0.0,
            rentowe_u_proc=0.0,
            rentowe_p_proc=0.0,
            chorobowe_proc=0.0,
            wypadkowe_proc=0.0,
            zdrowotne_proc=9.0,
            fp_proc=0.0,
            fgsp_proc=0.0,
        )
        assert r.emerytalne_zleceniobiorca == D("0")
        assert r.rentowe_zleceniobiorca == D("0")
        # zdrowotna = zaokr_zus(1500 * 9 / 100) = 135.00
        assert r.zdrowotne_zleceniobiorca == D("135.00")


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 6. Składki ZUS pracownicze
# ─────────────────────────────────────────────────────────────────────────────

class TestObliczSkladkiZus:
    def test_standard_pracownik(self):
        ubezp, platnik = oblicz_skladki_zus_pracownik(D("5000"))
        assert ubezp.emerytalne == zaokr_zus(D("5000") * D("9.76") / D("100"))
        assert ubezp.rentowe == zaokr_zus(D("5000") * D("1.50") / D("100"))
        assert platnik.emerytalne == zaokr_zus(D("5000") * D("9.76") / D("100"))
        assert platnik.rentowe == zaokr_zus(D("5000") * D("6.50") / D("100"))

    def test_limit_30x(self):
        """When narastajaco is already at the cap, emerytalne/rentowe must be zero."""
        from db.tax_calc_2026 import ROCZNA_PODSTAWA_LIMIT_30X
        ubezp, platnik = oblicz_skladki_zus_pracownik(
            D("5000"),
            podstawa_limit_narastajaco=ROCZNA_PODSTAWA_LIMIT_30X,
        )
        assert ubezp.emerytalne == D("0")
        assert ubezp.rentowe == D("0")
        assert platnik.emerytalne == D("0")
        assert platnik.rentowe == D("0")

    def test_chorobowe_disabled(self):
        ubezp, _ = oblicz_skladki_zus_pracownik(D("5000"), chorobowe_aktywne=False)
        assert ubezp.chorobowe == D("0")


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 7. Proporcjonalna baza ZUS
# ─────────────────────────────────────────────────────────────────────────────

class TestObliczBazeProporcjonalna:
    def test_full_month(self):
        result = oblicz_baze_proporcjonalna(D("5000"), 2026, 3, 31)
        assert result == D("5000.00")

    def test_zero_days(self):
        result = oblicz_baze_proporcjonalna(D("5000"), 2026, 3, 0)
        assert result == D("0")

    def test_half_month(self):
        # March has 31 days; 15 days worked
        result = oblicz_baze_proporcjonalna(D("4806"), 2026, 3, 15)
        expected = zaokr_zus(D("4806") / D("31") * D("15"))
        assert result == expected


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 8. Zbieg tytułów / student
# ─────────────────────────────────────────────────────────────────────────────

class TestCzyPelnyZusZlecenie:
    def test_no_etat_full_zus(self):
        assert czy_pelny_zus_dla_zlecenia(None) is True

    def test_etat_above_min_no_zus(self):
        assert czy_pelny_zus_dla_zlecenia(MINIMALNA_PLACA_MIESIECZNA) is False

    def test_etat_below_min_full_zus(self):
        assert czy_pelny_zus_dla_zlecenia(MINIMALNA_PLACA_MIESIECZNA - D("1")) is True


class TestCzyStudentWolnyOdZus:
    def test_student_under_26_free(self):
        birth = date(2001, 6, 1)
        payment = date(2026, 5, 1)  # age 24
        assert czy_student_wolny_od_zus(True, birth, payment) is True

    def test_student_over_26_not_free(self):
        birth = date(1998, 1, 1)
        payment = date(2026, 5, 1)  # age 28
        assert czy_student_wolny_od_zus(True, birth, payment) is False

    def test_not_student_not_free(self):
        birth = date(2003, 1, 1)
        payment = date(2026, 5, 1)
        assert czy_student_wolny_od_zus(False, birth, payment) is False

    def test_unknown_birth_conservative_false(self):
        assert czy_student_wolny_od_zus(True, None, date(2026, 5, 1)) is False


# ─────────────────────────────────────────────────────────────────────────────
# Sekcja 9. min_placa_na_date
# ─────────────────────────────────────────────────────────────────────────────

class TestMinPlacaNaDate:
    def test_2026_returns_constant(self):
        assert min_placa_na_date(date(2026, 1, 1)) == MINIMALNA_PLACA_MIESIECZNA
        assert min_placa_na_date(date(2026, 12, 31)) == MINIMALNA_PLACA_MIESIECZNA

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError):
            min_placa_na_date(date(2027, 1, 1))
