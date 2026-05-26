"""Merge PPK auxiliary lines with contract rows (by worker identity, not row order)."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any

import pandas as pd

from .utils import _normalize_pesel_value, _to_float


# Cyrillic lookalikes for P/K (exports sometimes contaminate Latin "PPK" label).
_PPK_CYR_TO_LATIN_PK = str.maketrans(
    {
        "\u0420": "P",
        "\u0440": "p",
        "\u041a": "K",
        "\u043a": "k",
    }
)


def _ppk_normalize_label_text(value: object) -> str:
    """Normalize cell text so Latin / Cyrillic confusables still match ``PPK``."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    t = unicodedata.normalize("NFKC", str(value))
    return t.translate(_PPK_CYR_TO_LATIN_PK).strip().lower()


def _norm_typ_text(value: object) -> str:
    return str(value or "").strip().lower()


def is_ppk_typ_cell(value: object) -> bool:
    s = _ppk_normalize_label_text(value)
    if not s:
        return False
    n = re.sub(r"\s+", "", s)
    return s == "ppk" or n == "ppk" or s.startswith("ppk ") or "ppk" in n


_PL_CONTRACT_MAP = str.maketrans(
    "\u0105\u0107\u0119\u0142\u0144\u00f3\u015b\u017a\u017c"
    "\u0104\u0106\u0118\u0141\u0143\u00d3\u015a\u0179\u017b",
    "acelnoszzACELNOSZZ",
)


def _norm_contract_ascii(s: str) -> str:
    """Lowercase, translate PL diacritics to ASCII, remove non-alnum."""
    return re.sub(r"[^a-z0-9]", "", s.lower().translate(_PL_CONTRACT_MAP))


def is_contract_typ_for_merge(value: object) -> bool:
    """True for zlecenie / o dzieło / numeric 1–2; false for PPK-only lines."""
    if is_ppk_typ_cell(value):
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if pd.isna(value):
                return False
        except (TypeError, ValueError):
            pass
        try:
            return int(float(value)) in (1, 2)
        except (TypeError, ValueError):
            pass
    s = _norm_typ_text(value)
    if not s:
        return False
    if s in {"1", "2", "1.0", "2.0"}:
        return True
    # Raw text checks (works for already-normalized values or obvious substrings).
    if "zlecenie" in s or "zlecenia" in s:
        return True
    if "dzie" in s or "dzielo" in s:
        return True
    # Normalized ASCII check — handles Polish diacritics (ł→l, ó→o, ź→z …).
    n = _norm_contract_ascii(s)
    if "zleceni" in n or "zlecenie" in n:
        return True
    if "dzielo" in n or "odziel" in n or "odzie" in n:
        return True
    # Leading digit 1 or 2 (e.g. "1 - Umowa zlecenia").
    if re.match(r"^[12](?![0-9])", n):
        return True
    return False


def normalize_osoba_key(name: str) -> str:
    return " ".join(name.lower().split())


def _pesel_key_from_series(row: pd.Series) -> str:
    for col in row.index:
        if "pesel" in str(col).lower():
            p = _normalize_pesel_value(row[col])
            if len(p) >= 11:
                return f"id:{p}"
    return ""


# Column names that may hold the worker's full name in various WaPro exports.
_NAME_COLS_PRIORITY = (
    "__ppk_match_osoba",  # internal: renamed from "Pracownik" during format transform
    "Pracownik",
    "Zleceniobiorca",
    "Imię i Nazwisko",
    "Imie i Nazwisko",
    "Nazwisko i Imię",
    "Nazwisko i Imie",
    "Osoba",
)

# Normalized (alnum-only lowercase) versions of the above for fuzzy column lookup.
_NAME_COLS_NORM = frozenset(
    re.sub(r"[^a-z0-9]", "", c.lower()) for c in _NAME_COLS_PRIORITY
)


