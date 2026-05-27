from __future__ import annotations

import re

import pandas as pd

from .profiles import (
    EMPLOYEE_ADDRESS_IMPORT_PROFILE,
    EMPLOYEE_IMPORT_PROFILE,
    ImportProfile,
    PRZEPROWADZKI_IMPORT_PROFILE,
    UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE,
    UMOWY_DZIELO_IMPORT_PROFILE,
    UMOWY_IMPORT_PROFILE,
    UMOWY_MIXED_IMPORT_PROFILE,
    _effective_required_fields,
)
from .utils import (
    _normalize_pesel_value,
    _normalize_typ_ubezpieczenia,
    _normalize_typ_umowy,
    _to_float,
)


def _resolve_lookup_series(
    df: pd.DataFrame, mapping: dict[str, str], lookup_field: str
) -> pd.Series:
    # Defensive fix: if file has an explicit PESEL column, prefer it.
    if lookup_field == "PESEL":
        for col in df.columns:
            if str(col).strip().lower() == "pesel":
                return df[col]
    return df[mapping[lookup_field]]


def _optional_tax_rate_series(df: pd.DataFrame, index: pd.Index) -> pd.Series:
    for col in ("Stawka podatku [%]", "Ставка податка", "Stawka podatku", "stawka_podatku"):
        if col in df.columns:
            return df[col].map(lambda v: float(_to_float(v) or 0.0))
    return pd.Series([None] * len(index), index=index, dtype=object)


def read_excel(file_path: str) -> pd.DataFrame:
    return pd.read_excel(file_path)


def _norm_umowy_sheet_column(name: object) -> str:
    """Lowercase alnum-only key (same convention as format transform)."""
    return re.sub(r"[^a-z0-9]+", "", str(name).strip().lower())


def _sheet_looks_like_umowy_grid(df: pd.DataFrame) -> bool:
    """True if Excel sheet has columns typical for payroll system umowy / PPK export."""
    if df.empty or df.shape[1] < 2:
        return False
    keys = {_norm_umowy_sheet_column(c) for c in df.columns}
    return bool(
        keys
        & {
            "typ",
            "typumowy",
            "rodzajumowy",
            "pracownik",
            "zleceniobiorca",
            "pesel",
            "numberumowy",
            "numerumowy",
            "kwotabrutto",
        }
    )


def read_excel_umowy_format(file_path: str) -> tuple[pd.DataFrame, list[str]]:
    """Read workbook for UMOWY formatting: concatenate all plausible data sheets.

    payroll system often splits PPK auxiliary lines vs agreements across sheets while
    `pd.read_excel` default reads only the first sheet — PPK would be missing then.
    """
    xl = pd.ExcelFile(file_path)
    chunks: list[pd.DataFrame] = []
    used: list[str] = []
    for sn in xl.sheet_names:
        try:
            df = pd.read_excel(xl, sheet_name=sn)
        except (ValueError, OSError):
            continue
        if df.empty or df.shape[1] < 2:
            continue
        if _sheet_looks_like_umowy_grid(df):
            chunks.append(df)
            used.append(sn)
    if not chunks:
        first = xl.sheet_names[0]
        return pd.read_excel(file_path, sheet_name=first), [first]
    if len(chunks) == 1:
        return chunks[0].copy(), used
    merged = pd.concat(chunks, axis=0, ignore_index=True, sort=False)
    return merged, used


def preview_dataframe(df: pd.DataFrame, limit: int = 200) -> pd.DataFrame:
    return df.head(limit).copy()


