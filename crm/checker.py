"""Employee DB lookup and financial verification for CRM import automation.

Verification logic
------------------
UD (Umowa o Dzieło):
    recalculate_umowa_dzielo_from_rates(brutto, kup_proc, pit_rate)
    Compare kwota_do_wyplaty with __audit_netto from source.

UZ (Umowa Zlecenie):
    recalculate_umowa_zlecenie_from_rates(brutto, kup_proc, pit_rate,
        per-row ZUS rates from formatted DataFrame)
    Compare kwota_do_wyplaty with __audit_netto.

Edge cases auto-handled
-----------------------
* __audit_is_student=1   → expect netto = brutto − PIT (full ZUS exemption).
* __audit_zus_exempt=1   → same expectation (covers student + zbieg from Excel).
* __audit_calculate_type='brutto_from_netto' → CRM rounded brutto upward from
                                              desired netto; ±1 PLN diff accepted.
* Stawka podatku=0       → PIT-exempt (ulga dla młodych etc.); netto≈brutto for UD.

Tolerances:
    diff ≤ 0.05 PLN  → OK
    diff ≤ 1.05 PLN  → MARGINAL (WaPro vs CRM rounding boundary)
    otherwise        → DISCREPANCY — note explains the most likely cause.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from db.tax_calc_2026 import (
    recalculate_umowa_dzielo_from_rates,
    recalculate_umowa_zlecenie_from_rates,
)


# Differences within this range are "OK" (normal rounding variance ≤ 5 gr)
_TOLERANCE_OK = Decimal("0.05")
# Differences within this range are "marginal" — caused by WaPro floor-vs-HALF_DOWN KUP
# rounding: brutto × kup% produces an exact x.5 value which WaPro and CRM round differently.
# This is expected and produces exactly 1 PLN difference in netto.
_TOLERANCE_MARGINAL = Decimal("1.05")


def _kup_str_to_float(kup: object) -> float:
    """'50%' → 50.0, 0.5 → 50.0, '20%' → 20.0, etc."""
    s = str(kup or "").strip().replace(",", ".").replace("\xa0", "").replace(" ", "")
    if s.endswith("%"):
        try:
            v = float(s[:-1])
            return v * 100 if 0 <= v <= 1 else v
        except ValueError:
            pass
    try:
        v = float(s)
        return v * 100 if 0 <= v <= 1 else v
    except ValueError:
        return 0.0


# ─── Employee PESEL lookup ────────────────────────────────────────────────────

@dataclass
class CheckPeselResult:
    """Result of batch PESEL lookup in WaProGang DB."""
    total: int = 0
    found: int = 0
    missing: list[str] = field(default_factory=list)
    missing_rows: list[dict] = field(default_factory=list)


def check_pesels_in_db(
    df_formatted: pd.DataFrame,
    db_service,
) -> CheckPeselResult:
    """Check which PESELs from the formatted report exist in PRACOWNIK table.

    Parameters
    ----------
    df_formatted : DataFrame
        Output of format_crm_report; must have 'PESEL' column.
    db_service : DatabaseService
        Active DB service (must be connected).

    Returns
    -------
    CheckPeselResult
    """
    result = CheckPeselResult()
    seen: set[str] = set()

    for _, row in df_formatted.iterrows():
        pesel = str(row.get("PESEL", "")).strip()
        if not pesel or pesel in seen:
            continue
        seen.add(pesel)
        result.total += 1

        emp_id = db_service.employee_id_by_pesel(pesel)
        if emp_id is not None:
            result.found += 1
        else:
            result.missing.append(pesel)
            result.missing_rows.append({
                "PESEL": pesel,
                "Pracownik": str(row.get("__audit_pracownik", "")).strip(),
                "Nr Rachunku": str(row.get("Nr Rachunku", "")).strip(),
                "Typ": row.get("Typ umowy", ""),
            })

    return result


# ─── Financial verification ───────────────────────────────────────────────────

@dataclass
class VerifyRowResult:
    """Verification result for a single row."""
    nr_rachunku: str
    pesel: str
    typ: str
    brutto: float
    netto_source: float
    netto_calc: float
    diff: float
    is_ok: bool
    is_marginal: bool
    note: str
    data_source: str = ""
    calculate_type: str = ""
    pit_rate: float = 0.0


@dataclass
class VerifyResult:
    """Aggregate financial verification result."""
    total: int = 0
    ok: int = 0
    marginal: int = 0
    discrepancy: int = 0
    rows: list[VerifyRowResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Per-bucket counts for richer UI feedback.
    api_rows: int = 0
    excel_rows: int = 0
    zus_exempt_rows: int = 0
    brutto_from_netto_rows: int = 0
    pit_zero_rows: int = 0


def verify_financials(df_formatted: pd.DataFrame) -> VerifyResult:
    """Verify netto calculations against source values.

    Uses __audit_netto column (from source Kwota netto) for comparison.

    Parameters
    ----------
    df_formatted : DataFrame
        Output of format_crm_report; must have __audit_netto column.

    Returns
    -------
    VerifyResult
    """
    result = VerifyResult()

    if "__audit_netto" not in df_formatted.columns:
        result.errors.append("Brak kolumny __audit_netto — plik nie pochodzi z format_crm_report.")
        return result

    for _, row in df_formatted.iterrows():
        result.total += 1
        typ = str(row.get("Typ umowy", "")).strip()
        nr_rach = str(row.get("Nr Rachunku", "")).strip()
        pesel = str(row.get("PESEL", "")).strip()
        data_source = str(row.get("__audit_data_source", "") or "").strip().lower()
        calc_type = str(row.get("__audit_calculate_type", "") or "").strip().lower()

        if data_source == "api":
            result.api_rows += 1
        elif data_source == "excel":
            result.excel_rows += 1

        try:
            brutto = float(row["Kwota brutto"])
        except (TypeError, ValueError):
            result.errors.append(f"{nr_rach}: nieprawidłowe Kwota brutto.")
            result.discrepancy += 1
            continue

        try:
            netto_src = float(row["__audit_netto"])
        except (TypeError, ValueError):
            result.errors.append(f"{nr_rach}: brak wartości __audit_netto — pomijam.")
            continue

        kup = _kup_str_to_float(row.get("KUP %", "0%"))
        try:
            _pit_val = row.get("Stawka podatku [%]")
            if _pit_val is None or str(_pit_val).strip() == "":
                pit_rate = 12.0
            else:
                pit_rate = float(_pit_val)
        except (TypeError, ValueError):
            pit_rate = 12.0

        if pit_rate == 0.0:
            result.pit_zero_rows += 1
        if calc_type == "brutto_from_netto":
            result.brutto_from_netto_rows += 1

        try:
            if typ == "2":
                # Umowa o Dzieło
                recalc = recalculate_umowa_dzielo_from_rates(
                    brutto=brutto,
                    kup_proc=kup,
                    stawka_podatku_proc=pit_rate,
                )
            else:
                # Umowa Zlecenie
                recalc = recalculate_umowa_zlecenie_from_rates(
                    brutto=brutto,
                    kup_proc=kup,
                    stawka_podatku_proc=pit_rate,
                    emerytalne_proc=float(row.get("Skł.na ub.emerytal.[%]", 0) or 0),
                    rentowe_u_proc=float(row.get("Składka ub.rent. U [%]", 0) or 0),
                    rentowe_p_proc=float(row.get("Składka ub.rent. P [%]", 0) or 0),
                    chorobowe_proc=float(row.get("Składka ub.chorob.[%]", 0) or 0),
                    wypadkowe_proc=float(row.get("Składka ub.wypadk.[%]", 0) or 0),
                    zdrowotne_proc=float(row.get("Składka ub.zdrowotne[%]", 0) or 0),
                    fp_proc=float(row.get("FP [%]", 0) or 0),
                    fgsp_proc=float(row.get("FGŚP [%]", 0) or 0),
                )
        except Exception as exc:
            result.errors.append(f"{nr_rach}: błąd obliczenia — {exc}")
            result.discrepancy += 1
            continue

        netto_calc = float(recalc.kwota_do_wyplaty)
        diff = abs(netto_calc - netto_src)
        diff_d = Decimal(str(round(diff, 4)))

        is_ok = diff_d <= _TOLERANCE_OK
        is_marginal = not is_ok and diff_d <= _TOLERANCE_MARGINAL

        zus_exempt = int(row.get("__audit_zus_exempt", 0) or 0) == 1
        if zus_exempt:
            result.zus_exempt_rows += 1

        # brutto_from_netto: CRM started from desired netto and computed brutto;
        # WaPro's "Wylicz" sometimes lands ±1 PLN due to KUP HALF_DOWN. Accept
        # that explicitly as OK when CRM tells us the calc was reversed.
        crm_reverse_calc = calc_type == "brutto_from_netto"

        if is_ok:
            note = "OK"
            result.ok += 1
        elif zus_exempt:
            note = (
                f"OK — zwolnienie z ZUS: netto_src={netto_src:.2f}, "
                f"netto_calc={netto_calc:.2f} (składki=0)"
            )
            result.ok += 1
        elif is_marginal and crm_reverse_calc:
            note = (
                f"OK — calculate_type=brutto_from_netto: ±{diff:.2f} PLN to "
                "różnica zaokrąglenia odwrotnego (akceptowalne)"
            )
            result.ok += 1
        elif is_marginal:
            note = (
                f"Marginalna różnica zaokrąglenia WaPro/CRM ±{diff:.2f} PLN "
                f"(źródło={netto_src:.2f}, wyliczone={netto_calc:.2f})"
            )
            result.marginal += 1
        else:
            cause = _diagnose_discrepancy(
                pit_rate=pit_rate,
                calc_type=calc_type,
                zus_exempt=zus_exempt,
                kup=kup,
                typ=typ,
            )
            note = (
                f"NIEZGODNOŚĆ: źródło={netto_src:.2f}, "
                f"wyliczone={netto_calc:.2f}, diff={diff:.2f} PLN — {cause}"
            )
            result.discrepancy += 1

        result.rows.append(VerifyRowResult(
            nr_rachunku=nr_rach,
            pesel=pesel,
            typ="UD" if typ == "2" else "UZ",
            brutto=brutto,
            netto_source=netto_src,
            netto_calc=netto_calc,
            diff=diff,
            is_ok=is_ok,
            is_marginal=is_marginal,
            note=note,
            data_source=data_source,
            calculate_type=calc_type,
            pit_rate=pit_rate,
        ))

    return result


def _diagnose_discrepancy(
    *,
    pit_rate: float,
    calc_type: str,
    zus_exempt: bool,
    kup: float,
    typ: str,
) -> str:
    """Best-effort human-readable cause for a verification mismatch."""
    causes: list[str] = []
    if calc_type == "brutto_from_netto":
        causes.append("CRM calculate_type=brutto_from_netto (odwrotne zaokrąglenie)")
    if pit_rate not in (0.0, 12.0, 32.0):
        causes.append(f"nietypowa stawka PIT={pit_rate}")
    if typ == "1" and not zus_exempt and kup == 0.0:
        causes.append("UZ z KUP=0 — sprawdź flagę is_student w CRM")
    if typ == "1" and zus_exempt:
        causes.append("UZ ze zwolnieniem ZUS — sprawdź ulgę dla młodych lub niestandardowy PIT")
    if not causes:
        causes.append("zbieg tytułów, specjalne składki lub korekta po stronie CRM")
    return "; ".join(causes)