def _find_name_in_row(row: pd.Series) -> str:
    """Return worker full name from the row using known column names or heuristics."""
    # 1. Exact / known column names.
    for col in _NAME_COLS_PRIORITY:
        if col in row.index:
            s = str(row.get(col) or "").strip()
            if s and s.lower() not in ("nan", "none", ""):
                return s
    # 2. Normalized column name lookup (handles minor spelling variants).
    for col in row.index:
        norm = re.sub(r"[^a-z0-9]", "", str(col).lower())
        if norm in _NAME_COLS_NORM:
            s = str(row.get(col) or "").strip()
            if s and s.lower() not in ("nan", "none", ""):
                return s
    # 3. Heuristic: any column whose name contains "pracow" / "zleceniob" / "osoba".
    for col in row.index:
        col_l = str(col).lower()
        if any(kw in col_l for kw in ("pracow", "zleceniob", "osoba")):
            s = str(row.get(col) or "").strip()
            if s and s.lower() not in ("nan", "none", ""):
                return s
    # 4. Heuristic: column immediately after "Typ umowy" in the row index
    #    (in WaPro exports the name column follows the type column).
    idx_list = list(row.index)
    for i, col in enumerate(idx_list):
        if str(col).lower() in ("typ umowy", "typ", "typ_umowy"):
            for j in range(i + 1, min(i + 4, len(idx_list))):
                s = str(row.iloc[j] if hasattr(row, "iloc") else row.get(idx_list[j]) or "").strip()
                # Accept only if it looks like a name (has a space, no digits, reasonable length).
                if s and " " in s and len(s) > 4 and not any(ch.isdigit() for ch in s):
                    return s
            break
    return ""


def format_worker_match_key(row: pd.Series) -> str:
    """Match PPK rows to contract rows by worker name or PESEL."""
    name = _find_name_in_row(row)
    if name:
        return f"n:{normalize_osoba_key(name)}"
    return _pesel_key_from_series(row)


def mapped_worker_match_key(row: pd.Series) -> str:
    name = str(row.get("ppk_match_name") or "").strip()
    if name:
        return f"n:{normalize_osoba_key(name)}"
    mode = str(row.get("employee_lookup_mode") or "nr").strip().lower()
    if mode == "pesel":
        p = _normalize_pesel_value(row.get("employee_lookup_value"))
        if len(p) >= 11:
            return f"id:{p}"
    ev = str(row.get("employee_lookup_value") or "").strip()
    if ev:
        return f"u:{mode}:{ev.lower()}"
    return ""


_SKIP_COLS_FOR_PPK_EXTRACT = frozenset(
    {"Typ umowy", "Forma Opodtkowania", "employee_lookup_mode",
     "typ_umowy", "forma_podatka", "employee_lookup_mode"}
)


def extract_ppk_kwota_from_row(ppk_row: pd.Series, contract_row: pd.Series | None = None) -> float:
    """Best-effort PPK employee amount from the auxiliary row.

    Priority order:
    1. Any column whose name contains 'ppk' (e.g. 'PPK pracownika PLN').
    2. 'Kwota brutto' / 'wynagrodzenie_brutto_source' — only when it looks like
       a PPK-sized amount (not equal to the contract's full brutto).
    3. Fallback: largest remaining positive value that is clearly smaller than
       the contract brutto.
    """
    # --- 1. Explicit PPK-named column (highest confidence) ---
    for col in ppk_row.index:
        if "ppk" in str(col).lower():
            f = _to_float(ppk_row[col])
            if f is not None and f > 0:
                return float(f)

    # --- Determine contract brutto for sanity checks ---
    brutto_ctr: float | None = None
    for ctr_key in ("Kwota brutto", "wynagrodzenie_brutto_source"):
        if contract_row is not None and ctr_key in contract_row.index:
            brutto_ctr = brutto_ctr or _to_float(contract_row.get(ctr_key))

    # --- 2. 'Kwota brutto' / 'wynagrodzenie_brutto_source' ---
    for key in ("Kwota brutto", "wynagrodzenie_brutto_source"):
        if key in ppk_row.index:
            f = _to_float(ppk_row.get(key))
            if f is None or f <= 0:
                continue
            # Skip if it equals the contract brutto — that means the column was
            # copied from the contract row and does NOT represent the PPK amount.
            if brutto_ctr is not None and f >= brutto_ctr * 0.90:
                continue
            return float(f)

    # --- 2b. WaPro / Sheets: PPK auxiliary rows often put the contribution in **Podatek** ---
    # (same header as PIT withholdings on umowa rows.)
    _podatek_keys = ("Podatek", "podatek pit", "zaliczka pit")
    for key in _podatek_keys:
        if key not in ppk_row.index:
            continue
        f = _to_float(ppk_row.get(key))
        if f is None or f <= 0:
            continue
        if brutto_ctr is not None and f >= brutto_ctr * 0.90:
            continue
        return float(f)

    # --- 3. Fallback: largest positive value that is not the contract brutto ---
    best = 0.0
    for col in ppk_row.index:
        if col in _SKIP_COLS_FOR_PPK_EXTRACT:
            continue
        f = _to_float(ppk_row[col])
        if f is None or f <= 0:
            continue
        if brutto_ctr is not None and f >= brutto_ctr * 0.90:
            continue
        if f >= 1e8:
            continue
        best = max(best, f)
    return float(best)


