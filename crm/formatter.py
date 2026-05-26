"""Transform CRM UDUZ04-type report into batch_merged (payroll system import) format.

Source columns (UDUZ04.xlsx):
    [0] Lp.           [1] Number umowy  [2] Number (rachunku)  [3] Typ
    [4] Pracownik     [5] PESEL         [6] Kwota netto         [7] Kwota Brutto
    [8] KUP %         [9] Data zawarcia [10] Podatek            [11] Data akceptacji
    [12] PPK counter

Extra API-only columns (when raw row originates from crm.api_client):
    [13] vat (PIT rate)            [14] is_student            [15] zus_chorobowe
    [16] calculate_type            [17] zus_emerytalne        [18] zus_zdrowotne
    [19] bill_id

Target columns (batch_merged / payroll system mixed import):
    Number umowy | Nr Rachunku | Typ umowy | PESEL | Kwota brutto | KUP %
    Data zawarcia umowy | Data wypłaty | PPK pracownika PLN | Forma Opodtkowania
    Stawka podatku [%]
    Skł.na ub.emerytal.[%] | Składka ub.rent. U [%] | Składka ub.rent. P [%]
    Składka ub.chorob.[%]  | Składka ub.wypadk.[%]  | Składka ub.zdrowotne[%]
    FP [%] | FGŚP [%]

Extra audit columns (prefixed __audit_, stripped before Excel save / import):
    __audit_netto                 — source Kwota netto
    __audit_podatek               — source Podatek
    __audit_podatek_effective_pct — Podatek/brutto×100 (UD only; 0 for UZ)
    __audit_pracownik             — source Pracownik (for PPK matching)
    __audit_zus_exempt            — 1 if ZUS-exempt (student/zbieg), else 0
    __audit_data_source           — 'api' for CRM API rows, 'excel' for files
    __audit_is_student            — 1/0/None, only meaningful for 'api'
    __audit_zus_chorobowe_flag    — 1/0, whether chorobowe 2.45% applies
    __audit_calculate_type        — 'netto_from_brutto' | 'brutto_from_netto' | ''
    __audit_bill_id               — CRM bill id (None for Excel)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_DOWN, ROUND_HALF_UP
from typing import Optional

import pandas as pd


# ─── ZUS rates applied to Umowa Zlecenie ────────────────────────────────────
_UZ_RATES = {
    "Skł.na ub.emerytal.[%]": 19.52,
    "Składka ub.rent. U [%]": 1.50,
    "Składka ub.rent. P [%]": 6.50,
    "Składka ub.chorob.[%]": 0,
    "Składka ub.wypadk.[%]": 0,
    "Składka ub.zdrowotne[%]": 9.00,
    "FP [%]": 2.45,
    "FGŚP [%]": 0,
}

_UD_RATES = {k: 0 for k in _UZ_RATES}

_ZUS_COLS = list(_UZ_RATES.keys())

_TYP_UD = "Umowa o Dzieło"
_TYP_UZ = "Umowa Zlecenie"

# These PESELs always receive Składka chorobowa 2.45% — used as fallback for Excel
# files where CRM `zus_chorobowe` flag is not available.
_SPECIAL_CHOROBOWE_PESELS: frozenset[str] = frozenset({"83122516550", "91100715534"})
_SPECIAL_CHOROBOWE_PCT = 2.45

# If |netto - brutto| < this threshold, treat the UZ row as ZUS-exempt (student/zbieg).
# Used as fallback when CRM `is_student` flag is not available (Excel files).
_ZUS_EXEMPT_DELTA = 1.0


def _bool_from_raw(value: object) -> Optional[bool]:
    """Convert various truthy/falsy markers stored in raw rows to bool/None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if pd.isna(value):
            return None
        return bool(int(value))
    text = str(value).strip().lower()
    if text in ("", "nan", "none", "null"):
        return None
    if text in ("1", "true", "t", "yes", "y"):
        return True
    if text in ("0", "false", "f", "no", "n"):
        return False
    return None


