"""Pure data-processing utilities for Excel/umowy column handling.

Extracted from main.py so that these pure functions can be used and tested
independently of the PyQt6 UI layer.

No Qt imports — safe to import in tests, CLI tools, and service modules.

Contents:
  - Column-name normalisation and alias lookup
  - Typ umowy label → '1' / '2' mapping
  - Float / numeric parsing helpers
  - PESEL display helpers (pandas-aware wrappers over utils.pesel)
  - Format constants shared across the import pipeline
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

import pandas as pd

from importer.utils import _normalize_typ_umowy
from utils.pesel import (
    normalize_pesel as _normalize_pesel_util,
    birthdate_from_pesel as _birthdate_from_pesel,
    age_on as _age_on,
)


# ─── Format constants ─────────────────────────────────────────────────────────

FORMAT_UMOWY_DROP_COLS = {"lp", "pracownik", "kwotanetto"}

# Internal: mirror of dropped «Kwota netto» for SKŁADKI exemption logic after PPK merge.
_SKLADKI_NETTO_MIRROR_COL = "__sklad_netto_src"

FORMAT_UMOWY_RENAME_COLS: dict[str, str] = {
    "number": "Nr Rachunku",
    "typ": "Typ umowy",
    "typumowy": "Typ umowy",
    "rodzajumowy": "Typ umowy",
    "data akceptacji": "Data wypłaty",
}

_FORMAT_TYP_UMOWY_COL = "Typ umowy"

FORMAT_UMOWY_SKLADKI_COLUMNS: list[tuple[str, float]] = [
    ("Skł.na ub.emerytal.[%]", 19.52),
    ("Składka ub.rent. U [%]", 1.50),
    ("Składka ub.rent. P [%]", 6.50),
    ("Składka ub.chorob.[%]", 0.00),
    ("Składka ub.wypadk.[%]", 0.00),
    ("Składka ub.zdrowotne[%]", 9.00),
    ("FP [%]", 2.45),
    ("FGŚP [%]", 0.0),
]

FORMAT_MODE_UMOWY = "umowy"
FORMAT_MODE_UMOWY_DZIELO = "umowy_dzielo"
FORMAT_MODE_UMOWY_MIXED = "umowy_mixed"
FORMAT_MODE_UMOWY_BATCH = "umowy_batch"
FORMAT_MODE_UBEZPIECZENIA = "ubezpieczenia"

UBEZPIECZENIA_OUTPUT_COLUMNS: list[str] = [
    "PESEL",
    "Номер умовы",
    "Typ ubezpieczenia",
    "Data powstania obowiazku ubezpieczenia",
    "Osoba podlega ubezpieczeniu Emerytalnemu",
    "Osoba podlega ubezpieczeniu Rentowemu",
    "Osoba podlega ubezpieczeniu Wypadkowemu",
    "Osoba podlega ubezpieczeniu Chorobowemu",
]

UBEZPIECZENIA_TYP_CONST = "0411"
UBEZPIECZENIA_SPECIAL_PESELS: set[str] = {"83122516550", "91100715534"}
# Zlecenie: these PESELs receive Składka chorobowa 2.45% (instead of 0%).
UMOWY_SPECIAL_PESELS: set[str] = {"83122516550", "91100715534"}
UMOWY_SPECIAL_CHOROBOWE_COLUMN = "Składka ub.chorob.[%]"
UMOWY_SPECIAL_CHOROBOWE_PERCENT = 2.45

UBEZPIECZENIA_SOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    "pesel": ("PESEL",),
    "nr_umowy": ("Numer umowy", "Nr umowy", "Number umowy", "Номер умовы"),
    "data_zawarcia": ("Data zawarcia umowy",),
    "kwota_netto": ("Kwota netto",),
    "kwota_brutto": ("Kwota Brutto", "Kwota brutto"),
    "kup": ("KUP %", "KUP"),
    "podatek": ("Podatek",),
}

FORMAT_UMOWY_TYPE_VALUE_MAP: dict[str, str] = {
    "umowazlecenia": "1",
    "umowazlecenie": "1",
    "umowanazlecenie": "1",
    "umowanazlecenia": "1",
    "umowaodzielo": "2",
    "umowaodziela": "2",
    "zlecenie": "1",
    "zlecenia": "1",
    "dzielo": "2",
    "dziela": "2",
}

# RU labels sometimes used in bilingual exports (polyglot compact string).
_RU_TYP_UMOWY_MARK_1 = ("поруч", "поручени", "поручит")
_RU_TYP_UMOWY_MARK_2 = ("подряд", "подрядн", "подряда", "подрядно")


# ─── Column-name normalisation ────────────────────────────────────────────────

def _normalize_column_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).strip().lower())


def _normalize_text_value(value: object) -> str:
    text = str(value).strip().lower()
    ascii_text = (
        unicodedata.normalize("NFKD", text)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return re.sub(r"[^a-z0-9]+", "", ascii_text)


_PL_LETTER_MAP: dict[int, str] = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)


def _compact_umowy_typ_alnum(value: object) -> str:
    """Compact alphanumeric key: Polish diacritics → base ASCII, then keep alnum only."""
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return ""
    except TypeError:
        if value is None:
            return ""
    text = str(value).strip().lower().translate(_PL_LETTER_MAP)
    if not text or text == "nan":
        return ""
    # Strip remaining combining marks (e.g. Cyrillic accents).
    decomposed = unicodedata.normalize("NFKD", text)
    parts: list[str] = []
    for ch in decomposed:
        if unicodedata.combining(ch):
            continue
        if ch.isalnum():
            parts.append(ch)
    return "".join(parts)


def _nonempty_umowy_typ_cell(value: object) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    s = str(value).strip()
    return bool(s) and s.lower() != "nan"


def _coalesce_typ_umowy_column_values(left: object, right: object) -> list[object]:
    """Row-wise: keep left cell when non-empty, else right. Same row count as inputs."""
    n = len(left)  # type: ignore[arg-type]
    nr = len(right)  # type: ignore[arg-type]
    out: list[object] = []
    for i in range(n):
        ai = left[i]  # type: ignore[index]
        bi = right[i] if i < nr else None
        if _nonempty_umowy_typ_cell(ai):
            out.append(ai)
        elif _nonempty_umowy_typ_cell(bi):
            out.append(bi)
        else:
            out.append(ai)
    return out


def _map_umowy_typ_text_to_12(value: object) -> object:
    """Map any Typ umowy label to '1' (zlecenie) or '2' (o dzieło)."""
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return value
    except TypeError:
        if value is None:
            return value
    # Plain numbers first (1, 2, 1.0 …)
    as_digit = _normalize_typ_umowy(value)
    if as_digit in ("1", "2"):
        return as_digit
    ascii_key = _normalize_text_value(value)
    poly_key = _compact_umowy_typ_alnum(value)
    for key in (poly_key, ascii_key):
        if not key:
            continue
        if key in FORMAT_UMOWY_TYPE_VALUE_MAP:
            return FORMAT_UMOWY_TYPE_VALUE_MAP[key]
    for key in (poly_key, ascii_key):
        if not key:
            continue
        m = re.match(r"^([12])(?![0-9])", key)
        if m:
            return m.group(1)
    for key in (poly_key, ascii_key):
        if not key:
            continue
        for tag in ("odziel", "dzielo", "dziela", "dzielu", "odzie"):
            if tag in key:
                return "2"
        if "zlecen" in key:
            return "1"
        for tag in _RU_TYP_UMOWY_MARK_2:
            if tag in key:
                return "2"
        for tag in _RU_TYP_UMOWY_MARK_1:
            if tag in key:
                return "1"
    return value


# ─── Numeric helpers ──────────────────────────────────────────────────────────

def _normalize_pesel(value: object) -> str:
    return _normalize_pesel_util(value)


def _pesel_to_display(value: object) -> object:
    try:
        if pd.isna(value):
            return value
    except (TypeError, ValueError):
        pass
    normalized = _normalize_pesel(value)
    return normalized if normalized else value


def _to_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("\xa0", "").replace(" ", "").replace("%", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _numeric_equal(a: object, b: object, tol: float = 1e-6) -> bool:
    fa = _to_float(a)
    fb = _to_float(b)
    if fa is None or fb is None:
        return False
    return abs(fa - fb) < tol


def _is_under_26_on_date_from_pesel(pesel: object, on_date_value: object) -> bool:
    birth = _birthdate_from_pesel(pesel)
    if birth is None:
        return False
    parsed_date = pd.to_datetime(on_date_value, dayfirst=True, errors="coerce")
    if pd.isna(parsed_date):
        return False
    on_date = parsed_date.date()
    return _age_on(birth, on_date) < 26


def _find_column(df: pd.DataFrame, aliases: tuple[str, ...]) -> Optional[str]:
    normalized_aliases = {_normalize_column_name(a) for a in aliases}
    for column in df.columns:
        if _normalize_column_name(str(column)) in normalized_aliases:
            return str(column)
    return None


def _is_excel_unnamed_column(name: object) -> bool:
    # Excel often stores an index column as "Unnamed: 0", "Unnamed: 1", etc.
    return _normalize_column_name(str(name)).startswith("unnamed")


def typ_umowy_kind(value: object) -> Optional[int]:
    raw = _normalize_typ_umowy(value)
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None