def _find_adjacent_contract_row_index(
    df: pd.DataFrame,
    idx_list: list[Any],
    typ_col: str,
    ppk_pos: int,
) -> Any | None:
    n = len(idx_list)
    for delta in (1, -1, 2, -2, 3, -3, 4, -4, 5, -5):
        np_ = ppk_pos + delta
        if 0 <= np_ < n:
            nidx = idx_list[np_]
            if is_contract_typ_for_merge(df.at[nidx, typ_col]):
                return nidx
    return None


def _safe_ppk_add(current: Any, delta: float) -> float:
    """Add delta to current PPK value, treating None/NaN as 0."""
    try:
        f = float(current)
        if f != f:  # NaN check
            f = 0.0
    except (TypeError, ValueError):
        f = 0.0
    return f + delta


def merge_ppk_companion_rows_format(
    df: pd.DataFrame,
    debug_log: list[str] | None = None,
) -> pd.DataFrame:
    """Drop PPK rows; sum PPK amounts per worker key onto the first matching contract row.

    Parameters
    ----------
    debug_log:
        If a list is supplied, diagnostic messages are appended to it so the
        caller (main.py) can forward them to the UI log without this module
        depending on UI internals.
    """

    def _dbg(msg: str) -> None:
        if debug_log is not None:
            debug_log.append(msg)

    # Try common alternative names for the type column.
    typ_col: str | None = None
    for _candidate in ("Typ umowy", "Rodzaj umowy", "Typ", "typ_umowy"):
        if _candidate in df.columns:
            typ_col = _candidate
            break

    if df.empty or typ_col is None:
        _dbg(f"[ppk/merge] brak kolumny Typ umowy — kolumny: {list(df.columns)}")
        return df

    out = df.copy()
    ppk_col = "PPK pracownika PLN"
    if ppk_col not in out.columns:
        out[ppk_col] = 0.0
    else:
        # Normalize existing values to float (replace NaN with 0).
        out[ppk_col] = out[ppk_col].apply(
            lambda v: 0.0 if v is None or (isinstance(v, float) and v != v) else float(v)
        )

    idx_list = list(out.index)

    # Log ALL unique type values so the caller can see what the file contains.
    unique_types = sorted({str(out.at[i, typ_col]) for i in idx_list})
    _dbg(f"[ppk/merge] kolumna={typ_col!r} wartości: {unique_types}")

    # Also scan every OTHER column for rows that contain 'ppk' somewhere — these
    # are PPK rows where the type column wasn't the right column (WaPro formats vary).
    _ppk_col_candidates: list[str] = []
    for _c in out.columns:
        if _c == typ_col:
            continue
        col_l = str(_c).lower()
        if "ppk" in col_l:
            _ppk_col_candidates.append(_c)
    if _ppk_col_candidates:
        _dbg(f"[ppk/merge] kolumny zawierające 'ppk' w nazwie: {_ppk_col_candidates}")

    ppk_total_by_key: dict[str, float] = defaultdict(float)
    orphan_ppk: list[tuple[int, float]] = []

    for pos, i0 in enumerate(idx_list):
        t0 = out.at[i0, typ_col]
        # Standard detection: type column contains 'ppk'.
        _is_ppk_row = is_ppk_typ_cell(t0)
        # Fallback 1: another column explicitly named with 'ppk' has a non-empty value.
        if not _is_ppk_row:
            for _pc in _ppk_col_candidates:
                _pv = str(out.at[i0, _pc] or "").strip().lower()
                if "ppk" in _pv:
                    _is_ppk_row = True
                    break
        # Fallback 2: scan ALL columns for a PPK label (incl. Cyrillic lookalikes).
        if not _is_ppk_row:
            for _c2 in out.columns:
                _raw = out.at[i0, _c2]
                if _raw is None:
                    continue
                if is_ppk_typ_cell(_raw):
                    _is_ppk_row = True
                    _dbg(
                        f"[ppk/merge] PPK wykryty przez kolumnę {_c2!r}="
                        f"{out.at[i0, _c2]!r} w wierszu pos={pos}"
                    )
                    break

        # Fallback 3: NaN / empty type — two sub-cases.
        # 3A: "PPK pracownika PLN" already has a positive value.
        # 3B: "Kwota brutto" has a value (WaPro puts the PPK contribution amount
        #     there when the row has no type label).
        if not _is_ppk_row:
            _norm_t0 = str(t0 or "").strip().lower()
            if not _norm_t0 or _norm_t0 == "nan":
                # 3A
                _existing = _to_float(out.at[i0, ppk_col]) if ppk_col in out.columns else None
                if _existing is not None and _existing > 0:
                    _is_ppk_row = True
                    _dbg(f"[ppk/merge] PPK wykryty 3A: {ppk_col!r}={_existing} (pusty typ) pos={pos}")
                else:
                    # 3B — look for Kwota brutto / any amount column
                    _brutto_v: float | None = None
                    for _bk in ("Kwota brutto", "kwota_brutto", "wynagrodzenie_brutto_source", "Kwota"):
                        if _bk in out.columns:
                            _bv = _to_float(out.at[i0, _bk])
                            if _bv is not None and _bv > 0:
                                _brutto_v = _bv
                                break
                    if _brutto_v is not None and _brutto_v > 0:
                        _is_ppk_row = True
                        _dbg(f"[ppk/merge] PPK wykryty 3B: kwota_brutto={_brutto_v} (pusty typ) pos={pos}")
                    else:
                        # Diagnostic: show first 3 undetected NaN rows so we can see their content.
                        _nan_diag_count = sum(
                            1 for _msg in (debug_log or [])
                            if "[ppk/merge] NaN-wiersz" in _msg
                        )
                        if _nan_diag_count < 3:
                            nonempty: dict[str, str] = {}
                            for c in out.columns:
                                rv = out.at[i0, c]
                                if rv is None:
                                    continue
                                try:
                                    if pd.isna(rv):
                                        continue
                                except (TypeError, ValueError):
                                    pass
                                s = str(rv).strip()
                                if not s or s.lower() == "nan":
                                    continue
                                nonempty[str(c)] = s[:160]
                            if not nonempty:
                                _dbg(
                                    f"[ppk/merge] NaN-wiersz pos={pos}: całkiem pusty "
                                    f"(ostatnie linie arkusza / merged cells / brak pierwszego arkusza PPK)."
                                )
                            else:
                                _dbg(
                                    f"[ppk/merge] NaN-wiersz pos={pos} (nie PPK — brak Kwoty/brutu i PPK col): "
                                    f"{nonempty}"
                                )

        if not _is_ppk_row:
            continue
        row = out.loc[i0]
        key = format_worker_match_key(row)
        adj_idx = _find_adjacent_contract_row_index(out, idx_list, typ_col, pos)
        contract_row_for_extract = out.loc[adj_idx] if adj_idx is not None else None
        kw = extract_ppk_kwota_from_row(row, contract_row_for_extract)
        _dbg(
            f"[ppk/merge] PPK wiersz pos={pos} typ={t0!r} "
            f"key={key!r} kwota={kw} adj_typ="
            f"{out.at[adj_idx, typ_col] if adj_idx is not None else None!r}"
        )
        if key:
            ppk_total_by_key[key] += kw
        else:
            orphan_ppk.append((pos, kw))
            _dbg(f"[ppk/merge] → sierota (brak klucza), kwota={kw}")

    _dbg(f"[ppk/merge] klucze PPK: {dict(ppk_total_by_key)}")
    _dbg(f"[ppk/merge] sieroty: {orphan_ppk}")

    contracts: list[tuple[Any, str, dict[str, Any]]] = []
    for i0 in idx_list:
        t = out.at[i0, typ_col]
        if is_contract_typ_for_merge(t):
            row = out.loc[i0]
            k = format_worker_match_key(row)
            d = row.to_dict()
            d[ppk_col] = _safe_ppk_add(d.get(ppk_col), 0.0)
            contracts.append((i0, k, d))

    _dbg(f"[ppk/merge] umów do merge: {len(contracts)}")

    assigned_key: set[str] = set()
    rows_out: list[dict[str, Any]] = []
    orig_idx_to_contract: dict[Any, dict[str, Any]] = {}

    for orig_idx, k, d in contracts:
        amt = 0.0
        if k and k in ppk_total_by_key and k not in assigned_key:
            amt = ppk_total_by_key[k]
            assigned_key.add(k)
            _dbg(f"[ppk/merge] dopasowanie klucz={k!r} kwota={amt}")
        d[ppk_col] = _safe_ppk_add(d.get(ppk_col), amt)
        d.pop("__ppk_match_osoba", None)
        rows_out.append(d)
        orig_idx_to_contract[orig_idx] = d

    for ppk_pos, kw in orphan_ppk:
        tgt = _find_adjacent_contract_row_index(out, idx_list, typ_col, ppk_pos)
        if tgt is not None and tgt in orig_idx_to_contract:
            orig_idx_to_contract[tgt][ppk_col] = _safe_ppk_add(
                orig_idx_to_contract[tgt].get(ppk_col), kw
            )
            _dbg(f"[ppk/merge] sierota pos={ppk_pos} → tgt={tgt} kwota={kw}")
        else:
            _dbg(f"[ppk/merge] sierota pos={ppk_pos} — brak pasującego wiersza umowy")

    return pd.DataFrame(rows_out)