def map_columns(
    df: pd.DataFrame,
    mapping: dict[str, str],
    profile: ImportProfile,
    employee_lookup_mode: str = "nr",
) -> pd.DataFrame:
    required_fields = _effective_required_fields(profile, employee_lookup_mode)
    missing = [field for field in required_fields if field not in mapping]
    if missing:
        raise ValueError(f"Brak mapowania dla pol: {', '.join(missing)}")

    source_columns = [mapping[field] for field in required_fields]
    for column in source_columns:
        if column not in df.columns:
            raise ValueError(f"Kolumna '{column}' nie istnieje w pliku Excel.")

    if profile.key == EMPLOYEE_IMPORT_PROFILE.key:
        return map_employee_columns(df, mapping)
    if profile.key == EMPLOYEE_ADDRESS_IMPORT_PROFILE.key:
        return map_employee_address_columns(df, mapping, employee_lookup_mode=employee_lookup_mode)
    if profile.key == UMOWY_IMPORT_PROFILE.key:
        return map_umowy_columns(df, mapping, employee_lookup_mode=employee_lookup_mode)
    if profile.key == UMOWY_MIXED_IMPORT_PROFILE.key:
        return map_umowy_columns(df, mapping, employee_lookup_mode=employee_lookup_mode)
    if profile.key == UMOWY_DZIELO_IMPORT_PROFILE.key:
        return map_umowy_dzielo_columns(df, mapping, employee_lookup_mode=employee_lookup_mode)
    if profile.key == UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE.key:
        return map_ubezpieczenia_obowiazkowe_columns(
            df, mapping, employee_lookup_mode=employee_lookup_mode
        )
    if profile.key == PRZEPROWADZKI_IMPORT_PROFILE.key:
        return map_przeprowadzki_columns(df, mapping, employee_lookup_mode=employee_lookup_mode)
    return map_legacy_urzedy_columns(df, mapping, employee_lookup_mode=employee_lookup_mode)


