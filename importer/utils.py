from __future__ import annotations

import datetime as _dt
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import pandas as pd

_CLARION_BASE = pd.Timestamp(year=1800, month=12, day=28)
_EXCEL_BASE = pd.Timestamp(year=1899, month=12, day=30)
# Range in which we treat a parsed/inferred year as a legitimate contract date.
# Anything outside is rejected so silent +100 / -100 year shifts can't slip in.
_REASONABLE_MIN_YEAR = 1900
_REASONABLE_MAX_YEAR = 2100


def _is_reasonable_year(ts: pd.Timestamp) -> bool:
    return _REASONABLE_MIN_YEAR <= int(ts.year) <= _REASONABLE_MAX_YEAR


def _safe_clarion_days(ts: pd.Timestamp) -> int:
    return int((ts.normalize() - _CLARION_BASE).days)


URZEDY_REFERENCE_PATH = Path(__file__).resolve().parent.parent / "urzedy_reference.json"

ADDRESS_FIELD_LIMITS: dict[str, int] = {
    "country": 50,
    "voivodeship": 30,
    "powiat": 40,
    "gmina": 40,
    "street": 40,
    "house_no": 10,
    "flat_no": 10,
    "city": 40,
    "postal_code": 20,
    "post_office": 40,
}


_ADDRESS_KEY_TO_FIELD = {
    "country": "Kraj",
    "voivodeship": "Wojewodztwo",
    "powiat": "Powiat",
    "gmina": "Gmina",
    "street": "Ulica",
    "house_no": "Numer Domu",
    "flat_no": "Numer lokalu",
    "city": "Miejscowosc",
    "postal_code": "Kod pocztowy",
    "post_office": "Poczta",
}


def _address_key_to_field_name(key: str) -> str:
    return _ADDRESS_KEY_TO_FIELD.get(key, key)


def _normalize_urzad_name(name: str) -> str:
    if not name:
        return ""
    upper = name.upper().strip().replace("’", "'")
    no_accents = "".join(
        ch for ch in unicodedata.normalize("NFKD", upper) if not unicodedata.combining(ch)
    )
    compact = " ".join(no_accents.replace("-", " - ").split())
    return compact


def _urzad_name_variants(name: str) -> set[str]:
    base = _normalize_urzad_name(name)
    if not base:
        return set()
    variants = {base}
    variants.add(base.replace(" URZAD SKARBOWY WE ", " URZAD SKARBOWY W "))
    variants.add(base.replace(" URZAD SKARBOWY W ", " URZAD SKARBOWY WE "))
    variants.add(base.replace("URZAD SKARBOWY WE ", "URZAD SKARBOWY "))
    variants.add(base.replace("URZAD SKARBOWY W ", "URZAD SKARBOWY "))
    if base.startswith("US "):
        variants.add("URZAD SKARBOWY " + base[3:])
    if base.startswith("URZAD SKARBOWY "):
        variants.add("US " + base[len("URZAD SKARBOWY ") :])
    return {item for item in variants if item}


def _urzad_match_key(name: str) -> str:
    normalized = _normalize_urzad_name(name)
    # Drop generic office prefixes so "US Warszawa-Praga" and
    # "Urząd Skarbowy w Warszawie-Pradze" collapse to the same key.
    normalized = normalized.replace(" URZAD SKARBOWY ", " ")
    normalized = normalized.replace("URZAD SKARBOWY ", "")
    normalized = re.sub(r"^US\s+", "", normalized)
    normalized = re.sub(r"\bWE\b", " ", normalized)
    normalized = re.sub(r"\bW\b", " ", normalized)
    normalized = " ".join(normalized.split())
    return re.sub(r"[^A-Z0-9]", "", normalized)


def load_urzedy_reference() -> dict[str, str]:
    entries = load_urzedy_reference_entries()
    return {key: value[1] for key, value in entries.items()}


def load_urzedy_reference_entries() -> dict[str, tuple[str, str]]:
    if not URZEDY_REFERENCE_PATH.exists():
        return {}
    try:
        raw = json.loads(URZEDY_REFERENCE_PATH.read_text(encoding="utf-8"))
        result: dict[str, tuple[str, str]] = {}
        for key, value in raw.items():
            canonical_name = str(key).strip()
            code = str(value).strip()
            for normalized in _urzad_name_variants(str(key)):
                result.setdefault(normalized, (canonical_name, code))
        return result
    except Exception:
        return {}


def resolve_urzad_code(urzad_name: str, reference: dict[str, str]) -> str:
    for variant in _urzad_name_variants(urzad_name):
        code = reference.get(variant, "")
        if code:
            return code
    return ""


def resolve_urzad_reference_entry(
    urzad_name: str,
    reference_entries: dict[str, tuple[str, str]],
) -> tuple[str, str]:
    for variant in _urzad_name_variants(urzad_name):
        entry = reference_entries.get(variant)
        if entry:
            return entry
    lookup_key = _urzad_match_key(urzad_name)
    if lookup_key:
        for normalized, entry in reference_entries.items():
            if _urzad_match_key(normalized) == lookup_key:
                return entry
    return "", ""