def merge_ppk_companion_rows_mapped(df: pd.DataFrame) -> pd.DataFrame:
    """Same logic on mapped import rows (typ_umowy, employee_lookup_*, ppk_match_name)."""
    if df.empty or "typ_umowy" not in df.columns:
        return df
    out = df.copy()
    idx_list = list(out.index)
    n = len(idx_list)
    typ_col = "typ_umowy"

    ppk_total_by_key: dict[str, float] = defaultdict(float)
    orphan_ppk: list[tuple[int, float]] = []

    for pos, i0 in enumerate(idx_list):
        t0 = out.at[i0, typ_col]
        if not is_ppk_typ_cell(t0):
            continue
        row = out.loc[i0]
        key = mapped_worker_match_key(row)
        kw = extract_ppk_kwota_from_row(row, None)
        if key:
            ppk_total_by_key[key] += kw
        else:
            orphan_ppk.append((pos, kw))

    contracts: list[tuple[Any, str, dict[str, Any]]] = []
    for i0 in idx_list:
        t = out.at[i0, typ_col]
        if is_contract_typ_for_merge(t):
            row = out.loc[i0]
            k = mapped_worker_match_key(row)
            d = row.to_dict()
            d.setdefault("ppk_pracownika_kwota", 0.0)
            contracts.append((i0, k, d))

    assigned_key: set[str] = set()
    rows_out: list[dict[str, Any]] = []
    orig_idx_to_contract: dict[Any, dict[str, Any]] = {}

    for orig_idx, k, d in contracts:
        amt = 0.0
        if k and k in ppk_total_by_key and k not in assigned_key:
            amt = float(ppk_total_by_key[k])
            assigned_key.add(k)
        d["ppk_pracownika_kwota"] = float(d.get("ppk_pracownika_kwota", 0) or 0) + amt
        d.pop("ppk_match_name", None)
        rows_out.append(d)
        orig_idx_to_contract[orig_idx] = d

    for ppk_pos, kw in orphan_ppk:
        tgt = _find_adjacent_contract_row_index(out, idx_list, typ_col, ppk_pos)
        if tgt is not None and tgt in orig_idx_to_contract:
            d = orig_idx_to_contract[tgt]
            d["ppk_pracownika_kwota"] = float(d.get("ppk_pracownika_kwota", 0) or 0) + float(kw)

    return pd.DataFrame(rows_out)