def _resolve_uz_rates(
    *,
    pesel: str,
    pracownik: str,
    brutto: float,
    netto_src: Optional[float],
    api_is_student: Optional[bool],
    api_zus_chor: Optional[bool],
    data_source: str,
    warnings: list[str],
) -> tuple[int, dict[str, float]]:
    """Return (zus_exempt_marker, rates) for an Umowa Zlecenie row.

    Priority of decisions:
      1. CRM `is_student` flag → if True, full ZUS exemption (rates = 0).
      2. CRM `zus_chorobowe` flag → if True, chorobowe = 2.45% (else 0%).
      3. Excel fallback: if netto≈brutto, treat as ZUS-exempt.
      4. Excel fallback: legacy hardcoded PESEL list for chorobowe.
    """
    # Rule 1 — student exemption (most authoritative, comes from CRM).
    if api_is_student is True:
        warnings.append(
            f"PESEL {pesel} ({pracownik}): CRM is_student=True → "
            "pełne zwolnienie z ZUS (składki 0)."
        )
        return 1, {k: 0.0 for k in _UZ_RATES}

    # Excel fallback for student/zbieg when API flag is missing.
    if data_source == "excel" and netto_src is not None and abs(netto_src - brutto) < _ZUS_EXEMPT_DELTA:
        warnings.append(
            f"PESEL {pesel} ({pracownik}): netto≈brutto (Excel) → "
            "wykryto zwolnienie z ZUS (zbieg/student). Składki ustawione na 0."
        )
        return 1, {k: 0.0 for k in _UZ_RATES}

    # Standard UZ rates with chorobowe override.
    rates = _UZ_RATES.copy()
    if api_zus_chor is True:
        rates["Składka ub.chorob.[%]"] = _SPECIAL_CHOROBOWE_PCT
    elif api_zus_chor is None and pesel in _SPECIAL_CHOROBOWE_PESELS:
        # Excel fallback: keep legacy hardcoded PESELs only when API flag absent.
        rates["Składka ub.chorob.[%]"] = _SPECIAL_CHOROBOWE_PCT
    return 0, rates

_TARGET_COLS = [
    "Number umowy",
    "Nr Rachunku",
    "Typ umowy",
    "PESEL",
    "Kwota brutto",
    "KUP %",
    "Data zawarcia umowy",
    "Data wypłaty",
    "PPK pracownika PLN",
    "Forma Opodtkowania",
    "Stawka podatku [%]",
] + _ZUS_COLS