def _to_clarion_date(value: Any) -> int | None:
    """Convert assorted Excel/CSV inputs to a Clarion day count (days since 1800-12-28).

    The DB columns DATA_UMOWY / DATA_WYPLATY are integer day counts since the
    Clarion epoch 1800-12-28 (NOT SQL Server's 1900-01-01). A bare integer that
    looks like an Excel serial (~46000 for 2026) used to be returned as-is, which
    silently shifted the date roughly -99 years (e.g. 2026 -> 1927). Any parsed
    year outside of [_REASONABLE_MIN_YEAR; _REASONABLE_MAX_YEAR] is rejected.
    """

    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(value, (pd.Timestamp, _dt.datetime, _dt.date)):
        ts = pd.Timestamp(value)
        if not _is_reasonable_year(ts):
            return None
        return _safe_clarion_days(ts)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            n = float(value)
        except (TypeError, ValueError):
            return None
        return _interpret_numeric_date(n)

    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None

    # Compact YYYYMMDD form, e.g. "20260215".
    if text.isdigit() and len(text) == 8:
        try:
            ts = pd.Timestamp(year=int(text[0:4]), month=int(text[4:6]), day=int(text[6:8]))
        except (ValueError, OverflowError):
            ts = None
        if ts is not None and _is_reasonable_year(ts):
            return _safe_clarion_days(ts)

    # Pure integer string. Try Excel serial first (typical), then Clarion-as-is.
    if text.isdigit():
        try:
            return _interpret_numeric_date(float(text))
        except ValueError:
            return None

    # PL day-first numeric separators, e.g. 01.04.2026, 01-04-2026, 01/04/2026.
    has_numeric_separators = bool(re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", text))
    dt = pd.to_datetime(text, errors="coerce", dayfirst=has_numeric_separators)
    if pd.isna(dt):
        # Last resort: numeric string with decimals or comma, e.g. "46068.0".
        try:
            return _interpret_numeric_date(float(text.replace(",", ".")))
        except ValueError:
            return None

    ts = pd.Timestamp(dt)
    if not _is_reasonable_year(ts):
        return None
    return _safe_clarion_days(ts)


def _interpret_numeric_date(n: float) -> int | None:
    """Map a bare number to a Clarion day count using a year sanity check.

    Excel serial dates and Clarion day counts share the same shape (a positive
    integer-ish number) but use different epochs. We try Excel epoch first as
    that's what raw numeric Excel cells produce; if that yields an absurd year
    we fall back to treating the value as already-Clarion (e.g. data round-tripped
    from the DB). Anything outside the reasonable range returns None.
    """

    if n <= 0:
        return None
    try:
        ts_excel = _EXCEL_BASE + pd.Timedelta(days=n)
        if _is_reasonable_year(ts_excel):
            return _safe_clarion_days(ts_excel)
    except (OverflowError, ValueError):
        pass
    try:
        ts_clarion = _CLARION_BASE + pd.Timedelta(days=n)
        if _is_reasonable_year(ts_clarion):
            return int(n)
    except (OverflowError, ValueError):
        pass
    return None


def _resolve_data_od(source_value: Any, fallback_data_od: int, strict_mode: bool) -> int | None:
    parsed = _to_clarion_date(source_value)
    if parsed is not None:
        return parsed
    if strict_mode:
        return None
    if fallback_data_od > 0:
        return fallback_data_od
    return None


def _clarion_year(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        day_number = int(value)
    except (TypeError, ValueError):
        return None
    if day_number <= 0:
        return None
    try:
        return int((_CLARION_BASE + pd.Timedelta(days=day_number)).year)
    except (OverflowError, ValueError):
        return None


def _normalize_pesel_value(value: object) -> str:
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    if text.endswith(".0"):
        text = text[:-2]
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits and len(digits) < 11:
        digits = digits.zfill(11)
    return digits or text


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = (
        str(value)
        .strip()
        .replace("\xa0", "")
        .replace(" ", "")
        .replace("%", "")
        .replace(",", ".")
    )
    if not text or text.lower() == "nan":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip().replace(" ", "")
    if not text or text.lower() == "nan":
        return None
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    return None


def _normalize_typ_umowy(value: Any) -> str:
    """Canonicalise Umowa type to '1'/'2' string.

    Excel often returns 1.0 / "1.0" / floats for whole numbers. We keep a
    string to match how DB column RODZAJ_UMOWY stores it (nvarchar), but we
    strip .0 and whitespace, so '1' and '1.0' behave identically.
    """
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    if re.fullmatch(r"\d+(\.0+)?", text):
        return str(int(float(text)))
    return text


def _normalize_typ_ubezpieczenia(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    if re.fullmatch(r"\d+(\.0+)?", text):
        number = str(int(float(text)))
        return number.zfill(4)
    return text


def _to_bool_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip().lower()
    if not text or text == "nan":
        return None
    if text in {"1", "tak", "t", "true", "yes", "y"}:
        return 1
    if text in {"0", "nie", "n", "false", "no"}:
        return 0
    as_int = _to_int(text)
    if as_int in (0, 1):
        return as_int
    return None
