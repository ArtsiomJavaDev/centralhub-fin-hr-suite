"""Pure financial calculation functions for umowy cywilnoprawne.

Extracted from db/service.py so that these Decimal-based calculations can be
tested and reasoned about independently of the DatabaseService class and any
SQLAlchemy/ODBC imports.

These functions replicate the payroll system «Wylicz» button logic exactly:
  * ROUND_HALF_UP for intermediate ZUS/ZDR values (grosz precision)
  * ROUND_HALF_DOWN for PIT advance (whole złoty, "half goes down")
  * Dual-candidate netto selection to match payroll system display
  * x,99 → next whole złoty rounding in the payout line

Public API
----------
calculate_umowa_financials(brutto, kup_proc, stawka_podatku_proc,
                           emerytalne_proc, rentowe_u_proc, rentowe_p_proc,
                           chorobowe_proc, wypadkowe_proc, zdrowotne_proc,
                           fp_proc, fgsp_proc)  → dict[str, float]

calculate_umowa_o_dzielo_financials(brutto, kup_proc,
                                    stawka_podatku_proc)  → dict[str, float]
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_DOWN, ROUND_HALF_UP


# Module-level defaults (same values as in service.py).
PIT_DEFAULT_STAWKA = 12.0


def calculate_umowa_financials(
    brutto: float,
    kup_proc: float,
    stawka_podatku_proc: float,
    emerytalne_proc: float,
    rentowe_u_proc: float,
    rentowe_p_proc: float,
    chorobowe_proc: float,
    wypadkowe_proc: float,
    zdrowotne_proc: float,
    fp_proc: float,
    fgsp_proc: float,
) -> dict[str, float]:
    """Replicates payroll system's «Wylicz» button for umowa cywilno-prawna (zlecenie).

    Standard Polish ZUS rules for umowa zlecenia:
      * emerytalne (e.g. 19.52%) split equally between zleceniobiorca/zleceniodawca
      * rentowe split as separate inputs: rent_u (zleceniobiorca) + rent_p (zleceniodawca)
      * chorobowe (zleceniobiorca only, voluntary)
      * wypadkowe (zleceniodawca only)
      * zdrowotne (zleceniobiorca only) on podstawa = brutto − składki_zleceniobiorca
      * FP, FGSP (zleceniodawca only)
      * KUP applied to (brutto − składki_zleceniobiorca)
      * PIT HALF_DOWN to whole złoty; payout uses dual-candidate payroll system logic

    For umowa o dzieło (all ZUS/ZDR rates = 0) this degrades naturally.
    """
    def d(value: float) -> Decimal:
        return Decimal(str(value))

    def r2(value: Decimal | float) -> float:
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def r0_pit(value: Decimal | float) -> float:
        # payroll system «Wylicz» rounds PIT base and advance with HALF_DOWN — values ending
        # at .50 go down (e.g. 6170.50 → 6170, not 6171).
        return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_DOWN))

    brutto_d = d(brutto)

    emerytalne_u_proc_d = d(emerytalne_proc) / d(2)
    emerytalne_u_raw = brutto_d * emerytalne_u_proc_d / d(100)
    emerytalne_p_raw = brutto_d * emerytalne_u_proc_d / d(100)
    rentowe_u_raw = brutto_d * d(rentowe_u_proc) / d(100)
    rentowe_p_raw = brutto_d * d(rentowe_p_proc) / d(100)
    chorobowe_u_raw = brutto_d * d(chorobowe_proc) / d(100)
    wypadkowe_p_raw = brutto_d * d(wypadkowe_proc) / d(100)
    fp_raw = brutto_d * d(fp_proc) / d(100)
    fgsp_raw = brutto_d * d(fgsp_proc) / d(100)

    emerytalne_zleceniobiorca = r2(emerytalne_u_raw)
    emerytalne_zleceniodawca = r2(emerytalne_p_raw)
    rentowe_zleceniobiorca = r2(rentowe_u_raw)
    rentowe_zleceniodawca = r2(rentowe_p_raw)
    chorobowe_zleceniobiorca = r2(chorobowe_u_raw)
    wypadkowe_zleceniodawca = r2(wypadkowe_p_raw)
    fp_kwota = r2(fp_raw)
    fgsp_kwota = r2(fgsp_raw)

    skladki_zleceniobiorca_raw = emerytalne_u_raw + rentowe_u_raw + chorobowe_u_raw
    podstawa_raw = brutto_d - skladki_zleceniobiorca_raw
    zdrowotne_raw = podstawa_raw * d(zdrowotne_proc) / d(100)
    kup_raw = podstawa_raw * d(kup_proc) / d(100)
    dochod_raw = brutto_d - skladki_zleceniobiorca_raw - kup_raw
    podstawa_opodatkowania = r0_pit(dochod_raw)
    stawka_podatku = r2(stawka_podatku_proc)
    podatek_naliczony = r2(d(podstawa_opodatkowania) * d(stawka_podatku) / d(100))
    zaliczka_podatku = r0_pit(podatek_naliczony)

    skladki_zleceniobiorca = r2(skladki_zleceniobiorca_raw)
    zdrowotne_zleceniobiorca = r2(zdrowotne_raw)
    kup_kwota = r2(kup_raw)
    dochod = r2(dochod_raw)

    # payroll system payout: keep two candidates, pick by whether tax rounding went up.
    net_raw = Decimal(
        str(r2(brutto_d - skladki_zleceniobiorca_raw - zdrowotne_raw - d(zaliczka_podatku)))
    )
    podstawa_rounded = brutto_d - d(r2(skladki_zleceniobiorca_raw))
    zdrowotne_rounded = d(r2(podstawa_rounded * d(zdrowotne_proc) / d(100)))
    net_rounded_components = Decimal(
        str(r2(brutto_d - d(r2(skladki_zleceniobiorca_raw)) - zdrowotne_rounded - d(zaliczka_podatku)))
    )
    selected_net = net_rounded_components if d(zaliczka_podatku) > d(podatek_naliczony) else net_raw

    # Preserve observed payroll display rule: x,99 → next whole złoty.
    next_zl = selected_net.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if next_zl > selected_net and (next_zl - selected_net) <= Decimal("0.01"):
        kwota_do_wyplaty = float(next_zl)
    else:
        kwota_do_wyplaty = float(selected_net)

    return {
        "emerytalne_zleceniobiorca": emerytalne_zleceniobiorca,
        "emerytalne_zleceniodawca": emerytalne_zleceniodawca,
        "rentowe_zleceniobiorca": rentowe_zleceniobiorca,
        "rentowe_zleceniodawca": rentowe_zleceniodawca,
        "chorobowe_zleceniobiorca": chorobowe_zleceniobiorca,
        "wypadkowe_zleceniodawca": wypadkowe_zleceniodawca,
        "zdrowotne_zleceniobiorca": zdrowotne_zleceniobiorca,
        "fp_kwota": fp_kwota,
        "fgsp_kwota": fgsp_kwota,
        "kup_kwota": kup_kwota,
        "dochod": dochod,
        "stawka_podatku": stawka_podatku,
        "kwota_podatku": zaliczka_podatku,
        "kwota_do_wyplaty": kwota_do_wyplaty,
    }


def calculate_umowa_o_dzielo_financials(
    brutto: float,
    kup_proc: float,
    stawka_podatku_proc: float = PIT_DEFAULT_STAWKA,
) -> dict[str, float]:
    """Like calculate_umowa_financials but without ZUS/ZDR contributions.

    Podstawa KUP = pełne brutto (no ZUS deductions from the KUP base).
    """
    def d(value: float) -> Decimal:
        return Decimal(str(value))

    def r2(value: Decimal | float) -> float:
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def r0_pit(value: Decimal | float) -> float:
        return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_DOWN))

    brutto_d = d(brutto)
    kup_raw = brutto_d * d(kup_proc) / d(100)
    dochod_raw = brutto_d - kup_raw
    podstawa_opodatkowania = r0_pit(dochod_raw)
    stawka_podatku = r2(stawka_podatku_proc)
    podatek_naliczony = r2(d(podstawa_opodatkowania) * d(stawka_podatku) / d(100))
    zaliczka_podatku = r0_pit(podatek_naliczony)

    kup_kwota = r2(kup_raw)
    dochod = r2(dochod_raw)

    # Umowa o dzieło: no ZUS/ZDR → netto = brutto − zaliczka_podatku only.
    selected_net = Decimal(str(r2(brutto_d - d(zaliczka_podatku))))
    next_zl = selected_net.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if next_zl > selected_net and (next_zl - selected_net) <= Decimal("0.01"):
        kwota_do_wyplaty = float(next_zl)
    else:
        kwota_do_wyplaty = float(selected_net)

    return {
        "emerytalne_zleceniobiorca": 0.0,
        "emerytalne_zleceniodawca": 0.0,
        "rentowe_zleceniobiorca": 0.0,
        "rentowe_zleceniodawca": 0.0,
        "chorobowe_zleceniobiorca": 0.0,
        "wypadkowe_zleceniodawca": 0.0,
        "zdrowotne_zleceniobiorca": 0.0,
        "fp_kwota": 0.0,
        "fgsp_kwota": 0.0,
        "kup_kwota": kup_kwota,
        "dochod": dochod,
        "stawka_podatku": stawka_podatku,
        "kwota_podatku": zaliczka_podatku,
        "kwota_do_wyplaty": kwota_do_wyplaty,
    }