def map_employee_columns(df: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
    nazwisko = df[mapping["Nazwisko"]].astype(str).str.strip()
    imie = df[mapping["Imie"]].astype(str).str.strip()
    full_name = (nazwisko + " " + imie).str.replace(r"\s+", " ", regex=True).str.strip()
    return pd.DataFrame(
        {
            "full_name": full_name,
            "birth_date": df[mapping["Data urodzenia"]].astype(str).str.strip(),
            "pesel": df[mapping["PESEL"]].apply(_normalize_pesel_value),
            "id_card_no": df[mapping["Nr dowodu"]].astype(str).str.strip(),
            "passport_no": df[mapping["Nr paszportu"]].astype(str).str.strip(),
            "phone": df[mapping["telefon"]].astype(str).str.strip(),
            "country": df[mapping["Kraj"]].astype(str).str.strip(),
            "voivodeship": df[mapping["Wojewodztwo"]].astype(str).str.strip(),
            "powiat": df[mapping["Powiat"]].astype(str).str.strip(),
            "gmina": df[mapping["Gmina"]].astype(str).str.strip(),
            "street": df[mapping["Ulica"]].astype(str).str.strip(),
            "house_no": df[mapping["Numer Domu"]].astype(str).str.strip(),
            "flat_no": df[mapping["Numer lokalu"]].astype(str).str.strip(),
            "city": df[mapping["Miejscowosc"]].astype(str).str.strip(),
            "postal_code": df[mapping["Kod pocztowy"]].astype(str).str.strip(),
            "post_office": df[mapping["Poczta"]].astype(str).str.strip(),
            "urzad_name": df[mapping["nazwa Urząd Skarbowy"]].astype(str).str.strip(),
            "data_od_source": df[mapping["Od dnia"]],
        }
    )


def map_legacy_urzedy_columns(
    df: pd.DataFrame, mapping: dict[str, str], employee_lookup_mode: str = "nr"
) -> pd.DataFrame:
    lookup_field = "PESEL" if employee_lookup_mode == "pesel" else "Nr Ewidencyjny"
    return pd.DataFrame(
        {
            "kod_us": df[mapping["Kod US"]].astype(str).str.strip(),
            "nazwa": df[mapping["Nazwa"]].astype(str).str.strip(),
            "employee_lookup_value": df[mapping[lookup_field]].astype(str).str.strip(),
            "employee_lookup_mode": employee_lookup_mode,
            "data_od_source": df[mapping["Od dnia"]],
        }
    )


def map_employee_address_columns(
    df: pd.DataFrame, mapping: dict[str, str], employee_lookup_mode: str = "nr"
) -> pd.DataFrame:
    lookup_field = "PESEL" if employee_lookup_mode == "pesel" else "NR Ewidencyjny"
    lookup_series = _resolve_lookup_series(df, mapping, lookup_field)
    return pd.DataFrame(
        {
            "country": df[mapping["Kraj"]].astype(str).str.strip(),
            "voivodeship": df[mapping["Wojewodztwo"]].astype(str).str.strip(),
            "powiat": df[mapping["Powiat"]].astype(str).str.strip(),
            "gmina": df[mapping["Gmina"]].astype(str).str.strip(),
            "street": df[mapping["Ulica"]].astype(str).str.strip(),
            "house_no": df[mapping["Numer Domu"]].astype(str).str.strip(),
            "flat_no": df[mapping["Numer lokalu"]].astype(str).str.strip(),
            "city": df[mapping["Miejscowosc"]].astype(str).str.strip(),
            "postal_code": df[mapping["Kod pocztowy"]].astype(str).str.strip(),
            "post_office": df[mapping["Poczta"]].astype(str).str.strip(),
            "employee_lookup_value": (
                lookup_series.apply(_normalize_pesel_value)
                if lookup_field == "PESEL"
                else lookup_series.astype(str).str.strip()
            ),
            "employee_lookup_mode": employee_lookup_mode,
            "data_od_source": df[mapping["Od dnia"]],
        }
    )


def map_umowy_columns(
    df: pd.DataFrame, mapping: dict[str, str], employee_lookup_mode: str = "nr"
) -> pd.DataFrame:
    lookup_field = "PESEL" if employee_lookup_mode == "pesel" else "NR Ewidencyjny"
    lookup_series = _resolve_lookup_series(df, mapping, lookup_field)
    idx = df.index
    ppk_col = "PPK pracownika PLN"
    ppk_series = df[ppk_col] if ppk_col in df.columns else pd.Series(0.0, index=idx, dtype=float)
    name_series: pd.Series | None = None
    for col in ("__ppk_match_osoba", "Pracownik"):
        if col in df.columns:
            name_series = df[col].astype(str).str.strip()
            break
    if name_series is None:
        name_series = pd.Series("", index=idx, dtype=object)
    return pd.DataFrame(
        {
            "employee_lookup_value": (
                lookup_series.apply(_normalize_pesel_value)
                if lookup_field == "PESEL"
                else lookup_series.astype(str).str.strip()
            ),
            "employee_lookup_mode": employee_lookup_mode,
            "numer_umowy": df[mapping["номер умовы"]].astype(str).str.strip(),
            "numer_rachunku": df[mapping["номер рахунка"]].astype(str).str.strip(),
            "typ_umowy": df[mapping["Тип умовы"]].apply(_normalize_typ_umowy),
            "ppk_match_name": name_series,
            "ppk_pracownika_kwota": ppk_series.map(lambda v: float(_to_float(v) or 0.0)),
            "data_wyplaty_source": df[mapping["Дата выплаты"]],
            "data_umowy_source": df[mapping["Дата умовы"]],
            "forma_podatka": df[mapping["Форма податка"]].astype(str).str.strip(),
            "stawka_podatku_proc": _optional_tax_rate_series(df, idx),
            "wynagrodzenie_brutto_source": df[mapping["Kwota brutto"]],
            "koszty_proc_source": df[mapping["KOSZTY UZYSKANIA PRZYCHODU %"]],
            "emerytalne_proc_source": df[mapping["Skł.na ub.emerytal.[%]"]],
            "rentowe_u_proc_source": df[mapping["Składka ub.rent. U [%]"]],
            "rentowe_p_proc_source": df[mapping["Składka ub.rent. P [%]"]],
            "chorobowe_proc_source": df[mapping["Składka ub.chorob.[%]"]],
            "wypadkowe_proc_source": df[mapping["Składka ub.wypadk.[%]"]],
            "zdrowotne_proc_source": df[mapping["Składka ub.zdrowotne[%]"]],
            "fp_proc_source": df[mapping["FP [%]"]],
            "fgsp_proc_source": df[mapping["FGŚP [%]"]],
        },
        index=idx,
    )


def map_umowy_dzielo_columns(
    df: pd.DataFrame, mapping: dict[str, str], employee_lookup_mode: str = "nr"
) -> pd.DataFrame:
    lookup_field = "PESEL" if employee_lookup_mode == "pesel" else "NR Ewidencyjny"
    lookup_series = _resolve_lookup_series(df, mapping, lookup_field)
    idx = df.index
    return pd.DataFrame(
        {
            "employee_lookup_value": (
                lookup_series.apply(_normalize_pesel_value)
                if lookup_field == "PESEL"
                else lookup_series.astype(str).str.strip()
            ),
            "employee_lookup_mode": employee_lookup_mode,
            "numer_umowy": df[mapping["номер умовы"]].astype(str).str.strip(),
            "numer_rachunku": df[mapping["номер рахунка"]].astype(str).str.strip(),
            "typ_umowy": pd.Series(["2"] * len(idx), index=idx, dtype=object),
            "data_wyplaty_source": df[mapping["Дата выплаты"]],
            "data_umowy_source": df[mapping["Дата умовы"]],
            "forma_podatka": df[mapping["Форма податка"]].astype(str).str.strip(),
            "stawka_podatku_proc": _optional_tax_rate_series(df, idx),
            "wynagrodzenie_brutto_source": df[mapping["Kwota brutto"]],
            "koszty_proc_source": df[mapping["KOSZTY UZYSKANIA PRZYCHODU %"]],
        },
        index=idx,
    )


def map_ubezpieczenia_obowiazkowe_columns(
    df: pd.DataFrame, mapping: dict[str, str], employee_lookup_mode: str = "nr"
) -> pd.DataFrame:
    lookup_field = "PESEL" if employee_lookup_mode == "pesel" else "NR Ewidencyjny"
    lookup_series = _resolve_lookup_series(df, mapping, lookup_field)
    return pd.DataFrame(
        {
            "employee_lookup_value": (
                lookup_series.apply(_normalize_pesel_value)
                if lookup_field == "PESEL"
                else lookup_series.astype(str).str.strip()
            ),
            "employee_lookup_mode": employee_lookup_mode,
            "numer_umowy": df[mapping["Номер умовы"]].astype(str).str.strip(),
            "typ_ubezpieczenia": df[mapping["Typ ubezpieczenia"]].apply(
                _normalize_typ_ubezpieczenia
            ),
            "data_obowiazku_ubezpieczenia_source": df[
                mapping["Data powstania obowiazku ubezpieczenia"]
            ],
            "ubezpieczenie_emerytalne_source": df[
                mapping["Osoba podlega ubezpieczeniu Emerytalnemu"]
            ],
            "ubezpieczenie_rentowe_source": df[
                mapping["Osoba podlega ubezpieczeniu Rentowemu"]
            ],
            "ubezpieczenie_wypadkowe_source": df[
                mapping["Osoba podlega ubezpieczeniu Wypadkowemu"]
            ],
            "ubezpieczenie_chorobowe_source": df[
                mapping["Osoba podlega ubezpieczeniu Chorobowemu"]
            ],
        }
    )


def map_przeprowadzki_columns(
    df: pd.DataFrame, mapping: dict[str, str], employee_lookup_mode: str = "nr"
) -> pd.DataFrame:
    lookup_field = "PESEL" if employee_lookup_mode == "pesel" else "NR Ewidencyjny"
    lookup_series = _resolve_lookup_series(df, mapping, lookup_field)
    return pd.DataFrame(
        {
            "country": df[mapping["Kraj"]].astype(str).str.strip(),
            "voivodeship": df[mapping["Wojewodztwo"]].astype(str).str.strip(),
            "powiat": df[mapping["Powiat"]].astype(str).str.strip(),
            "gmina": df[mapping["Gmina"]].astype(str).str.strip(),
            "street": df[mapping["Ulica"]].astype(str).str.strip(),
            "house_no": df[mapping["Numer Domu"]].astype(str).str.strip(),
            "flat_no": df[mapping["Numer lokalu"]].astype(str).str.strip(),
            "city": df[mapping["Miejscowosc"]].astype(str).str.strip(),
            "postal_code": df[mapping["Kod pocztowy"]].astype(str).str.strip(),
            "post_office": df[mapping["Poczta"]].astype(str).str.strip(),
            "urzad_name": df[mapping["nazwa Urząd Skarbowy"]].astype(str).str.strip(),
            "employee_lookup_value": (
                lookup_series.apply(_normalize_pesel_value)
                if lookup_field == "PESEL"
                else lookup_series.astype(str).str.strip()
            ),
            "employee_lookup_mode": employee_lookup_mode,
            "data_od_source": df[mapping["Od dnia"]],
        }
    )