def _parse_kup(value: object) -> str:
    """Normalize KUP value to percentage string like '50%' or '20%'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "0%"
    s = str(value).strip().replace(",", ".").replace("\xa0", "").replace(" ", "")
    # Already clean string '50%'
    if s.endswith("%"):
        try:
            num = float(s[:-1])
            if 0 <= num <= 1:
                num = num * 100
            return f"{int(num)}%"
        except ValueError:
            pass
    # Float fraction like 0.5 → 50%
    try:
        num = float(s)
        if 0 <= num <= 1:
            num = num * 100
        return f"{int(num)}%"
    except ValueError:
        return "0%"


def _to_pesel_str(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    raw = str(value).strip()
    if raw.endswith(".0"):
        raw = raw[:-2]
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits if len(digits) == 11 else raw


def _normalize_typ(value: object) -> Optional[str]:
    """Return '1' for UZ, '2' for UD, None for PPK / header / unknown."""
    s = str(value or "").strip()
    if "Dzieło" in s or "Dzielo" in s:
        return "2"
    if "Zlecenie" in s or "zlecenie" in s:
        return "1"
    return None


def _to_float(value: object) -> Optional[float]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    if not cleaned or cleaned.lower() == "nan":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _round_tax_base(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_DOWN)


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _expected_podatek(
    brutto: float,
    kup_percent: float,
    rates: dict[str, float],
    pit_rate: float,
) -> float:
    br = Decimal(str(brutto))
    employee_social_raw = br * (
        Decimal(str(rates.get("Skł.na ub.emerytal.[%]", 0))) / Decimal("2")
        + Decimal(str(rates.get("Składka ub.rent. U [%]", 0)))
        + Decimal(str(rates.get("Składka ub.chorob.[%]", 0)))
    ) / Decimal("100")
    tax_base_before_kup = br - employee_social_raw
    kup_raw = tax_base_before_kup * Decimal(str(kup_percent)) / Decimal("100")
    income_raw = br - employee_social_raw - kup_raw
    if income_raw <= 0 or pit_rate <= 0:
        return 0.0
    tax_base = _round_tax_base(income_raw)
    tax_raw = _round_money(tax_base * Decimal(str(pit_rate)) / Decimal("100"))
    return float(_round_tax_base(tax_raw))


def infer_pit_rate_from_podatek(
    podatek: object,
    brutto: object,
    kup: object = "0%",
    rates: Optional[dict[str, float]] = None,
    *,
    default: float = 12.0,
) -> float:
    """Infer PIT rate from source Podatek amount.

    The source gives a PLN amount, not the rate. We compare it against payroll-system-like
    PIT advances for 12% and 32% using brutto, KUP, and employee ZUS rates.
    """
    podatek_value = _to_float(podatek)
    brutto_value = _to_float(brutto)
    if podatek_value is None or brutto_value is None or brutto_value <= 0:
        return default
    if abs(podatek_value) < 0.005:
        return 0.0

    kup_percent = _to_float(str(_parse_kup(kup)).rstrip("%")) or 0.0
    effective_rates = rates or {k: 0.0 for k in _ZUS_COLS}
    expected_12 = _expected_podatek(brutto_value, kup_percent, effective_rates, 12.0)
    expected_32 = _expected_podatek(brutto_value, kup_percent, effective_rates, 32.0)
    return 32.0 if abs(podatek_value - expected_32) < abs(podatek_value - expected_12) else 12.0


def podatek_effective_percent(podatek: object, brutto: object) -> float:
    podatek_value = _to_float(podatek)
    brutto_value = _to_float(brutto)
    if podatek_value is None or brutto_value is None or brutto_value <= 0:
        return 0.0
    return round((podatek_value / brutto_value) * 100, 4)


@dataclass
class FormatterResult:
    """Statistics from a formatting run."""
    total_rows: int = 0
    ud_count: int = 0
    uz_count: int = 0
    ppk_matched: int = 0
    skipped_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    date_wyplaty_min: Optional[str] = None
    date_wyplaty_max: Optional[str] = None
    date_zawarcia_min: Optional[str] = None
    date_zawarcia_max: Optional[str] = None


def format_crm_report(
    source: str | pd.DataFrame,
) -> tuple[pd.DataFrame, FormatterResult]:
    """Load (or accept) a UDUZ04-type CRM file and return formatted DataFrame.

    Parameters
    ----------
    source : str or DataFrame
        Path to the source .xlsx file or an already-loaded raw DataFrame
        (header=None, as returned by pd.read_excel(..., header=None)).

    Returns
    -------
    (df_formatted, result)
        df_formatted  — columns: TARGET_COLS + __audit_* columns
        result        — FormatterResult with counts and warnings
    """
    if isinstance(source, str):
        raw = pd.read_excel(source, header=None, dtype={5: str})
    else:
        raw = source.copy()

    result = FormatterResult()

    # ── Collect PPK rows (Typ == 'PPK') ─────────────────────────────────────
    # PPK row: col[3]='PPK', col[4]=pracownik name, col[10]=PPK amount (PLN)
    ppk_by_name: dict[str, float] = {}
    for _, prow in raw.iterrows():
        if str(prow[3]).strip() == "PPK":
            name = str(prow[4] or "").strip()
            try:
                amt = float(prow[10])
            except (TypeError, ValueError):
                amt = 0.0
            if name:
                ppk_by_name[name.lower()] = amt

    # ── Filter data rows ─────────────────────────────────────────────────────
    out_rows: list[dict] = []

    for _, row in raw.iterrows():
        typ_raw = str(row[3] or "").strip()

        # Skip header / sub-header / PPK / empty
        if typ_raw in ("Lp.", "Typ", "PPK", "") or pd.isna(row[3]):
            result.skipped_rows += 1
            continue

        typ_code = _normalize_typ(typ_raw)
        if typ_code is None:
            result.skipped_rows += 1
            continue

        pesel = _to_pesel_str(row[5])
        if not pesel:
            result.warnings.append(
                f"Wiersz bez PESEL (Nr Rachunku={row[2]}): pominięty."
            )
            result.skipped_rows += 1
            continue

        brutto_raw = row[7]
        brutto = _to_float(brutto_raw)
        if brutto is None:
            result.warnings.append(
                f"PESEL {pesel}: nieprawidłowe Kwota Brutto '{brutto_raw}', pominięty."
            )
            result.skipped_rows += 1
            continue

        kup_str = _parse_kup(row[8])

        # Date zawarcia: keep as-is string; if datetime convert to DD/MM/YYYY
        data_zaw = row[9]
        if hasattr(data_zaw, "strftime"):
            data_zaw = data_zaw.strftime("%d/%m/%Y")
        else:
            data_zaw = str(data_zaw or "").strip()

        # Data wypłaty = Data akceptacji (col 11)
        data_wpl = row[11]

        # PPK lookup
        pracownik = str(row[4] or "").strip()
        ppk_pln = 0.0
        if typ_code == "1":  # UZ only
            ppk_val = ppk_by_name.get(pracownik.lower(), 0.0)
            if ppk_val > 0:
                ppk_pln = ppk_val
                result.ppk_matched += 1

        # Source netto for student/zbieg detection (Excel fallback) and audit
        netto_src = _to_float(row[6])

        # Detect data origin and extract API-only flags (None when from Excel)
        is_api_row = len(row) > 13
        data_source = "api" if is_api_row else "excel"
        api_is_student = _bool_from_raw(row[14]) if len(row) > 14 else None
        api_zus_chor = _bool_from_raw(row[15]) if len(row) > 15 else None
        api_calculate_type = (
            str(row[16] or "").strip().lower() if len(row) > 16 else ""
        )
        api_bill_id = row[19] if len(row) > 19 else None

        # ZUS rates — determine using API flags first, fall back to heuristics for Excel
        if typ_code == "2":
            # Umowa o Dzieło — never has ZUS by Polish law
            rates = _UD_RATES.copy()
            zus_exempt_marker = 0
        else:
            zus_exempt_marker, rates = _resolve_uz_rates(
                pesel=pesel,
                pracownik=pracownik,
                brutto=brutto,
                netto_src=netto_src,
                api_is_student=api_is_student,
                api_zus_chor=api_zus_chor,
                data_source=data_source,
                warnings=result.warnings,
            )

        if is_api_row:
            # For API rows: pos13 = VAT field = actual PIT rate (0, 12, or 32).
            # pos10 = brutto-netto which for UZ includes ZUS too — inference would
            # be wrong here. Always take the API rate, defaulting to 12 if unexpected.
            api_rate = _to_float(row[13])
            if api_rate in (0.0, 12.0, 32.0):
                pit_rate = api_rate
            else:
                pit_rate = 12.0
                result.warnings.append(
                    f"PESEL {pesel} ({pracownik}): CRM vat='{row[13]}' nieoczekiwane — "
                    "ustawiono domyślne PIT 12%."
                )
        else:
            # For Excel UDUZ04 rows: pos10 = actual Podatek (PIT advance in PLN).
            pit_rate = infer_pit_rate_from_podatek(row[10], brutto, kup_str, rates)
        if pit_rate == 32.0:
            result.warnings.append(
                f"PESEL {pesel} ({pracownik}): wykryto stawkę PIT 32% "
                f"(brutto={brutto}, źródło={data_source})."
            )

        rec: dict = {
            "Number umowy": str(row[1] or "").strip(),
            "Nr Rachunku": str(row[2] or "").strip(),
            "Typ umowy": typ_code,
            "PESEL": pesel,
            "Kwota brutto": brutto,
            "KUP %": kup_str,
            "Data zawarcia umowy": data_zaw,
            "Data wypłaty": data_wpl,
            "PPK pracownika PLN": ppk_pln,
            "Forma Opodtkowania": 1,
            "Stawka podatku [%]": pit_rate,
            # ZUS rates
            **{col: rates[col] for col in _ZUS_COLS},
            # Audit columns (not exported to batch_merged Excel)
            "__audit_netto": row[6],
            "__audit_podatek": row[10],
            # For UD (no ZUS): pos10 = actual PIT advance. For UZ: pos10 = total deductions.
            # The percentage here reflects different semantics — use only for UD diagnosis.
            "__audit_podatek_effective_pct": (
                podatek_effective_percent(row[10], brutto) if typ_code == "2"
                else podatek_effective_percent(0, brutto)  # not meaningful for UZ — show 0
            ),
            "__audit_pracownik": pracownik,
            "__audit_zus_exempt": zus_exempt_marker,
            "__audit_data_source": data_source,
            "__audit_is_student": (
                1 if api_is_student is True else
                0 if api_is_student is False else None
            ),
            "__audit_zus_chorobowe_flag": (
                1 if api_zus_chor is True else
                0 if api_zus_chor is False else None
            ),
            "__audit_calculate_type": api_calculate_type,
            "__audit_bill_id": api_bill_id,
        }

        out_rows.append(rec)

        if typ_code == "2":
            result.ud_count += 1
        else:
            result.uz_count += 1

    result.total_rows = result.ud_count + result.uz_count

    if not out_rows:
        df_formatted = pd.DataFrame(columns=_TARGET_COLS + ["__audit_netto", "__audit_podatek", "__audit_pracownik"])
        result.warnings.append("Brak wierszy danych w pliku źródłowym.")
        return df_formatted, result

    df_formatted = pd.DataFrame(out_rows)

    # ── Derive date range ────────────────────────────────────────────────────
    _EPOCH_MIN = pd.Timestamp("1990-01-01")  # guard against epoch-0 / NaT artifacts
    try:
        dates_wpl = pd.to_datetime(df_formatted["Data wypłaty"], errors="coerce").dropna()
        dates_wpl = dates_wpl[dates_wpl >= _EPOCH_MIN]
        if not dates_wpl.empty:
            result.date_wyplaty_min = dates_wpl.min().strftime("%d/%m/%Y")
            result.date_wyplaty_max = dates_wpl.max().strftime("%d/%m/%Y")
    except Exception:
        pass

    try:
        dates_zaw = pd.to_datetime(
            df_formatted["Data zawarcia umowy"], format="%d/%m/%Y", errors="coerce"
        ).dropna()
        if not dates_zaw.empty:
            result.date_zawarcia_min = dates_zaw.min().strftime("%d/%m/%Y")
            result.date_zawarcia_max = dates_zaw.max().strftime("%d/%m/%Y")
    except Exception:
        pass

    return df_formatted, result


def df_to_export(df_formatted: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of the formatted DataFrame without __audit_* columns."""
    audit_cols = [c for c in df_formatted.columns if c.startswith("__audit_")]
    return df_formatted.drop(columns=audit_cols, errors="ignore")


# Canonical mapping from payroll system profile fields → formatted DataFrame columns.
# Used by crm/checker.py and ui/automatyzacja_tab.py for auto-import.
AUTO_MAPPING: dict[str, str] = {
    "PESEL": "PESEL",
    "NR Ewidencyjny": "PESEL",
    "номер умовы": "Number umowy",
    "номер рахунка": "Nr Rachunku",
    "Тип умовы": "Typ umowy",
    "Дата выплаты": "Data wypłaty",
    "Дата умовы": "Data zawarcia umowy",
    "Форма податка": "Forma Opodtkowania",
    "Ставка податка": "Stawka podatku [%]",
    "Kwota brutto": "Kwota brutto",
    "KOSZTY UZYSKANIA PRZYCHODU %": "KUP %",
    "Skł.na ub.emerytal.[%]": "Skł.na ub.emerytal.[%]",
    "Składka ub.rent. U [%]": "Składka ub.rent. U [%]",
    "Składka ub.rent. P [%]": "Składka ub.rent. P [%]",
    "Składka ub.chorob.[%]": "Składka ub.chorob.[%]",
    "Składka ub.wypadk.[%]": "Składka ub.wypadk.[%]",
    "Składka ub.zdrowotne[%]": "Składka ub.zdrowotne[%]",
    "FP [%]": "FP [%]",
    "FGŚP [%]": "FGŚP [%]",
}
