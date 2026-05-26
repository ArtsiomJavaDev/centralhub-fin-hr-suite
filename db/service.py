from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_DOWN, ROUND_HALF_UP
from difflib import SequenceMatcher
import re
from typing import Any, Callable, Optional
import unicodedata
from urllib.parse import quote_plus

import pandas as pd

from utils.pesel import (
    birthdate_from_pesel as _birthdate_from_pesel_util,
    age_on as _age_on_util,
    is_female_from_pesel as _is_female_from_pesel_util,
    first_day_of_next_month as _first_day_of_next_month_util,
)


ProgressCallback = Callable[[int, int], None]


class ImportCancelled(Exception):
    """Raised inside execute_* when the user requested cancellation.

    The surrounding `with engine.begin()` rolls back the transaction, so no
    partial rows are left in the database.
    """


class CancelToken:
    """Thread-safe cancellation flag shared between UI and DB worker."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled


def _notify_progress(callback: ProgressCallback | None, done: int, total: int) -> None:
    if callback is None:
        return
    try:
        callback(done, total)
    except Exception:
        # Progress reporting must never break the import itself.
        pass


def _raise_if_cancelled(cancel_token: CancelToken | None) -> None:
    if cancel_token is not None and cancel_token.is_cancelled:
        raise ImportCancelled("Импорт отменён пользователем")


# Single source of truth for DB column string limits. Matches PAYROLL_DB schema.
# Grouped by table for readability. Use `_fit(value, DB_FIELD_LIMITS[...])`
# instead of raw [:N] literals scattered across the file.
DB_FIELD_LIMITS: dict[str, int] = {
    # URZEDY
    "URZEDY.NAZWA": 60,
    "URZEDY.KOD_US": 10,
    "URZEDY.TYP_URZEDU": 10,
    # PRACOWNIK
    "PRACOWNIK.NAZWISKO": 40,
    "PRACOWNIK.IMIE_1": 30,
    "PRACOWNIK.PESEL": 20,
    "PRACOWNIK.DOWOD": 20,
    "PRACOWNIK.PASZPORT": 20,
    "PRACOWNIK.TELEFON": 30,
    "PRACOWNIK.NR_EWIDENCYJNY": 30,
    # ADRESY_PRACOWNIKA
    "ADRESY_PRACOWNIKA.WOJEWODZTWO": 30,
    "ADRESY_PRACOWNIKA.POWIAT": 40,
    "ADRESY_PRACOWNIKA.GMINA": 40,
    "ADRESY_PRACOWNIKA.MIEJSCOWOSC": 40,
    "ADRESY_PRACOWNIKA.KOD_POCZTOWY": 20,
    "ADRESY_PRACOWNIKA.POCZTA": 40,
    "ADRESY_PRACOWNIKA.ULICA": 40,
    "ADRESY_PRACOWNIKA.NR_DOMU": 10,
    "ADRESY_PRACOWNIKA.NR_LOKALU": 10,
    "ADRESY_PRACOWNIKA.TELEFON": 50,
    "ADRESY_PRACOWNIKA.KRAJ": 50,
    # GANG_UMOWY_CYWILNO_PRAWNE
    "GANG_UMOWY_CYWILNO_PRAWNE.NUMER_UMOWY": 100,
    "GANG_UMOWY_CYWILNO_PRAWNE.NUMER_RACHUNKU": 100,
    "GANG_UMOWY_CYWILNO_PRAWNE.RODZAJ_UMOWY": 100,
    "GANG_UMOWY_CYWILNO_PRAWNE.FORMA_OPODATKOWANIA": 100,
    # GANG_UBEZPIECZENIA_OBOWIAZKOWE
    "GANG_UBEZPIECZENIA_OBOWIAZKOWE.KOD_TYTULU": 100,
}


def _fit(value: object, limit_key: str) -> str:
    """Trim a string to the configured DB column limit (single source of truth)."""
    limit = DB_FIELD_LIMITS.get(limit_key)
    text = "" if value is None else str(value)
    if limit is None:
        return text
    return text[:limit]


def _optional_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


_CLARION_BASE_DATE = date(1800, 12, 28)
PIT_ZERO_AGE_THRESHOLD = 26
PIT_DEFAULT_STAWKA = 12.0
# Próg brutto, poniżej którego nie nalicza się Funduszu Pracy = miesięczna
# minimalna płaca obowiązująca w 2026 r. (4 806,00 PLN). Trzymamy ten próg jako
# float, bo logika importu operuje na float; źródło prawdy: MINIMALNA_PLACA_MIESIECZNA
# w tax_calc_2026.py — przy aktualizacji minimalnej płacy zmieniaj tam i tutaj.
FP_MIN_BRUTTO_2026 = 4806.0

# Standard PPK rates applied when the source row signals an active PPK participant.
# The pracownik (uczestnik) podstawowa rate is deducted from the bank transfer;
# the pracodawca podstawowa rate is paid by the foundation on top of brutto and
# becomes additional PIT-able income for the participant per art. 12 ust. 1 ustawy
# o PIT, but is NOT deducted from KWOTA_DO_WYPLATY.
PPK_UCZESTNIK_PODSTAWOWA_PROC = 2.00
PPK_PRACODAWCA_PODSTAWOWA_PROC = 1.50

# Employer rates for umowy zlecenie — per company (ID_FIRMY).
# Default rates apply when the company is not listed explicitly.
# Students (ZUS-exempt rows) always get 0 regardless of these values.
_WYPADKOWE_DEFAULT: float = 0.67
_FGSP_DEFAULT: float = 0.00
_WYPADKOWE_BY_FIRMA: dict[int, float] = {
    2: 1.67,  # FBA Payroll Solutions Sp. z o.o.
}
_FGSP_BY_FIRMA: dict[int, float] = {
    2: 0.10,  # FBA Payroll Solutions Sp. z o.o.
}


def _normalize_kup_percent(value: float) -> float:
    """Normalize KUP to percent scale expected by business logic.

    Some Excel percentage values are read as fractions (e.g. 0.5 for 50%).
    Keep 0 as-is, convert non-zero values in [-1, 1] to *100.
    """
    if value == 0:
        return 0.0
    if -1.0 <= value <= 1.0:
        return value * 100.0
    return value


def _birthdate_from_pesel(pesel: object) -> Optional[date]:
    return _birthdate_from_pesel_util(pesel)


def _clarion_to_date(clarion_int: int) -> Optional[date]:
    try:
        return _CLARION_BASE_DATE + timedelta(days=int(clarion_int))
    except (ValueError, TypeError, OverflowError):
        return None


def _age_on(birth: date, on_date: date) -> int:
    return _age_on_util(birth, on_date)


def _resolve_pit_stawka(pesel: str, data_wyplaty_clarion: int) -> float:
    """Apply PL PIT-zero rule for under-26 employees, otherwise default 12%.

    If PESEL is unparseable or date conversion fails, fall back to default.
    """
    birth = _birthdate_from_pesel(pesel)
    payment_date = _clarion_to_date(data_wyplaty_clarion)
    if birth is None or payment_date is None:
        return PIT_DEFAULT_STAWKA
    age = _age_on(birth, payment_date)
    if age < PIT_ZERO_AGE_THRESHOLD:
        return 0.0
    return PIT_DEFAULT_STAWKA


def _is_female_from_pesel(pesel: object) -> Optional[bool]:
    return _is_female_from_pesel_util(pesel)


def _first_day_of_next_month(value: date) -> date:
    return _first_day_of_next_month_util(value)


def _is_fp_fgsp_age_exempt(pesel: str, data_wyplaty_clarion: int) -> bool:
    """Age exemption for FP/FGSP.

    Women: exempt from the month after turning 55.
    Men: exempt from the month after turning 60.

    Edge case: PESEL with birthday 29-Feb. If birth.year + 55/60 is not a leap
    year, date(...) would raise ValueError. Per ZUS practice, the anniversary
    in non-leap years is treated as 28-Feb (the last valid day of February).
    """
    birth = _birthdate_from_pesel(pesel)
    payment_date = _clarion_to_date(data_wyplaty_clarion)
    female = _is_female_from_pesel(pesel)
    if birth is None or payment_date is None or female is None:
        return False

    threshold_years = 55 if female else 60
    try:
        threshold_birthday = date(
            birth.year + threshold_years,
            birth.month,
            birth.day,
        )
    except ValueError:
        threshold_birthday = date(birth.year + threshold_years, 2, 28)
    exemption_starts = _first_day_of_next_month(threshold_birthday)
    return payment_date >= exemption_starts


def _is_student_umowa_case(
    kup_proc: float,
    emerytalne_proc: float,
    rentowe_u_proc: float,
    chorobowe_proc: float,
    zdrowotne_proc: float,
    fp_proc: float,
    fgsp_proc: float,
) -> bool:
    """Student case detection for UMOWY import: KUP == 0%.

    The PIT-zero under-26 exemption is applied only when the contract has
    KUP = 0%.  The age check is performed separately in
    _resolve_umowa_zlecenie_pit_stawka via _resolve_pit_stawka.
    Contracts with KUP > 0% (e.g. 20%) are never treated as student cases,
    regardless of age.
    """
    eps = 1e-9
    return abs(kup_proc) < eps


def _resolve_umowa_zlecenie_pit_stawka(
    pesel: str,
    data_wyplaty_clarion: int,
    is_student_case: bool,
) -> float:
    """Resolve PIT for zlecenie imports.

    PIT-zero under-26 exemption requires BOTH:
      * KUP == 0%  (is_student_case=True)
      * age < 26   (checked by _resolve_pit_stawka via PESEL)
    If either condition fails, the default 12% rate applies.
    """
    if not is_student_case:
        return PIT_DEFAULT_STAWKA
    return _resolve_pit_stawka(pesel, data_wyplaty_clarion)


from .financials import (
    calculate_umowa_financials as _calculate_umowa_financials,
    calculate_umowa_o_dzielo_financials as _calculate_umowa_o_dzielo_financials,
)

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from .config import DbConfig
from .preflight import PreflightReport
from .stats import (
    EmployeeAddressImportStats,
    EmployeeImportStats,
    ImportStats,
    PrzeprowadzkiImportStats,
    StatusUpdateStats,
    UbezpieczeniaImportStats,
    UmowaFieldDelta,
    UmowaVerificationIssue,
    UmowyImportStats,
    UmowyVerificationReport,
    UndoStats,
)
from .tax_calc_2026 import (
    DB_TO_RECALC_FIELD_MAP,
    STANDARD_RATES_2026,
    recalculate_umowa_dzielo_from_rates,
    recalculate_umowa_zlecenie_from_rates,
)


def _umowy_export_append_totals_row(df: pd.DataFrame) -> pd.DataFrame:
    """Dodaje wiersz „Razem”: sumuje kwoty PLN brutto/netto/składki; puste dla [%] i dat."""
    if df.empty:
        return df
    total_row: dict[str, Any] = {}
    for col in df.columns:
        if col == "Imię":
            total_row[col] = "Razem"
        elif col in ("Nazwisko", "Data wypłaty", "Rodzaj umowy", "Numer rachunku"):
            total_row[col] = ""
        elif "[%]" in col:
            total_row[col] = ""
        else:
            ser = pd.to_numeric(df[col], errors="coerce")
            total_row[col] = round(float(ser.sum()), 2)
    return pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)


# PESEL: wykluczeni z głównego eksportu UZ/UD; ten sam zestaw — osobny eksport UZ+UD w jednym pliku.
_UMOWY_EXPORT_EXCLUDE_PESEL: frozenset[str] = frozenset(
    {
        "91100715534",
        "83122516550",
        "92120610340",
        "95040812384",
        "00312509293",
        "99120910999",
        "06241913956",
        "01292713197",
        "02291110725",
        "96061610966",
        "87051219285",
        "93033116282",
        "00232411788",
        "98110809693",
        "01290412340",
        "96102414146",
        "05252110004",
        "00323009021",
        "97101511865",
        "97101209425",
        "84080623580",
        "01221411785",
        "94040717927",
        "02312809809",
        "95060312008",
        "02240312949",
        "91120515181",
        "89053115620",
        "94020511925",
        "05271212846",
        "91112413530",
        "00222009968",
        "98040213209",
        "96052113405",
    }
)


_UMOWY_EXPORT_ZLECENIE_SKLADKI_SQL = """
                , CAST(ISNULL(u.EMERYTALNE____, 0) AS FLOAT) AS pct_emerytalne
                , CAST(ISNULL(u.RENTOWE_U____, 0) AS FLOAT) AS pct_rentowe_u
                , CAST(ISNULL(u.RENTOWE____, 0) AS FLOAT) AS pct_rentowe_p
                , CAST(ISNULL(u.CHOROBOWE____, 0) AS FLOAT) AS pct_chorobowe
                , CAST(ISNULL(u.WYPADKOWE____, 0) AS FLOAT) AS pct_wypadkowe
                , CAST(ISNULL(u.ZDROWOTNE____, 0) AS FLOAT) AS pct_zdrowotne
                , CAST(ISNULL(u.FP____, 0) AS FLOAT) AS pct_fp
                , CAST(ISNULL(u.FGSP____, 0) AS FLOAT) AS pct_fgsp
                , CAST(ISNULL(u.EMERYTALNE_ZLECENIOBIORCA, 0) AS FLOAT) AS kw_em_zleceniobiorca
                , CAST(ISNULL(u.RENTOWE_ZLECENIOBIORCA, 0) AS FLOAT) AS kw_rent_zleceniobiorca
                , CAST(ISNULL(u.CHOROBOWE_ZLECENIOBIORCA, 0) AS FLOAT) AS kw_chorob_zleceniobiorca
                , CAST(ISNULL(u.ZDROWOTNE_ZLECENIOBIORCA, 0) AS FLOAT) AS kw_zdrow_zleceniobiorca
                , CAST(ISNULL(u.EMERYTALNE_ZLECENIODAWCA, 0) AS FLOAT) AS kw_em_zleceniodawca
                , CAST(ISNULL(u.RENTOWE_ZLECENIODAWCA, 0) AS FLOAT) AS kw_rent_zleceniodawca
                , CAST(ISNULL(u.WYPADKOWE_ZLECENIODAWCA, 0) AS FLOAT) AS kw_wypad_zleceniodawca
                , CAST(ISNULL(u.FP, 0) AS FLOAT) AS kw_fp
                , CAST(ISNULL(u.FGSP, 0) AS FLOAT) AS kw_fgsp
                , CAST(ISNULL(u.PODSTAWOWA_UCZESTNIKA_PPK, 0) AS FLOAT) AS kw_ppk_podstawowa
                , (
                      CAST(ISNULL(u.EMERYTALNE_ZLECENIOBIORCA, 0) AS FLOAT)
                    + CAST(ISNULL(u.RENTOWE_ZLECENIOBIORCA, 0) AS FLOAT)
                    + CAST(ISNULL(u.CHOROBOWE_ZLECENIOBIORCA, 0) AS FLOAT)
                    + CAST(ISNULL(u.ZDROWOTNE_ZLECENIOBIORCA, 0) AS FLOAT)
                  ) AS zus_zleceniobiorca_kw
                , (
                      CAST(ISNULL(u.EMERYTALNE_ZLECENIODAWCA, 0) AS FLOAT)
                    + CAST(ISNULL(u.RENTOWE_ZLECENIODAWCA, 0) AS FLOAT)
                    + CAST(ISNULL(u.WYPADKOWE_ZLECENIODAWCA, 0) AS FLOAT)
                    + CAST(ISNULL(u.FP, 0) AS FLOAT)
                    + CAST(ISNULL(u.FGSP, 0) AS FLOAT)
                  ) AS zus_zleceniodawca_kw
            """

_UMOWY_EXPORT_ZLECENIE_COLUMN_SPEC: list[tuple[str, str]] = [
    ("pct_emerytalne", "Skł.na ub.emerytal.[%]"),
    ("pct_rentowe_u", "Składka ub.rent. U [%]"),
    ("pct_rentowe_p", "Składka ub.rent. P [%]"),
    ("pct_chorobowe", "Składka ub.chorob.[%]"),
    ("pct_wypadkowe", "Składka ub.wypadk.[%]"),
    ("pct_zdrowotne", "Składka ub.zdrowotne[%]"),
    ("pct_fp", "FP [%]"),
    ("pct_fgsp", "FGŚP [%]"),
    ("zus_zleceniobiorca_kw", "ZUS zleceniobiorcy (razem PLN)"),
    ("zus_zleceniodawca_kw", "ZUS zleceniodawcy (razem PLN)"),
    ("kw_em_zleceniobiorca", "Emerytalna zleceniobiorcy (PLN)"),
    ("kw_rent_zleceniobiorca", "Rentowa zleceniobiorcy (PLN)"),
    ("kw_chorob_zleceniobiorca", "Chorobowa zleceniobiorcy (PLN)"),
    ("kw_zdrow_zleceniobiorca", "Zdrowotna zleceniobiorcy (PLN)"),
    ("kw_em_zleceniodawca", "Emerytalna zleceniodawcy (PLN)"),
    ("kw_rent_zleceniodawca", "Rentowa zleceniodawcy (PLN)"),
    ("kw_wypad_zleceniodawca", "Wypadkowa zleceniodawcy (PLN)"),
    ("kw_fp", "FP (PLN)"),
    ("kw_fgsp", "FGŚP (PLN)"),
    ("kw_ppk_podstawowa", "PPK podstawowa uczestnika (PLN)"),
]


def _umowy_export_date_filter_sql(
    params: dict[str, Any],
    rok_wyplaty: Optional[int],
    miesiac_wyplaty: Optional[int],
) -> str:
    frag = ""
    if rok_wyplaty is not None and int(rok_wyplaty) > 0:
        frag += """
              AND YEAR(DATEADD(day, CAST(u.DATA_WYPLATY AS int), CAST('1800-12-28' AS DATE))) = :rok
            """
        params["rok"] = int(rok_wyplaty)
    if miesiac_wyplaty is not None and 1 <= int(miesiac_wyplaty) <= 12:
        frag += """
              AND MONTH(DATEADD(day, CAST(u.DATA_WYPLATY AS int), CAST('1800-12-28' AS DATE))) = :miesiac
            """
        params["miesiac"] = int(miesiac_wyplaty)
    return frag


def _umowy_sidelist_pe_in_sql(params: dict[str, Any]) -> str:
    """Fragment `AND PESEL IN (...)` + bindy `:sid_pesel_*` wg `_UMOWY_EXPORT_EXCLUDE_PESEL`."""
    pesels_sorted = sorted(_UMOWY_EXPORT_EXCLUDE_PESEL)
    for i, pe in enumerate(pesels_sorted):
        params[f"sid_pesel_{i}"] = pe
    return (
        " AND LTRIM(RTRIM(ISNULL(p.PESEL, ''))) IN ("
        + ", ".join(f":sid_pesel_{j}" for j in range(len(pesels_sorted)))
        + ") "
    )


def _umowy_export_format_payment_date_column(df: pd.DataFrame) -> None:
    """`data_wyplaty_clarion` → `Data wypłaty` (dd/mm/yyyy), usuwa kolumnę źródłową."""

    def _fmt_clarion(val: object) -> str:
        if val is None:
            return ""
        try:
            if pd.isna(val):
                return ""
        except (TypeError, ValueError):
            pass
        try:
            d = _clarion_to_date(int(float(val)))
        except (TypeError, ValueError):
            return ""
        return d.strftime("%d/%m/%Y") if d else ""

    df["Data wypłaty"] = df["data_wyplaty_clarion"].map(_fmt_clarion)
    df.drop(columns=["data_wyplaty_clarion"], inplace=True)


class DatabaseService:
    def __init__(self, config: DbConfig) -> None:
        self.config = config
        self._engine: Optional[Engine] = None

    def build_connection_string(self) -> str:
        trusted = "yes" if self.config.trusted_connection else "no"
        params = [
            f"DRIVER={{{self.config.driver}}}",
            f"SERVER={self.config.server}",
            f"DATABASE={self.config.database}",
        ]

        if self.config.trusted_connection:
            params.append(f"Trusted_Connection={trusted}")
        else:
            params.append(f"UID={self.config.username}")
            params.append(f"PWD={self.config.password}")

        return "mssql+pyodbc:///?odbc_connect=" + quote_plus(";".join(params))

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(self.build_connection_string(), future=True)
        return self._engine

    def test_connection(self) -> tuple[bool, str]:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True, "Polaczenie z baza SQL dziala."
        except SQLAlchemyError as exc:
            return False, f"Blad polaczenia: {exc}"

    def preflight_check(self) -> PreflightReport:
        report = PreflightReport()
        required_tables = (
            "PRACOWNIK",
            "URZEDY",
            "URZEDY_PRACOWNIKA",
            "ADRESY_PRACOWNIKA",
            "GANG_UMOWY_CYWILNO_PRAWNE",
            "GANG_UBEZPIECZENIA_OBOWIAZKOWE",
        )
        sql_list = ", ".join(f"'{name}'" for name in required_tables)

        with self.engine.connect() as connection:
            existing_rows = connection.execute(
                text(
                    f"""
                    SELECT t.name
                    FROM sys.tables t
                    WHERE t.name IN ({sql_list})
                    """
                )
            ).all()
            existing = {str(row[0]) for row in existing_rows}
            report.missing_tables = [name for name in required_tables if name not in existing]

            perm_targets = (
                ("PRACOWNIK", "SELECT"),
                ("PRACOWNIK", "UPDATE"),
                ("URZEDY", "SELECT"),
                ("URZEDY", "INSERT"),
                ("URZEDY_PRACOWNIKA", "SELECT"),
                ("URZEDY_PRACOWNIKA", "INSERT"),
                ("ADRESY_PRACOWNIKA", "SELECT"),
                ("ADRESY_PRACOWNIKA", "INSERT"),
                ("GANG_UMOWY_CYWILNO_PRAWNE", "SELECT"),
                ("GANG_UMOWY_CYWILNO_PRAWNE", "INSERT"),
                ("GANG_UBEZPIECZENIA_OBOWIAZKOWE", "SELECT"),
                ("GANG_UBEZPIECZENIA_OBOWIAZKOWE", "INSERT"),
            )
            for table_name, permission in perm_targets:
                has_perm = connection.execute(
                    text(
                        """
                        SELECT HAS_PERMS_BY_NAME(:obj_name, 'OBJECT', :perm_name)
                        """
                    ),
                    {"obj_name": f"dbo.{table_name}", "perm_name": permission},
                ).scalar()
                if int(has_perm or 0) == 0:
                    report.permission_warnings.append(
                        f"Нет права {permission} для dbo.{table_name}"
                    )
        return report

    def urzad_exists(self, kod_us: str) -> bool:
        query = text(
            """
            SELECT 1
            FROM URZEDY
            WHERE KOD_US = :kod_us
            """
        )
        with self.engine.connect() as connection:
            return connection.execute(query, {"kod_us": kod_us}).first() is not None

    def employee_exists(self, nr_ewidencyjny: str) -> bool:
        query = text(
            """
            SELECT 1
            FROM PRACOWNIK
            WHERE NR_EWIDENCYJNY = :nr_ewidencyjny
            """
        )
        with self.engine.connect() as connection:
            return (
                connection.execute(query, {"nr_ewidencyjny": nr_ewidencyjny}).first()
                is not None
            )

    def employee_id_by_nr(
        self,
        nr_ewidencyjny: str,
        connection: Connection | None = None,
        id_firmy: int | None = None,
    ) -> Optional[int]:
        firm_clause = " AND ID_FIRMY = :id_firmy" if id_firmy is not None else ""
        query = text(
            f"""
            SELECT TOP 1 ID_PRACOWNIKA
            FROM PRACOWNIK
            WHERE NR_EWIDENCYJNY = :nr_ewidencyjny{firm_clause}
            ORDER BY ID_PRACOWNIKA
            """
        )
        params: dict = {"nr_ewidencyjny": nr_ewidencyjny}
        if id_firmy is not None:
            params["id_firmy"] = id_firmy
        if connection is None:
            with self.engine.connect() as conn:
                row = conn.execute(query, params).first()
        else:
            row = connection.execute(query, params).first()
        return int(row[0]) if row else None

    def employee_id_by_pesel(
        self,
        pesel: str,
        connection: Connection | None = None,
        id_firmy: int | None = None,
    ) -> Optional[int]:
        firm_clause = " AND ID_FIRMY = :id_firmy" if id_firmy is not None else ""
        query = text(
            f"""
            SELECT TOP 1 ID_PRACOWNIKA
            FROM PRACOWNIK
            WHERE PESEL = :pesel{firm_clause}
            ORDER BY ID_PRACOWNIKA
            """
        )
        params: dict = {"pesel": pesel}
        if id_firmy is not None:
            params["id_firmy"] = id_firmy
        if connection is None:
            with self.engine.connect() as conn:
                row = conn.execute(query, params).first()
        else:
            row = connection.execute(query, params).first()
        return int(row[0]) if row else None

    def pesel_by_employee_id(
        self, employee_id: int, connection: Connection | None = None
    ) -> Optional[str]:
        query = text(
            """
            SELECT TOP 1 PESEL
            FROM PRACOWNIK
            WHERE ID_PRACOWNIKA = :id_pracownika
            """
        )
        if connection is None:
            with self.engine.connect() as conn:
                row = conn.execute(query, {"id_pracownika": int(employee_id)}).first()
        else:
            row = connection.execute(query, {"id_pracownika": int(employee_id)}).first()
        if row is None or row[0] is None:
            return None
        return str(row[0]).strip() or None

    def firma_id_by_pesel(
        self,
        pesel: str,
        connection: Connection | None = None,
    ) -> Optional[int]:
        """Return ID_FIRMY from PRACOWNIK for the given PESEL.

        Used during umowy zlecenie import to select per-company employer rates
        (wypadkowe, FGSP).  Returns None when the employee is not found.
        """
        query = text(
            """
            SELECT TOP 1 ID_FIRMY
            FROM PRACOWNIK
            WHERE LTRIM(RTRIM(ISNULL(CAST(PESEL AS VARCHAR(50)), ''))) = :pesel
            """
        )
        if connection is None:
            with self.engine.connect() as conn:
                row = conn.execute(query, {"pesel": pesel}).first()
        else:
            row = connection.execute(query, {"pesel": pesel}).first()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def urzad_id_by_kod(self, kod_us: str) -> Optional[int]:
        query = text(
            """
            SELECT TOP 1 ID_URZEDU
            FROM URZEDY
            WHERE KOD_US = :kod_us
            ORDER BY ID_URZEDU
            """
        )
        with self.engine.connect() as connection:
            row = connection.execute(query, {"kod_us": kod_us}).first()
            return int(row[0]) if row else None

    def urzad_exists_by_name(self, urzad_name: str) -> bool:
        return self.find_urzad_id_by_name(urzad_name) is not None

    def find_urzad_id_by_name(self, urzad_name: str) -> int | None:
        with self.engine.connect() as connection:
            return self._find_urzad_id_by_name_connection(connection, urzad_name)

    def link_exists(self, employee_id: int, urzad_id: int, data_od: int) -> bool:
        query = text(
            """
            SELECT 1
            FROM URZEDY_PRACOWNIKA
            WHERE ID_PRACOWNIKA = :employee_id
              AND ID_URZEDU = :urzad_id
              AND DATA_OD = :data_od
            """
        )
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    query,
                    {
                        "employee_id": employee_id,
                        "urzad_id": urzad_id,
                        "data_od": data_od,
                    },
                ).first()
                is not None
            )

    def umowa_exists(
        self,
        employee_id: int,
        numer_umowy: str,
        data_umowy: int,
        data_wyplaty: int | None = None,
        numer_rachunku: str | None = None,
    ) -> bool:
        where_extra = ""
        params: dict[str, object] = {
            "employee_id": employee_id,
            "numer_umowy": numer_umowy,
            "data_umowy": data_umowy,
        }
        if data_wyplaty is not None:
            where_extra += " AND DATA_WYPLATY = :data_wyplaty"
            params["data_wyplaty"] = int(data_wyplaty)
        if numer_rachunku is not None:
            where_extra += " AND NUMER_RACHUNKU = :numer_rachunku"
            params["numer_rachunku"] = str(numer_rachunku).strip()

        query = text(
            f"""
            SELECT 1
            FROM GANG_UMOWY_CYWILNO_PRAWNE
            WHERE ID_NADRZEDNEGO = :employee_id
              AND NUMER_UMOWY = :numer_umowy
              AND DATA_UMOWY = :data_umowy
              {where_extra}
            """
        )
        with self.engine.connect() as connection:
            return connection.execute(query, params).first() is not None

    def umowa_numbers_for_year(self, employee_id: int, year: int) -> set[str]:
        query = text(
            """
            SELECT DISTINCT LTRIM(RTRIM(NUMER_UMOWY)) AS NUMER_UMOWY
            FROM GANG_UMOWY_CYWILNO_PRAWNE
            WHERE ID_NADRZEDNEGO = :employee_id
              AND YEAR(DATEADD(day, DATA_UMOWY, CAST('1800-12-28' AS date))) = :year
              AND NUMER_UMOWY IS NOT NULL
              AND LTRIM(RTRIM(NUMER_UMOWY)) <> ''
            """
        )
        with self.engine.connect() as connection:
            rows = connection.execute(
                query,
                {"employee_id": employee_id, "year": int(year)},
            ).fetchall()
            return {str(row[0]).strip() for row in rows if row[0] is not None}

    def count_existing_nr_rachunki(
        self, nr_rachunki: list[str]
    ) -> tuple[int, list[str]]:
        """Check how many NUMER_RACHUNKU values already exist in GANG_UMOWY_CYWILNO_PRAWNE.

        Returns (count, list_of_found_numbers).
        Used for pre-import duplicate detection.
        """
        if not nr_rachunki:
            return 0, []
        # Normalise + de-duplicate
        clean = list({str(r).strip() for r in nr_rachunki if r})
        if not clean:
            return 0, []
        # SQL Server parameterised IN clause
        params: dict[str, str] = {f"r{i}": v for i, v in enumerate(clean)}
        placeholders = ", ".join(f":r{i}" for i in range(len(clean)))
        query = text(
            f"""
            SELECT LTRIM(RTRIM(NUMER_RACHUNKU)) AS nr
            FROM GANG_UMOWY_CYWILNO_PRAWNE
            WHERE LTRIM(RTRIM(NUMER_RACHUNKU)) IN ({placeholders})
            """
        )
        with self.engine.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        found = [str(r[0]).strip() for r in rows if r[0]]
        return len(found), found

    def umowy_export_dataframe(
        self,
        rodzaj_umowy: int,
        *,
        rok_wyplaty: Optional[int] = None,
        miesiac_wyplaty: Optional[int] = None,
    ) -> pd.DataFrame:
        """Eksport UMOWY → Excel: dane pracownika, kwoty i data wypłaty; dla UZ (+ składki).

        Parametry
        ---------
        rodzaj_umowy:
            `1` — umowa zlecenie (UZ), `2` — umowa o dzieło (UD), zgodnie z `RODZAJ_UMOWY` w BD.
        rok_wyplaty:
            Opcjonalny filtr roku wg `DATA_WYPLATY`; `None` lub `0` = bez filtra roku.
        miesiac_wyplaty:
            Opcjonalny miesiąc (1–12) wg `DATA_WYPLATY`; `None` lub `0` = bez filtra miesiąca.
            Można łączyć z rokiem lub używać samego miesiąca (wszystkie lata).
        Pracownicy z PESEL w `_UMOWY_EXPORT_EXCLUDE_PESEL` nie są zwracani (UZ ani UD).
        """
        if rodzaj_umowy not in (1, 2):
            raise ValueError("rodzaj_umowy must be 1 (zlecenie) or 2 (o dzieło)")

        skladki_select = _UMOWY_EXPORT_ZLECENIE_SKLADKI_SQL if rodzaj_umowy == 1 else ""

        params: dict[str, Any] = {"rodzaj": int(rodzaj_umowy)}
        date_frag = _umowy_export_date_filter_sql(params, rok_wyplaty, miesiac_wyplaty)

        exclude_frag = ""
        pesels_sorted = sorted(_UMOWY_EXPORT_EXCLUDE_PESEL)
        if pesels_sorted:
            exclude_frag = (
                " AND LTRIM(RTRIM(ISNULL(p.PESEL, ''))) NOT IN ("
                + ", ".join(f":exc_pesel_{i}" for i in range(len(pesels_sorted)))
                + ") "
            )
            for i, pe in enumerate(pesels_sorted):
                params[f"exc_pesel_{i}"] = pe

        sql = (
            """
            SELECT
                  LTRIM(RTRIM(ISNULL(p.IMIE_1, ''))) AS imie
                , LTRIM(RTRIM(ISNULL(p.NAZWISKO, ''))) AS nazwisko
                , LTRIM(RTRIM(ISNULL(u.NUMER_RACHUNKU, ''))) AS numer_rachunku
                , CAST(ISNULL(u.KWOTA_DO_WYPLATY, 0) AS FLOAT) AS kwota_netto
                , CAST(ISNULL(u.WYNAGRODZENIE_BRUTTO, 0) AS FLOAT) AS kwota_brutto
                , u.DATA_WYPLATY AS data_wyplaty_clarion
            """
            + skladki_select
            + """
            FROM GANG_UMOWY_CYWILNO_PRAWNE u
            INNER JOIN PRACOWNIK p ON p.ID_PRACOWNIKA = u.ID_NADRZEDNEGO
            WHERE TRY_CAST(LTRIM(RTRIM(ISNULL(CAST(u.RODZAJ_UMOWY AS VARCHAR(40)), ''))) AS INT) = :rodzaj
            """
            + date_frag
            + exclude_frag
            + """
            ORDER BY u.DATA_WYPLATY, p.NAZWISKO, p.IMIE_1, u.NUMER_RACHUNKU
            """
        )

        with self.engine.connect() as connection:
            df = pd.read_sql(text(sql), connection, params=params)

        if df.empty:
            return df

        _umowy_export_format_payment_date_column(df)

        rename_common: dict[str, str] = {
            "imie": "Imię",
            "nazwisko": "Nazwisko",
            "numer_rachunku": "Numer rachunku",
            "kwota_netto": "Kwota netto / do wypłaty",
            "kwota_brutto": "Kwota brutto",
        }
        rename_zlecenie = dict(_UMOWY_EXPORT_ZLECENIE_COLUMN_SPEC)
        rename_map = {**rename_common, **(rename_zlecenie if rodzaj_umowy == 1 else {})}
        df.rename(columns=rename_map, inplace=True)

        head = [
            "Imię",
            "Nazwisko",
            "Numer rachunku",
            "Kwota netto / do wypłaty",
            "Kwota brutto",
            "Data wypłaty",
        ]
        tail = [label for _, label in _UMOWY_EXPORT_ZLECENIE_COLUMN_SPEC] if rodzaj_umowy == 1 else []
        ordered = [c for c in head + tail if c in df.columns]
        out = df[ordered]
        return _umowy_export_append_totals_row(out)

    def umowy_export_sidelist_both_types_dataframe(
        self,
        *,
        rok_wyplaty: Optional[int] = None,
        miesiac_wyplaty: Optional[int] = None,
    ) -> pd.DataFrame:
        """UZ i UD w jednym arkuszu — tylko pracownicy z PESEL z `_UMOWY_EXPORT_EXCLUDE_PESEL`.

        Ta sama lista PESEL co przy wykluczeniu z głównego eksportu. Kolumny: Imię, Nazwisko,
        rodzaj umowy (etykieta), kwota netto, kwota brutto; wiersz „Razem” jak w głównym eksporcie.
        """
        if not _UMOWY_EXPORT_EXCLUDE_PESEL:
            return pd.DataFrame()

        params: dict[str, Any] = {}
        include_frag = _umowy_sidelist_pe_in_sql(params)
        date_frag = _umowy_export_date_filter_sql(params, rok_wyplaty, miesiac_wyplaty)

        sql = (
            """
            SELECT
                  LTRIM(RTRIM(ISNULL(p.IMIE_1, ''))) AS imie
                , LTRIM(RTRIM(ISNULL(p.NAZWISKO, ''))) AS nazwisko
                , LTRIM(RTRIM(ISNULL(u.NUMER_RACHUNKU, ''))) AS numer_rachunku
                , TRY_CAST(LTRIM(RTRIM(ISNULL(CAST(u.RODZAJ_UMOWY AS VARCHAR(40)), ''))) AS INT) AS rodzaj_umowy
                , CAST(ISNULL(u.KWOTA_DO_WYPLATY, 0) AS FLOAT) AS kwota_netto
                , CAST(ISNULL(u.WYNAGRODZENIE_BRUTTO, 0) AS FLOAT) AS kwota_brutto
            FROM GANG_UMOWY_CYWILNO_PRAWNE u
            INNER JOIN PRACOWNIK p ON p.ID_PRACOWNIKA = u.ID_NADRZEDNEGO
            WHERE TRY_CAST(LTRIM(RTRIM(ISNULL(CAST(u.RODZAJ_UMOWY AS VARCHAR(40)), ''))) AS INT) IN (1, 2)
            """
            + date_frag
            + include_frag
            + """
            ORDER BY p.NAZWISKO, p.IMIE_1, rodzaj_umowy, u.DATA_WYPLATY, u.NUMER_RACHUNKU
            """
        )

        with self.engine.connect() as connection:
            df = pd.read_sql(text(sql), connection, params=params)

        if df.empty:
            return df

        _rodzaj_label: dict[int, str] = {
            1: "UZ — umowa zlecenie",
            2: "UD — umowa o dzieło",
        }

        def _lbl(val: object) -> str:
            if val is None:
                return ""
            try:
                if pd.isna(val):
                    return ""
            except (TypeError, ValueError):
                pass
            try:
                k = int(float(val))
            except (TypeError, ValueError):
                return ""
            return _rodzaj_label.get(k, str(k))

        df["Rodzaj umowy"] = df["rodzaj_umowy"].map(_lbl)
        df.drop(columns=["rodzaj_umowy"], inplace=True)
        df.rename(
            columns={
                "imie": "Imię",
                "nazwisko": "Nazwisko",
                "numer_rachunku": "Numer rachunku",
                "kwota_netto": "Kwota netto / do wypłaty",
                "kwota_brutto": "Kwota brutto",
            },
            inplace=True,
        )
        ordered = [
            "Imię",
            "Nazwisko",
            "Numer rachunku",
            "Rodzaj umowy",
            "Kwota netto / do wypłaty",
            "Kwota brutto",
        ]
        return _umowy_export_append_totals_row(df[[c for c in ordered if c in df.columns]])

    def umowy_export_sidelist_zlecenie_dataframe(
        self,
        *,
        rok_wyplaty: Optional[int] = None,
        miesiac_wyplaty: Optional[int] = None,
    ) -> pd.DataFrame:
        """Tylko UZ (1) dla PESEL z `_UMOWY_EXPORT_EXCLUDE_PESEL` — jak główny eksport UZ (składki + data)."""
        if not _UMOWY_EXPORT_EXCLUDE_PESEL:
            return pd.DataFrame()

        params: dict[str, Any] = {"rodzaj": 1}
        include_frag = _umowy_sidelist_pe_in_sql(params)
        date_frag = _umowy_export_date_filter_sql(params, rok_wyplaty, miesiac_wyplaty)

        sql = (
            """
            SELECT
                  LTRIM(RTRIM(ISNULL(p.IMIE_1, ''))) AS imie
                , LTRIM(RTRIM(ISNULL(p.NAZWISKO, ''))) AS nazwisko
                , LTRIM(RTRIM(ISNULL(u.NUMER_RACHUNKU, ''))) AS numer_rachunku
                , CAST(ISNULL(u.KWOTA_DO_WYPLATY, 0) AS FLOAT) AS kwota_netto
                , CAST(ISNULL(u.WYNAGRODZENIE_BRUTTO, 0) AS FLOAT) AS kwota_brutto
                , u.DATA_WYPLATY AS data_wyplaty_clarion
            """
            + _UMOWY_EXPORT_ZLECENIE_SKLADKI_SQL
            + """
            FROM GANG_UMOWY_CYWILNO_PRAWNE u
            INNER JOIN PRACOWNIK p ON p.ID_PRACOWNIKA = u.ID_NADRZEDNEGO
            WHERE TRY_CAST(LTRIM(RTRIM(ISNULL(CAST(u.RODZAJ_UMOWY AS VARCHAR(40)), ''))) AS INT) = :rodzaj
            """
            + date_frag
            + include_frag
            + """
            ORDER BY u.DATA_WYPLATY, p.NAZWISKO, p.IMIE_1, u.NUMER_RACHUNKU
            """
        )

        with self.engine.connect() as connection:
            df = pd.read_sql(text(sql), connection, params=params)

        if df.empty:
            return df

        _umowy_export_format_payment_date_column(df)

        rename_map = {
            "imie": "Imię",
            "nazwisko": "Nazwisko",
            "numer_rachunku": "Numer rachunku",
            "kwota_netto": "Kwota netto / do wypłaty",
            "kwota_brutto": "Kwota brutto",
            **dict(_UMOWY_EXPORT_ZLECENIE_COLUMN_SPEC),
        }
        df.rename(columns=rename_map, inplace=True)

        head = [
            "Imię",
            "Nazwisko",
            "Numer rachunku",
            "Kwota netto / do wypłaty",
            "Kwota brutto",
            "Data wypłaty",
        ]
        tail = [label for _, label in _UMOWY_EXPORT_ZLECENIE_COLUMN_SPEC]
        ordered = [c for c in head + tail if c in df.columns]
        return _umowy_export_append_totals_row(df[ordered])

    def umowy_export_sidelist_dzielo_dataframe(
        self,
        *,
        rok_wyplaty: Optional[int] = None,
        miesiac_wyplaty: Optional[int] = None,
    ) -> pd.DataFrame:
        """Tylko UD (2) dla PESEL z `_UMOWY_EXPORT_EXCLUDE_PESEL` — Imię, Nazwisko, netto, brutto, data."""
        if not _UMOWY_EXPORT_EXCLUDE_PESEL:
            return pd.DataFrame()

        params: dict[str, Any] = {"rodzaj": 2}
        include_frag = _umowy_sidelist_pe_in_sql(params)
        date_frag = _umowy_export_date_filter_sql(params, rok_wyplaty, miesiac_wyplaty)

        sql = (
            """
            SELECT
                  LTRIM(RTRIM(ISNULL(p.IMIE_1, ''))) AS imie
                , LTRIM(RTRIM(ISNULL(p.NAZWISKO, ''))) AS nazwisko
                , LTRIM(RTRIM(ISNULL(u.NUMER_RACHUNKU, ''))) AS numer_rachunku
                , CAST(ISNULL(u.KWOTA_DO_WYPLATY, 0) AS FLOAT) AS kwota_netto
                , CAST(ISNULL(u.WYNAGRODZENIE_BRUTTO, 0) AS FLOAT) AS kwota_brutto
                , u.DATA_WYPLATY AS data_wyplaty_clarion
            FROM GANG_UMOWY_CYWILNO_PRAWNE u
            INNER JOIN PRACOWNIK p ON p.ID_PRACOWNIKA = u.ID_NADRZEDNEGO
            WHERE TRY_CAST(LTRIM(RTRIM(ISNULL(CAST(u.RODZAJ_UMOWY AS VARCHAR(40)), ''))) AS INT) = :rodzaj
            """
            + date_frag
            + include_frag
            + """
            ORDER BY u.DATA_WYPLATY, p.NAZWISKO, p.IMIE_1, u.NUMER_RACHUNKU
            """
        )

        with self.engine.connect() as connection:
            df = pd.read_sql(text(sql), connection, params=params)

        if df.empty:
            return df

        _umowy_export_format_payment_date_column(df)
        df.rename(
            columns={
                "imie": "Imię",
                "nazwisko": "Nazwisko",
                "numer_rachunku": "Numer rachunku",
                "kwota_netto": "Kwota netto / do wypłaty",
                "kwota_brutto": "Kwota brutto",
            },
            inplace=True,
        )
        ordered = [
            "Imię",
            "Nazwisko",
            "Numer rachunku",
            "Kwota netto / do wypłaty",
            "Kwota brutto",
            "Data wypłaty",
        ]
        return _umowy_export_append_totals_row(df[[c for c in ordered if c in df.columns]])

    def umowa_type_one_exists(
        self,
        employee_id: int,
        numer_umowy: str,
        connection: Connection | None = None,
    ) -> bool:
        query = text(
            """
            SELECT 1
            FROM GANG_UMOWY_CYWILNO_PRAWNE
            WHERE ID_NADRZEDNEGO = :employee_id
              AND NUMER_UMOWY = :numer_umowy
              AND TRY_CAST(RODZAJ_UMOWY AS int) = 1
            """
        )
        if connection is None:
            with self.engine.connect() as conn:
                row = conn.execute(
                    query,
                    {"employee_id": employee_id, "numer_umowy": numer_umowy},
                ).first()
                return row is not None
        row = connection.execute(
            query,
            {"employee_id": employee_id, "numer_umowy": numer_umowy},
        ).first()
        return row is not None

    def obowiazkowe_ubezpieczenie_exists(
        self,
        employee_id: int,
        data_od: int,
        data_obowiazku: int,
        emerytalne: int,
        rentowe: int,
        wypadkowe: int,
        chorobowe: int,
        connection: Connection | None = None,
    ) -> bool:
        query = text(
            """
            SELECT 1
            FROM GANG_UBEZPIECZENIA_OBOWIAZKOWE
            WHERE ID_NADRZEDNEGO = :employee_id
              AND DATA_OD = :data_od
              AND DATA_OBOWIAZKU = :data_obowiazku
              AND EMERYTALNE = :emerytalne
              AND RENTOWE = :rentowe
              AND WYPADKOWE = :wypadkowe
              AND CHOROBOWE = :chorobowe
            """
        )
        params = {
            "employee_id": employee_id,
            "data_od": data_od,
            "data_obowiazku": data_obowiazku,
            "emerytalne": emerytalne,
            "rentowe": rentowe,
            "wypadkowe": wypadkowe,
            "chorobowe": chorobowe,
        }
        if connection is None:
            with self.engine.connect() as conn:
                row = conn.execute(query, params).first()
                return row is not None
        row = connection.execute(query, params).first()
        return row is not None

    def obowiazkowe_ubezpieczenie_exists_for_year(
        self,
        employee_id: int,
        year: int,
        connection: Connection | None = None,
    ) -> bool:
        """Czy pracownik ma już zapis GANG_UBEZPIECZENIA_OBOWIAZKOWE z DATA_OBOWIAZKU w danym roku."""
        query = text(
            """
            SELECT 1
            FROM GANG_UBEZPIECZENIA_OBOWIAZKOWE
            WHERE ID_NADRZEDNEGO = :employee_id
              AND YEAR(DATEADD(day, DATA_OBOWIAZKU, CAST('1800-12-28' AS date))) = :year
            """
        )
        params = {"employee_id": employee_id, "year": int(year)}
        if connection is None:
            with self.engine.connect() as conn:
                return conn.execute(query, params).first() is not None
        return connection.execute(query, params).first() is not None

    def address_exists_on_date(self, employee_id: int, data_od: int) -> bool:
        query = text(
            """
            SELECT 1
            FROM ADRESY_PRACOWNIKA
            WHERE ID_PRACOWNIKA = :employee_id
              AND DATA_OD = :data_od
            """
        )
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    query,
                    {"employee_id": employee_id, "data_od": data_od},
                ).first()
                is not None
            )

    def execute_import(
        self,
        rows: list[dict],
        start_urzad_id: int,
        data_od: int,
        progress_callback: ProgressCallback | None = None,
        cancel_token: CancelToken | None = None,
    ) -> ImportStats:
        if not rows:
            return ImportStats()

        stats = ImportStats()
        total = len(rows)
        _notify_progress(progress_callback, 0, total)
        with self.engine.begin() as connection:
            used_urzedy_ids = self._load_used_ints_locked(
                connection=connection,
                table_name="URZEDY",
                column_name="ID_URZEDU",
                min_value=start_urzad_id,
            )
            used_link_ids = self._load_used_ints_locked(
                connection=connection,
                table_name="URZEDY_PRACOWNIKA",
                column_name="IDENTYFIKATOR",
            )
            urzad_cache = self._load_urzedy_cache(connection)
            urzad_code_index = self._build_urzad_code_index(connection)

            for idx, row in enumerate(rows, start=1):
                _raise_if_cancelled(cancel_token)
                employee_id = int(row["employee_id"])
                kod_us = str(row["kod_us"]).strip()
                nazwa = str(row["nazwa"]).strip()
                row_data_od = int(row.get("data_od", data_od))

                urzad_id, urzad_created = self._resolve_or_create_urzad(
                    connection=connection,
                    urzad_name=nazwa,
                    urzad_code=kod_us,
                    used_urzedy_ids=used_urzedy_ids,
                    start_urzad_id=start_urzad_id,
                    create_urzad_name=nazwa,
                    urzad_cache=urzad_cache,
                    urzad_code_index=urzad_code_index,
                )
                self._ensure_urzad_code(
                    connection=connection,
                    urzad_id=int(urzad_id),
                    urzad_code=kod_us,
                )
                if urzad_created:
                    stats.created_urzedy += 1
                    stats.created_urzedy_ids.append(int(urzad_id))

                link_row = connection.execute(
                    text(
                        """
                        SELECT 1
                        FROM URZEDY_PRACOWNIKA
                        WHERE ID_PRACOWNIKA = :employee_id
                          AND ID_URZEDU = :urzad_id
                          AND DATA_OD = :data_od
                        """
                    ),
                    {"employee_id": employee_id, "urzad_id": urzad_id, "data_od": row_data_od},
                ).first()
                resolved_data_od = row_data_od
                if link_row is not None:
                    resolved_data_od = self._next_free_data_od_for_link(
                        connection=connection,
                        employee_id=employee_id,
                        urzad_id=urzad_id,
                        proposed_data_od=row_data_od,
                    )
                    stats.shifted_link_dates += 1

                next_link_id = self._allocate_next_free_id(used_ids=used_link_ids, start_value=1)
                connection.execute(
                    text(
                        """
                        INSERT INTO URZEDY_PRACOWNIKA
                            (IDENTYFIKATOR, ID_URZEDU, ID_PRACOWNIKA, DATA_OD)
                        VALUES
                            (:identyfikator, :id_urzedu, :id_pracownika, :data_od)
                        """
                    ),
                    {
                        "identyfikator": next_link_id,
                        "id_urzedu": urzad_id,
                        "id_pracownika": employee_id,
                        "data_od": resolved_data_od,
                    },
                )
                stats.created_links += 1
                stats.created_link_ids.append(next_link_id)
                _notify_progress(progress_callback, idx, total)

        return stats

    def execute_employee_import(
        self,
        rows: list[dict],
        start_urzad_id: int,
        id_firmy: int,
        data_od: int,
        progress_callback: ProgressCallback | None = None,
        cancel_token: CancelToken | None = None,
    ) -> EmployeeImportStats:
        stats = EmployeeImportStats()
        if not rows:
            return stats

        total = len(rows)
        _notify_progress(progress_callback, 0, total)
        with self.engine.begin() as connection:
            used_employee_ids = self._load_used_ints_locked(
                connection=connection,
                table_name="PRACOWNIK",
                column_name="ID_PRACOWNIKA",
            )
            used_kod_pracownika = self._load_used_ints_locked(
                connection=connection,
                table_name="PRACOWNIK",
                column_name="KOD_PRACOWNIKA",
                where_sql="ID_FIRMY = :id_firmy AND KOD_PRACOWNIKA IS NOT NULL",
                params={"id_firmy": id_firmy},
            )
            used_nr_ewid = self._load_used_ints_locked(
                connection=connection,
                table_name="PRACOWNIK",
                column_name="NR_EWIDENCYJNY",
                where_sql=(
                    "ID_FIRMY = :id_firmy "
                    "AND NR_EWIDENCYJNY IS NOT NULL "
                    "AND TRY_CAST(NR_EWIDENCYJNY AS int) IS NOT NULL"
                ),
                params={"id_firmy": id_firmy},
                use_try_cast=True,
            )
            used_urzedy_ids = self._load_used_ints_locked(
                connection=connection,
                table_name="URZEDY",
                column_name="ID_URZEDU",
                min_value=start_urzad_id,
            )
            used_link_ids = self._load_used_ints_locked(
                connection=connection,
                table_name="URZEDY_PRACOWNIKA",
                column_name="IDENTYFIKATOR",
            )
            urzad_cache = self._load_urzedy_cache(connection)
            urzad_code_index = self._build_urzad_code_index(connection)
            used_pesels = self._load_used_pesels(connection=connection, id_firmy=id_firmy)
            firm_id_struktury, firm_tree, firm_id_kalendarza, firm_id_schematu = (
                self._resolve_firm_insert_params(connection, id_firmy)
            )

            for idx, row in enumerate(rows, start=1):
                _raise_if_cancelled(cancel_token)
                urzad_name = str(row.get("urzad_name", "")).strip()
                urzad_code = str(row.get("urzad_code_from_reference", "")).strip()
                urzad_name_from_reference = str(row.get("urzad_name_from_reference", "")).strip()
                row_data_od = int(row.get("data_od", data_od))
                if not urzad_code:
                    urzad_code = str(row.get("urzad_code", "")).strip()

                pesel_value = str(row.get("pesel", "")).strip()
                if pesel_value and pesel_value in used_pesels:
                    stats.skipped_duplicates += 1
                    _notify_progress(progress_callback, idx, total)
                    continue

                # Only resolve/create an urząd when the row actually contains
                # urząd information. CRM onboarding may legitimately have an
                # empty urząd (employee has no authority_agency yet).
                has_urzad_info = bool(urzad_code or urzad_name or urzad_name_from_reference)
                urzad_id: int | None = None
                if has_urzad_info:
                    urzad_id, urzad_created = self._resolve_or_create_urzad(
                        connection=connection,
                        urzad_name=urzad_name,
                        urzad_code=urzad_code,
                        create_urzad_name=urzad_name_from_reference,
                        used_urzedy_ids=used_urzedy_ids,
                        start_urzad_id=start_urzad_id,
                        urzad_cache=urzad_cache,
                        urzad_code_index=urzad_code_index,
                    )
                    self._ensure_urzad_code(
                        connection=connection,
                        urzad_id=int(urzad_id),
                        urzad_code=urzad_code,
                    )
                    if urzad_created:
                        stats.created_urzedy += 1
                        stats.created_urzedy_ids.append(int(urzad_id))

                next_employee_id = self._allocate_next_free_id(used_ids=used_employee_ids, start_value=1)
                next_kod_pracownika = self._allocate_next_free_id(
                    used_ids=used_kod_pracownika,
                    start_value=1,
                )
                next_nr_ewid = self._allocate_next_free_id(
                    used_ids=used_nr_ewid,
                    start_value=1,
                )
                nr_ewidencyjny = str(next_nr_ewid)
                nazwisko, imie = self._split_full_name(str(row.get("full_name", "")))
                data_urodzenia = self._parse_excel_date_to_clarion(str(row.get("birth_date", "")).strip())

                connection.execute(
                    text(
                        """
                        INSERT INTO PRACOWNIK
                            (ID_PRACOWNIKA, ID_FIRMY, KOD_PRACOWNIKA, NAZWISKO, IMIE_1,
                             DATA_URODZENIA, PESEL, DOWOD, PASZPORT, TELEFON,
                             NR_EWIDENCYJNY, ID_US, RODZAJ_PRACOWNIKA,
                             ID_STRUKTURY, ID_KALENDARZA, ID_SCHEMATU, TREE_STRUKTURY,
                             ARCHIWUM, GANG, DATA_WPROWADZENIA_DANYCH, UZYTKOWNIK_WPROWADZ,
                             DATA_MODYFIKACJI_DANYCH, UZYTKOWNIK_MODYFIK, RODO_DATA)
                        VALUES
                            (:id_pracownika, :id_firmy, :kod_pracownika, :nazwisko, :imie_1,
                             :data_urodzenia, :pesel, :dowod, :paszport, :telefon,
                             :nr_ewidencyjny, :id_us, :rodzaj_pracownika,
                             :id_struktury, :id_kalendarza, :id_schematu, :tree_struktury,
                             1, 1, CONVERT(int, DATEDIFF(day, '1800-12-28', GETDATE())), 'Administrator',
                             0, '', 0)
                        """
                    ),
                    {
                        "id_pracownika": next_employee_id,
                        "id_firmy": id_firmy,
                        "kod_pracownika": next_kod_pracownika,
                        "nazwisko": _fit(nazwisko, "PRACOWNIK.NAZWISKO"),
                        "imie_1": _fit(imie, "PRACOWNIK.IMIE_1"),
                        "data_urodzenia": data_urodzenia,
                        "pesel": _fit(row.get("pesel", ""), "PRACOWNIK.PESEL"),
                        "dowod": _fit(
                            ""
                            if str(row.get("id_card_no", "")).strip().lower() == "nan"
                            else row.get("id_card_no", ""),
                            "PRACOWNIK.DOWOD",
                        ),
                        "paszport": _fit(
                            ""
                            if str(row.get("passport_no", "")).strip().lower() == "nan"
                            else row.get("passport_no", ""),
                            "PRACOWNIK.PASZPORT",
                        ),
                        "telefon": _fit(row.get("phone", ""), "PRACOWNIK.TELEFON"),
                        "nr_ewidencyjny": _fit(nr_ewidencyjny, "PRACOWNIK.NR_EWIDENCYJNY"),
                        "id_us": None,
                        "rodzaj_pracownika": 2,
                        "id_struktury": firm_id_struktury,
                        "id_kalendarza": firm_id_kalendarza,
                        "id_schematu": firm_id_schematu,
                        "tree_struktury": firm_tree,
                    },
                )
                stats.created_employees += 1
                stats.created_employee_ids.append(int(next_employee_id))
                if pesel_value:
                    used_pesels.add(pesel_value)

                # When `skip_address` is set (CRM onboarding without Polish
                # address) we omit ADRESY_PRACOWNIKA — the urząd link in
                # URZEDY_PRACOWNIKA is still created when an urząd is known.
                skip_address = bool(row.get("skip_address", False))
                if not skip_address:
                    connection.execute(
                        text(
                            """
                            DECLARE @new_ids TABLE (id int);
                            INSERT INTO ADRESY_PRACOWNIKA
                                (DATA_OD, WOJEWODZTWO, POWIAT, GMINA,
                                 MIEJSCOWOSC, KOD_POCZTOWY, POCZTA, ULICA, NR_DOMU,
                                 NR_LOKALU, TELEFON, AKTYWNY, ID_PRACOWNIKA, KRAJ,
                                 KP_ID_URZEDU_SKARBOWEGO)
                            OUTPUT INSERTED.ID_ADRESY_PRAC INTO @new_ids(id)
                            VALUES
                                (:data_od, :wojewodztwo, :powiat, :gmina,
                                 :miejscowosc, :kod_pocztowy, :poczta, :ulica, :nr_domu,
                                 :nr_lokalu, :telefon, 1, :id_pracownika, :kraj,
                                 :kp_id_urzedu)
                            SELECT TOP 1 id AS ID_ADRESY_PRAC FROM @new_ids
                            """
                        ),
                        {
                            "data_od": row_data_od,
                            "wojewodztwo": _fit(row.get("voivodeship", ""), "ADRESY_PRACOWNIKA.WOJEWODZTWO"),
                            "powiat": _fit(row.get("powiat", ""), "ADRESY_PRACOWNIKA.POWIAT"),
                            "gmina": _fit(row.get("gmina", ""), "ADRESY_PRACOWNIKA.GMINA"),
                            "miejscowosc": _fit(row.get("city", ""), "ADRESY_PRACOWNIKA.MIEJSCOWOSC"),
                            "kod_pocztowy": _fit(row.get("postal_code", ""), "ADRESY_PRACOWNIKA.KOD_POCZTOWY"),
                            "poczta": _fit(row.get("post_office", ""), "ADRESY_PRACOWNIKA.POCZTA"),
                            "ulica": _fit(row.get("street", ""), "ADRESY_PRACOWNIKA.ULICA"),
                            "nr_domu": _fit(row.get("house_no", ""), "ADRESY_PRACOWNIKA.NR_DOMU"),
                            "nr_lokalu": _fit(row.get("flat_no", ""), "ADRESY_PRACOWNIKA.NR_LOKALU"),
                            "telefon": _fit(row.get("phone", ""), "ADRESY_PRACOWNIKA.TELEFON"),
                            "id_pracownika": next_employee_id,
                            "kraj": _fit(row.get("country", ""), "ADRESY_PRACOWNIKA.KRAJ"),
                            "kp_id_urzedu": urzad_id,
                        },
                    )
                    created_address_id = connection.execute(
                        text(
                            """
                            SELECT TOP 1 ID_ADRESY_PRAC
                            FROM ADRESY_PRACOWNIKA
                            WHERE ID_PRACOWNIKA = :id_pracownika
                              AND DATA_OD = :data_od
                            ORDER BY ID_ADRESY_PRAC DESC
                            """
                        ),
                        {"id_pracownika": int(next_employee_id), "data_od": int(row_data_od)},
                    ).scalar()
                    stats.created_addresses += 1
                    if created_address_id is not None:
                        stats.created_address_ids.append(int(created_address_id))

                # Link to urząd: only when we actually resolved/created one and
                # the row provided an urzad code/name. Skipping the link when
                # no urząd info is present keeps card clean for manual review.
                has_urzad_info = bool(urzad_code or urzad_name)
                if has_urzad_info:
                    next_link_id = self._allocate_next_free_id(used_ids=used_link_ids, start_value=1)
                    connection.execute(
                        text(
                            """
                            INSERT INTO URZEDY_PRACOWNIKA
                                (IDENTYFIKATOR, ID_URZEDU, ID_PRACOWNIKA, DATA_OD)
                            VALUES
                                (:identyfikator, :id_urzedu, :id_pracownika, :data_od)
                            """
                        ),
                        {
                            "identyfikator": next_link_id,
                            "id_urzedu": urzad_id,
                            "id_pracownika": next_employee_id,
                            "data_od": row_data_od,
                        },
                    )
                    stats.created_links += 1
                    stats.created_link_ids.append(int(next_link_id))
                _notify_progress(progress_callback, idx, total)

        return stats

    def execute_employee_address_import(
        self,
        rows: list[dict],
        data_od: int,
        id_firmy: int = 1,
        progress_callback: ProgressCallback | None = None,
        cancel_token: CancelToken | None = None,
    ) -> EmployeeAddressImportStats:
        stats = EmployeeAddressImportStats()
        if not rows:
            return stats

        total = len(rows)
        _notify_progress(progress_callback, 0, total)
        with self.engine.begin() as connection:
            for idx, row in enumerate(rows, start=1):
                _raise_if_cancelled(cancel_token)
                employee_lookup_value = str(row.get("employee_lookup_value", "")).strip()
                employee_lookup_mode = str(row.get("employee_lookup_mode", "nr")).strip().lower()
                row_data_od = int(row.get("data_od", data_od))
                employee_id = row.get("employee_id")
                if employee_id is None and employee_lookup_value:
                    employee_id = (
                        self.employee_id_by_pesel(employee_lookup_value, connection=connection, id_firmy=id_firmy)
                        if employee_lookup_mode == "pesel"
                        else self.employee_id_by_nr(employee_lookup_value, connection=connection, id_firmy=id_firmy)
                    )
                if employee_id is None:
                    stats.missing_employees += 1
                    _notify_progress(progress_callback, idx, total)
                    continue

                existing_address = connection.execute(
                    text(
                        """
                        SELECT TOP 1 ID_ADRESY_PRAC
                        FROM ADRESY_PRACOWNIKA
                        WHERE ID_PRACOWNIKA = :id_pracownika
                          AND DATA_OD = :data_od
                        ORDER BY ID_ADRESY_PRAC DESC
                        """
                    ),
                    {"id_pracownika": int(employee_id), "data_od": row_data_od},
                ).first()

                params = {
                    "id_pracownika": int(employee_id),
                    "data_od": row_data_od,
                    "wojewodztwo": _fit(row.get("voivodeship", ""), "ADRESY_PRACOWNIKA.WOJEWODZTWO"),
                    "powiat": _fit(row.get("powiat", ""), "ADRESY_PRACOWNIKA.POWIAT"),
                    "gmina": _fit(row.get("gmina", ""), "ADRESY_PRACOWNIKA.GMINA"),
                    "miejscowosc": _fit(row.get("city", ""), "ADRESY_PRACOWNIKA.MIEJSCOWOSC"),
                    "kod_pocztowy": _fit(row.get("postal_code", ""), "ADRESY_PRACOWNIKA.KOD_POCZTOWY"),
                    "poczta": _fit(row.get("post_office", ""), "ADRESY_PRACOWNIKA.POCZTA"),
                    "ulica": _fit(row.get("street", ""), "ADRESY_PRACOWNIKA.ULICA"),
                    "nr_domu": _fit(row.get("house_no", ""), "ADRESY_PRACOWNIKA.NR_DOMU"),
                    "nr_lokalu": _fit(row.get("flat_no", ""), "ADRESY_PRACOWNIKA.NR_LOKALU"),
                    "telefon": _fit(row.get("phone", ""), "ADRESY_PRACOWNIKA.TELEFON"),
                    "kraj": _fit(row.get("country", ""), "ADRESY_PRACOWNIKA.KRAJ"),
                }
                if existing_address is not None:
                    params["data_od"] = self._next_free_data_od_for_address(
                        connection=connection,
                        employee_id=int(employee_id),
                        proposed_data_od=row_data_od,
                    )
                    stats.shifted_address_dates += 1

                created_address_id = connection.execute(
                    text(
                        """
                        INSERT INTO ADRESY_PRACOWNIKA
                            (DATA_OD, WOJEWODZTWO, POWIAT, GMINA, MIEJSCOWOSC,
                             KOD_POCZTOWY, POCZTA, ULICA, NR_DOMU, NR_LOKALU,
                             TELEFON, AKTYWNY, ID_PRACOWNIKA, KRAJ)
                        OUTPUT INSERTED.ID_ADRESY_PRAC
                        VALUES
                            (:data_od, :wojewodztwo, :powiat, :gmina, :miejscowosc,
                             :kod_pocztowy, :poczta, :ulica, :nr_domu, :nr_lokalu,
                             :telefon, 1, :id_pracownika, :kraj)
                        """
                    ),
                    params,
                ).scalar()
                stats.created_addresses += 1
                if created_address_id is not None:
                    stats.created_address_ids.append(int(created_address_id))
                _notify_progress(progress_callback, idx, total)

        return stats

    def execute_umowy_import(
        self,
        rows: list[dict],
        id_firmy: int = 1,
        progress_callback: ProgressCallback | None = None,
        cancel_token: CancelToken | None = None,
    ) -> UmowyImportStats:
        stats = UmowyImportStats()
        if not rows:
            return stats

        total = len(rows)
        _notify_progress(progress_callback, 0, total)
        with self.engine.begin() as connection:
            seen_batch_keys: set[tuple[int, str, int, int, str]] = set()
            for idx, row in enumerate(rows, start=1):
                _raise_if_cancelled(cancel_token)
                employee_id = row.get("employee_id")
                employee_lookup_value = str(row.get("employee_lookup_value", "")).strip()
                employee_lookup_mode = str(row.get("employee_lookup_mode", "nr")).strip().lower()
                if employee_id is None and employee_lookup_value:
                    employee_id = (
                        self.employee_id_by_pesel(employee_lookup_value, connection=connection, id_firmy=id_firmy)
                        if employee_lookup_mode == "pesel"
                        else self.employee_id_by_nr(employee_lookup_value, connection=connection, id_firmy=id_firmy)
                    )
                if employee_id is None:
                    stats.missing_employees += 1
                    _notify_progress(progress_callback, idx, total)
                    continue

                numer_umowy = _fit(
                    str(row.get("numer_umowy", "")).strip(),
                    "GANG_UMOWY_CYWILNO_PRAWNE.NUMER_UMOWY",
                )
                data_umowy = int(row.get("data_umowy", 0))
                data_wyplaty = int(row.get("data_wyplaty", 0))
                numer_rachunku = _fit(
                    str(row.get("numer_rachunku", "")).strip(),
                    "GANG_UMOWY_CYWILNO_PRAWNE.NUMER_RACHUNKU",
                )
                batch_key = (
                    int(employee_id),
                    numer_umowy,
                    data_umowy,
                    data_wyplaty,
                    numer_rachunku,
                )
                if batch_key in seen_batch_keys:
                    stats.skipped_duplicates += 1
                    _notify_progress(progress_callback, idx, total)
                    continue

                existing_umowa = connection.execute(
                    text(
                        """
                        SELECT 1
                        FROM GANG_UMOWY_CYWILNO_PRAWNE
                        WHERE ID_NADRZEDNEGO = :employee_id
                          AND NUMER_UMOWY = :numer_umowy
                          AND DATA_UMOWY = :data_umowy
                          AND DATA_WYPLATY = :data_wyplaty
                          AND NUMER_RACHUNKU = :numer_rachunku
                        """
                    ),
                    {
                        "employee_id": int(employee_id),
                        "numer_umowy": numer_umowy,
                        "data_umowy": data_umowy,
                        "data_wyplaty": data_wyplaty,
                        "numer_rachunku": numer_rachunku,
                    },
                ).first()
                if existing_umowa is not None:
                    stats.skipped_duplicates += 1
                    _notify_progress(progress_callback, idx, total)
                    continue

                brutto = float(row.get("wynagrodzenie_brutto", 0))
                ppk_pracownika_kwota_source = float(row.get("ppk_pracownika_kwota", 0) or 0)
                has_ppk = ppk_pracownika_kwota_source > 0.0 and brutto > 0.0
                if has_ppk:
                    ppk_uczestnik_kwota = round(
                        brutto * PPK_UCZESTNIK_PODSTAWOWA_PROC / 100.0, 2
                    )
                    ppk_pracodawcy_kwota = round(
                        brutto * PPK_PRACODAWCA_PODSTAWOWA_PROC / 100.0, 2
                    )
                    ppk_uczestnik_proc = PPK_UCZESTNIK_PODSTAWOWA_PROC
                    ppk_pracodawcy_proc = PPK_PRACODAWCA_PODSTAWOWA_PROC
                else:
                    ppk_uczestnik_kwota = 0.0
                    ppk_pracodawcy_kwota = 0.0
                    ppk_uczestnik_proc = 0.0
                    ppk_pracodawcy_proc = 0.0
                koszty_proc = _normalize_kup_percent(float(row.get("koszty_proc", 0)))
                emerytalne_proc = float(row.get("emerytalne_proc", 0))
                rentowe_u_proc = float(row.get("rentowe_u_proc", 0))
                rentowe_p_proc = float(row.get("rentowe_p_proc", 0))
                chorobowe_proc = float(row.get("chorobowe_proc", 0))
                zdrowotne_proc = float(row.get("zdrowotne_proc", 0))
                fp_proc = float(row.get("fp_proc", 0))
                fgsp_proc = float(row.get("fgsp_proc", 0))

                source_student_case = _is_student_umowa_case(
                    kup_proc=koszty_proc,
                    emerytalne_proc=emerytalne_proc,
                    rentowe_u_proc=rentowe_u_proc,
                    chorobowe_proc=chorobowe_proc,
                    zdrowotne_proc=zdrowotne_proc,
                    fp_proc=fp_proc,
                    fgsp_proc=fgsp_proc,
                )

                # Resolve PESEL for student PIT handling.
                pesel_for_age = ""
                if employee_lookup_mode == "pesel":
                    pesel_for_age = employee_lookup_value
                else:
                    pesel_for_age = (
                        self.pesel_by_employee_id(int(employee_id), connection=connection) or ""
                    )
                stawka_podatku_proc = _resolve_umowa_zlecenie_pit_stawka(
                    pesel_for_age,
                    int(data_wyplaty),
                    source_student_case,
                )
                stawka_z_pliku = _optional_float(row.get("stawka_podatku_proc"))
                if stawka_z_pliku is not None:
                    stawka_podatku_proc = stawka_z_pliku

                # Legal rules 2026:
                # - FP disabled when brutto is below minimum wage threshold.
                # - FP disabled by age exemption (K 55+, M 60+ from next month).
                fp_active = brutto >= FP_MIN_BRUTTO_2026
                age_exempt = _is_fp_fgsp_age_exempt(pesel_for_age, int(data_wyplaty))
                if not fp_active or age_exempt:
                    fp_proc = 0.0

                # Employer rates depend on which company (ID_FIRMY) the employee
                # belongs to.  Students (ZUS-exempt rows) always get 0.
                _eps = 1e-9
                _source_zus_exempt = (
                    abs(emerytalne_proc) < _eps
                    and abs(rentowe_u_proc) < _eps
                    and abs(chorobowe_proc) < _eps
                    and abs(zdrowotne_proc) < _eps
                )
                _firma_id = (
                    self.firma_id_by_pesel(pesel_for_age, connection=connection)
                    if pesel_for_age
                    else None
                )
                _wypadkowe_rate = _WYPADKOWE_BY_FIRMA.get(
                    _firma_id, _WYPADKOWE_DEFAULT
                ) if _firma_id is not None else _WYPADKOWE_DEFAULT
                _fgsp_rate = _FGSP_BY_FIRMA.get(
                    _firma_id, _FGSP_DEFAULT
                ) if _firma_id is not None else _FGSP_DEFAULT

                wypadkowe_proc = 0.0 if _source_zus_exempt else _wypadkowe_rate
                # FGSP follows the same exemption rules as FP:
                # zero for ZUS-exempt workers, age-exempt workers, and below
                # minimum-wage brutto.
                fgsp_proc = (
                    0.0
                    if (_source_zus_exempt or not fp_active or age_exempt)
                    else _fgsp_rate
                )
                forma_opodatkowania = _fit(
                    str(row.get("forma_podatka", "")).strip(),
                    "GANG_UMOWY_CYWILNO_PRAWNE.FORMA_OPODATKOWANIA",
                )
                rodzaj_umowy = _fit(
                    str(row.get("typ_umowy", "")).strip(),
                    "GANG_UMOWY_CYWILNO_PRAWNE.RODZAJ_UMOWY",
                )

                # Replicate "Wylicz" so user does not have to click 200 contracts manually.
                wylicz = _calculate_umowa_financials(
                    brutto=brutto,
                    kup_proc=koszty_proc,
                    stawka_podatku_proc=stawka_podatku_proc,
                    emerytalne_proc=emerytalne_proc,
                    rentowe_u_proc=rentowe_u_proc,
                    rentowe_p_proc=rentowe_p_proc,
                    chorobowe_proc=chorobowe_proc,
                    wypadkowe_proc=wypadkowe_proc,
                    zdrowotne_proc=zdrowotne_proc,
                    fp_proc=fp_proc,
                    fgsp_proc=fgsp_proc,
                )

                created_contract_id = connection.execute(
                    text(
                        """
                        INSERT INTO GANG_UMOWY_CYWILNO_PRAWNE
                            (ID_NADRZEDNEGO, NUMER_UMOWY, DATA_UMOWY,
                             RODZAJ_UMOWY, FORMA_OPODATKOWANIA, NUMER_RACHUNKU,
                             DATA_RACHUNKU, DATA_WYPLATY, WYNAGRODZENIE_BRUTTO,
                             STAWKA_PODATKU____, KWOTA_PODATKU, KWOTA_DO_WYPLATY,
                             KOSZTY_UZYSKANIA____, KOSZTY_UZYSKANIA__KWOTA_, DOCHOD,
                             EMERYTALNE____, RENTOWE_U____, RENTOWE____,
                             CHOROBOWE____, WYPADKOWE____, ZDROWOTNE____, FP____, FGSP____,
                             EMERYTALNE_ZLECENIOBIORCA, RENTOWE_ZLECENIOBIORCA,
                             CHOROBOWE_ZLECENIOBIORCA, ZDROWOTNE_ZLECENIOBIORCA,
                             EMERYTALNE_ZLECENIODAWCA, RENTOWE_ZLECENIODAWCA,
                             WYPADKOWE_ZLECENIODAWCA, FP, FGSP,
                             PODSTAWOWA_UCZESTNIKA_PPK, DODATKOWA_UCZESTNIKA_PPK,
                             PODSTAWOWA_PRACODAWCY_PPK, DODATKOWA_PRACODAWCY_PPK,
                             PPK_PODSTAWOWA_UCZESTNIK____, PPK_DODATKOWA_UCZESTNIK____,
                             PPK_PODSTAWOWA_PRACODAWCA____, PPK_DODATKOWA_PRACODAWCA____)
                        OUTPUT INSERTED.IDENTYFIKATOR
                        VALUES
                            (:id_nadrzednego, :numer_umowy, :data_umowy,
                             :rodzaj_umowy, :forma_opodatkowania, :numer_rachunku,
                             :data_rachunku, :data_wyplaty, :wynagrodzenie_brutto,
                             :stawka_podatku, :kwota_podatku, :kwota_do_wyplaty,
                             :koszty_uzyskania, :koszty_uzyskania_kwota, :dochod,
                             :emerytalne_proc, :rentowe_u_proc, :rentowe_p_proc,
                             :chorobowe_proc, :wypadkowe_proc, :zdrowotne_proc, :fp_proc, :fgsp_proc,
                             :emerytalne_zleceniobiorca, :rentowe_zleceniobiorca,
                             :chorobowe_zleceniobiorca, :zdrowotne_zleceniobiorca,
                             :emerytalne_zleceniodawca, :rentowe_zleceniodawca,
                             :wypadkowe_zleceniodawca, :fp_kwota, :fgsp_kwota,
                             :ppk_uczestnik_pod, :ppk_uczestnik_dod,
                             :ppk_pracodawca_pod, :ppk_pracodawca_dod,
                             :ppk_uczestnik_pod_proc, :ppk_uczestnik_dod_proc,
                             :ppk_pracodawca_pod_proc, :ppk_pracodawca_dod_proc)
                        """
                    ),
                    {
                        "id_nadrzednego": int(employee_id),
                        "numer_umowy": numer_umowy,
                        "data_umowy": data_umowy,
                        "rodzaj_umowy": rodzaj_umowy,
                        "forma_opodatkowania": forma_opodatkowania,
                        "numer_rachunku": numer_rachunku,
                        "data_rachunku": data_wyplaty,
                        "data_wyplaty": data_wyplaty,
                        "wynagrodzenie_brutto": brutto,
                        "stawka_podatku": wylicz["stawka_podatku"],
                        "kwota_podatku": wylicz["kwota_podatku"],
                        "kwota_do_wyplaty": wylicz["kwota_do_wyplaty"],
                        "koszty_uzyskania": koszty_proc,
                        "koszty_uzyskania_kwota": wylicz["kup_kwota"],
                        "dochod": wylicz["dochod"],
                        "emerytalne_proc": emerytalne_proc,
                        "rentowe_u_proc": rentowe_u_proc,
                        "rentowe_p_proc": rentowe_p_proc,
                        "chorobowe_proc": chorobowe_proc,
                        "wypadkowe_proc": wypadkowe_proc,
                        "zdrowotne_proc": zdrowotne_proc,
                        "fp_proc": fp_proc,
                        "fgsp_proc": fgsp_proc,
                        "emerytalne_zleceniobiorca": wylicz["emerytalne_zleceniobiorca"],
                        "rentowe_zleceniobiorca": wylicz["rentowe_zleceniobiorca"],
                        "chorobowe_zleceniobiorca": wylicz["chorobowe_zleceniobiorca"],
                        "zdrowotne_zleceniobiorca": wylicz["zdrowotne_zleceniobiorca"],
                        "emerytalne_zleceniodawca": wylicz["emerytalne_zleceniodawca"],
                        "rentowe_zleceniodawca": wylicz["rentowe_zleceniodawca"],
                        "wypadkowe_zleceniodawca": wylicz["wypadkowe_zleceniodawca"],
                        "fp_kwota": wylicz["fp_kwota"],
                        "fgsp_kwota": wylicz["fgsp_kwota"],
                        "ppk_uczestnik_pod": ppk_uczestnik_kwota,
                        "ppk_uczestnik_dod": 0.0,
                        "ppk_pracodawca_pod": ppk_pracodawcy_kwota,
                        "ppk_pracodawca_dod": 0.0,
                        "ppk_uczestnik_pod_proc": ppk_uczestnik_proc,
                        "ppk_uczestnik_dod_proc": 0.0,
                        "ppk_pracodawca_pod_proc": ppk_pracodawcy_proc,
                        "ppk_pracodawca_dod_proc": 0.0,
                    },
                ).scalar()
                seen_batch_keys.add(batch_key)
                stats.created_contracts += 1
                if created_contract_id is not None:
                    stats.created_contract_ids.append(int(created_contract_id))
                _notify_progress(progress_callback, idx, total)

        return stats

    def execute_umowy_dzielo_import(
        self,
        rows: list[dict],
        id_firmy: int = 1,
        progress_callback: ProgressCallback | None = None,
        cancel_token: CancelToken | None = None,
    ) -> UmowyImportStats:
        """Import umów o dzieło: bez składek ZUS, stała stawka PIT 12%, KUP od pełnego brutto."""
        stats = UmowyImportStats()
        if not rows:
            return stats

        total = len(rows)
        _notify_progress(progress_callback, 0, total)
        with self.engine.begin() as connection:
            seen_batch_keys: set[tuple[int, str, int, int, str]] = set()
            for idx, row in enumerate(rows, start=1):
                _raise_if_cancelled(cancel_token)
                employee_id = row.get("employee_id")
                employee_lookup_value = str(row.get("employee_lookup_value", "")).strip()
                employee_lookup_mode = str(row.get("employee_lookup_mode", "nr")).strip().lower()
                if employee_id is None and employee_lookup_value:
                    employee_id = (
                        self.employee_id_by_pesel(employee_lookup_value, connection=connection, id_firmy=id_firmy)
                        if employee_lookup_mode == "pesel"
                        else self.employee_id_by_nr(employee_lookup_value, connection=connection, id_firmy=id_firmy)
                    )
                if employee_id is None:
                    stats.missing_employees += 1
                    _notify_progress(progress_callback, idx, total)
                    continue

                numer_umowy = _fit(
                    str(row.get("numer_umowy", "")).strip(),
                    "GANG_UMOWY_CYWILNO_PRAWNE.NUMER_UMOWY",
                )
                data_umowy = int(row.get("data_umowy", 0))
                data_wyplaty = int(row.get("data_wyplaty", 0))
                numer_rachunku = _fit(
                    str(row.get("numer_rachunku", "")).strip(),
                    "GANG_UMOWY_CYWILNO_PRAWNE.NUMER_RACHUNKU",
                )
                batch_key = (
                    int(employee_id),
                    numer_umowy,
                    data_umowy,
                    data_wyplaty,
                    numer_rachunku,
                )
                if batch_key in seen_batch_keys:
                    stats.skipped_duplicates += 1
                    _notify_progress(progress_callback, idx, total)
                    continue

                existing_umowa = connection.execute(
                    text(
                        """
                        SELECT 1
                        FROM GANG_UMOWY_CYWILNO_PRAWNE
                        WHERE ID_NADRZEDNEGO = :employee_id
                          AND NUMER_UMOWY = :numer_umowy
                          AND DATA_UMOWY = :data_umowy
                          AND DATA_WYPLATY = :data_wyplaty
                          AND NUMER_RACHUNKU = :numer_rachunku
                        """
                    ),
                    {
                        "employee_id": int(employee_id),
                        "numer_umowy": numer_umowy,
                        "data_umowy": data_umowy,
                        "data_wyplaty": data_wyplaty,
                        "numer_rachunku": numer_rachunku,
                    },
                ).first()
                if existing_umowa is not None:
                    stats.skipped_duplicates += 1
                    _notify_progress(progress_callback, idx, total)
                    continue

                brutto = float(row.get("wynagrodzenie_brutto", 0))
                koszty_proc = _normalize_kup_percent(float(row.get("koszty_proc", 0)))
                forma_opodatkowania = _fit(
                    str(row.get("forma_podatka", "")).strip(),
                    "GANG_UMOWY_CYWILNO_PRAWNE.FORMA_OPODATKOWANIA",
                )
                rodzaj_umowy = _fit(
                    "2",
                    "GANG_UMOWY_CYWILNO_PRAWNE.RODZAJ_UMOWY",
                )

                stawka_z_pliku = _optional_float(row.get("stawka_podatku_proc"))
                stawka_podatku_proc = (
                    stawka_z_pliku if stawka_z_pliku is not None else PIT_DEFAULT_STAWKA
                )

                wylicz = _calculate_umowa_o_dzielo_financials(
                    brutto=brutto,
                    kup_proc=koszty_proc,
                    stawka_podatku_proc=stawka_podatku_proc,
                )

                created_contract_id = connection.execute(
                    text(
                        """
                        INSERT INTO GANG_UMOWY_CYWILNO_PRAWNE
                            (ID_NADRZEDNEGO, NUMER_UMOWY, DATA_UMOWY,
                             RODZAJ_UMOWY, FORMA_OPODATKOWANIA, NUMER_RACHUNKU,
                             DATA_RACHUNKU, DATA_WYPLATY, WYNAGRODZENIE_BRUTTO,
                             STAWKA_PODATKU____, KWOTA_PODATKU, KWOTA_DO_WYPLATY,
                             KOSZTY_UZYSKANIA____, KOSZTY_UZYSKANIA__KWOTA_, DOCHOD,
                             EMERYTALNE____, RENTOWE_U____, RENTOWE____,
                             CHOROBOWE____, WYPADKOWE____, ZDROWOTNE____, FP____, FGSP____,
                             EMERYTALNE_ZLECENIOBIORCA, RENTOWE_ZLECENIOBIORCA,
                             CHOROBOWE_ZLECENIOBIORCA, ZDROWOTNE_ZLECENIOBIORCA,
                             EMERYTALNE_ZLECENIODAWCA, RENTOWE_ZLECENIODAWCA,
                             WYPADKOWE_ZLECENIODAWCA, FP, FGSP,
                             PODSTAWOWA_UCZESTNIKA_PPK, DODATKOWA_UCZESTNIKA_PPK)
                        OUTPUT INSERTED.IDENTYFIKATOR
                        VALUES
                            (:id_nadrzednego, :numer_umowy, :data_umowy,
                             :rodzaj_umowy, :forma_opodatkowania, :numer_rachunku,
                             :data_rachunku, :data_wyplaty, :wynagrodzenie_brutto,
                             :stawka_podatku, :kwota_podatku, :kwota_do_wyplaty,
                             :koszty_uzyskania, :koszty_uzyskania_kwota, :dochod,
                             :emerytalne_proc, :rentowe_u_proc, :rentowe_p_proc,
                             :chorobowe_proc, :wypadkowe_proc, :zdrowotne_proc, :fp_proc, :fgsp_proc,
                             :emerytalne_zleceniobiorca, :rentowe_zleceniobiorca,
                             :chorobowe_zleceniobiorca, :zdrowotne_zleceniobiorca,
                             :emerytalne_zleceniodawca, :rentowe_zleceniodawca,
                             :wypadkowe_zleceniodawca, :fp_kwota, :fgsp_kwota,
                             :ppk_podstawowa, :ppk_dodatkowa)
                        """
                    ),
                    {
                        "id_nadrzednego": int(employee_id),
                        "numer_umowy": numer_umowy,
                        "data_umowy": data_umowy,
                        "rodzaj_umowy": rodzaj_umowy,
                        "forma_opodatkowania": forma_opodatkowania,
                        "numer_rachunku": numer_rachunku,
                        "data_rachunku": data_wyplaty,
                        "data_wyplaty": data_wyplaty,
                        "wynagrodzenie_brutto": brutto,
                        "stawka_podatku": wylicz["stawka_podatku"],
                        "kwota_podatku": wylicz["kwota_podatku"],
                        "kwota_do_wyplaty": wylicz["kwota_do_wyplaty"],
                        "koszty_uzyskania": koszty_proc,
                        "koszty_uzyskania_kwota": wylicz["kup_kwota"],
                        "dochod": wylicz["dochod"],
                        "emerytalne_proc": 0.0,
                        "rentowe_u_proc": 0.0,
                        "rentowe_p_proc": 0.0,
                        "chorobowe_proc": 0.0,
                        "wypadkowe_proc": 0.0,
                        "zdrowotne_proc": 0.0,
                        "fp_proc": 0.0,
                        "fgsp_proc": 0.0,
                        "emerytalne_zleceniobiorca": wylicz["emerytalne_zleceniobiorca"],
                        "rentowe_zleceniobiorca": wylicz["rentowe_zleceniobiorca"],
                        "chorobowe_zleceniobiorca": wylicz["chorobowe_zleceniobiorca"],
                        "zdrowotne_zleceniobiorca": wylicz["zdrowotne_zleceniobiorca"],
                        "emerytalne_zleceniodawca": wylicz["emerytalne_zleceniodawca"],
                        "rentowe_zleceniodawca": wylicz["rentowe_zleceniodawca"],
                        "wypadkowe_zleceniodawca": wylicz["wypadkowe_zleceniodawca"],
                        "fp_kwota": wylicz["fp_kwota"],
                        "fgsp_kwota": wylicz["fgsp_kwota"],
                        "ppk_podstawowa": 0.0,
                        "ppk_dodatkowa": 0.0,
                    },
                ).scalar()
                seen_batch_keys.add(batch_key)
                stats.created_contracts += 1
                if created_contract_id is not None:
                    stats.created_contract_ids.append(int(created_contract_id))
                _notify_progress(progress_callback, idx, total)

        return stats

    def execute_ubezpieczenia_obowiazkowe_import(
        self,
        rows: list[dict],
        id_firmy: int = 1,
        progress_callback: ProgressCallback | None = None,
        cancel_token: CancelToken | None = None,
    ) -> UbezpieczeniaImportStats:
        stats = UbezpieczeniaImportStats()
        if not rows:
            return stats

        total = len(rows)
        _notify_progress(progress_callback, 0, total)
        with self.engine.begin() as connection:
            # Avoid indefinite freeze on SQL locks: fail fast with clear error.
            connection.execute(text("SET LOCK_TIMEOUT 15000"))
            for idx, row in enumerate(rows, start=1):
                _raise_if_cancelled(cancel_token)
                employee_id = row.get("employee_id")
                employee_lookup_value = str(row.get("employee_lookup_value", "")).strip()
                employee_lookup_mode = str(row.get("employee_lookup_mode", "nr")).strip().lower()
                numer_umowy = str(row.get("numer_umowy", "")).strip()
                if employee_id is None and employee_lookup_value:
                    employee_id = (
                        self.employee_id_by_pesel(employee_lookup_value, connection=connection, id_firmy=id_firmy)
                        if employee_lookup_mode == "pesel"
                        else self.employee_id_by_nr(employee_lookup_value, connection=connection, id_firmy=id_firmy)
                    )
                if employee_id is None:
                    stats.missing_employees += 1
                    _notify_progress(progress_callback, idx, total)
                    continue
                if not self.umowa_type_one_exists(
                    int(employee_id),
                    numer_umowy,
                    connection=connection,
                ):
                    stats.missing_type1_contract += 1
                    _notify_progress(progress_callback, idx, total)
                    continue

                data_obowiazku = int(row.get("data_obowiazku_ubezpieczenia", 0))
                data_od = int(row.get("data_od", data_obowiazku))
                emerytalne = int(row.get("ubezpieczenie_emerytalne", 0))
                rentowe = int(row.get("ubezpieczenie_rentowe", 0))
                wypadkowe = int(row.get("ubezpieczenie_wypadkowe", 0))
                chorobowe = int(row.get("ubezpieczenie_chorobowe", 0))

                row_year = None
                row_date = _clarion_to_date(data_obowiazku)
                if row_date is not None:
                    row_year = row_date.year
                if row_year is not None and self.obowiazkowe_ubezpieczenie_exists_for_year(
                    employee_id=int(employee_id),
                    year=row_year,
                    connection=connection,
                ):
                    stats.skipped_existing_year += 1
                    _notify_progress(progress_callback, idx, total)
                    continue

                if self.obowiazkowe_ubezpieczenie_exists(
                    employee_id=int(employee_id),
                    data_od=data_od,
                    data_obowiazku=data_obowiazku,
                    emerytalne=emerytalne,
                    rentowe=rentowe,
                    wypadkowe=wypadkowe,
                    chorobowe=chorobowe,
                    connection=connection,
                ):
                    stats.skipped_duplicates += 1
                    _notify_progress(progress_callback, idx, total)
                    continue

                created_insurance_id = connection.execute(
                    text(
                        """
                        INSERT INTO GANG_UBEZPIECZENIA_OBOWIAZKOWE
                            (ID_NADRZEDNEGO, DATA_OD, KOD_TYTULU,
                             EMERYTURA, NIEPELNOSPRAWNOSC, DATA_OBOWIAZKU, EMERYTALNE,
                             RENTOWE, CHOROBOWE, WYPADKOWE, ZDROWOTNE_OD, PODSTAWA, FGSP_WSP)
                        OUTPUT INSERTED.IDENTYFIKATOR
                        VALUES
                            (:id_nadrzednego, :data_od, :kod_tytulu,
                             '0', '0', :data_obowiazku, :emerytalne,
                             :rentowe, :chorobowe, :wypadkowe, :zdrowotne_od, 0, 0)
                        """
                    ),
                    {
                        "id_nadrzednego": int(employee_id),
                        "data_od": data_od,
                        "kod_tytulu": _fit(
                            str(row.get("typ_ubezpieczenia", "")).strip(),
                            "GANG_UBEZPIECZENIA_OBOWIAZKOWE.KOD_TYTULU",
                        ),
                        "data_obowiazku": data_obowiazku,
                        "emerytalne": emerytalne,
                        "rentowe": rentowe,
                        "chorobowe": chorobowe,
                        "wypadkowe": wypadkowe,
                        "zdrowotne_od": data_obowiazku,
                    },
                ).scalar()
                stats.created_insurance_rows += 1
                if created_insurance_id is not None:
                    stats.created_insurance_ids.append(int(created_insurance_id))
                _notify_progress(progress_callback, idx, total)
        return stats

    def execute_przeprowadzki_import(
        self,
        rows: list[dict],
        start_urzad_id: int,
        data_od: int,
        id_firmy: int = 1,
        progress_callback: ProgressCallback | None = None,
        cancel_token: CancelToken | None = None,
    ) -> PrzeprowadzkiImportStats:
        stats = PrzeprowadzkiImportStats()
        if not rows:
            return stats

        total = len(rows)
        _notify_progress(progress_callback, 0, total)
        with self.engine.begin() as connection:
            used_urzedy_ids = self._load_used_ints_locked(
                connection=connection,
                table_name="URZEDY",
                column_name="ID_URZEDU",
                min_value=start_urzad_id,
            )
            used_link_ids = self._load_used_ints_locked(
                connection=connection,
                table_name="URZEDY_PRACOWNIKA",
                column_name="IDENTYFIKATOR",
            )
            urzad_cache = self._load_urzedy_cache(connection)
            urzad_code_index = self._build_urzad_code_index(connection)

            for idx, row in enumerate(rows, start=1):
                _raise_if_cancelled(cancel_token)
                employee_id = row.get("employee_id")
                employee_lookup_value = str(row.get("employee_lookup_value", "")).strip()
                employee_lookup_mode = str(row.get("employee_lookup_mode", "nr")).strip().lower()
                row_data_od = int(row.get("data_od", data_od))
                if employee_id is None and employee_lookup_value:
                    employee_id = (
                        self.employee_id_by_pesel(employee_lookup_value, connection=connection, id_firmy=id_firmy)
                        if employee_lookup_mode == "pesel"
                        else self.employee_id_by_nr(employee_lookup_value, connection=connection, id_firmy=id_firmy)
                    )
                if employee_id is None:
                    stats.missing_employees += 1
                    _notify_progress(progress_callback, idx, total)
                    continue

                urzad_name = str(row.get("urzad_name", "")).strip()
                urzad_code = str(row.get("urzad_code_from_reference", "")).strip()
                urzad_name_from_reference = str(row.get("urzad_name_from_reference", "")).strip()
                urzad_id, urzad_created = self._resolve_or_create_urzad(
                    connection=connection,
                    urzad_name=urzad_name,
                    urzad_code=urzad_code,
                    create_urzad_name=urzad_name_from_reference,
                    used_urzedy_ids=used_urzedy_ids,
                    start_urzad_id=start_urzad_id,
                    urzad_cache=urzad_cache,
                    urzad_code_index=urzad_code_index,
                )
                self._ensure_urzad_code(
                    connection=connection,
                    urzad_id=int(urzad_id),
                    urzad_code=urzad_code,
                )
                if urzad_created:
                    stats.created_urzedy += 1
                    stats.created_urzedy_ids.append(int(urzad_id))

                address_params = {
                    "id_pracownika": int(employee_id),
                    "data_od": row_data_od,
                    "wojewodztwo": _fit(row.get("voivodeship", ""), "ADRESY_PRACOWNIKA.WOJEWODZTWO"),
                    "powiat": _fit(row.get("powiat", ""), "ADRESY_PRACOWNIKA.POWIAT"),
                    "gmina": _fit(row.get("gmina", ""), "ADRESY_PRACOWNIKA.GMINA"),
                    "miejscowosc": _fit(row.get("city", ""), "ADRESY_PRACOWNIKA.MIEJSCOWOSC"),
                    "kod_pocztowy": _fit(row.get("postal_code", ""), "ADRESY_PRACOWNIKA.KOD_POCZTOWY"),
                    "poczta": _fit(row.get("post_office", ""), "ADRESY_PRACOWNIKA.POCZTA"),
                    "ulica": _fit(row.get("street", ""), "ADRESY_PRACOWNIKA.ULICA"),
                    "nr_domu": _fit(row.get("house_no", ""), "ADRESY_PRACOWNIKA.NR_DOMU"),
                    "nr_lokalu": _fit(row.get("flat_no", ""), "ADRESY_PRACOWNIKA.NR_LOKALU"),
                    "telefon": _fit(row.get("phone", ""), "ADRESY_PRACOWNIKA.TELEFON"),
                    "kraj": _fit(row.get("country", ""), "ADRESY_PRACOWNIKA.KRAJ"),
                }
                existing_address = connection.execute(
                    text(
                        """
                        SELECT TOP 1 ID_ADRESY_PRAC
                        FROM ADRESY_PRACOWNIKA
                        WHERE ID_PRACOWNIKA = :id_pracownika
                          AND DATA_OD = :data_od
                        ORDER BY ID_ADRESY_PRAC DESC
                        """
                    ),
                    {
                        "id_pracownika": int(employee_id),
                        "data_od": row_data_od,
                    },
                ).first()
                if existing_address is not None:
                    address_params["data_od"] = self._next_free_data_od_for_address(
                        connection=connection,
                        employee_id=int(employee_id),
                        proposed_data_od=row_data_od,
                    )
                    stats.shifted_address_dates += 1

                created_address_id = connection.execute(
                    text(
                        """
                        INSERT INTO ADRESY_PRACOWNIKA
                            (DATA_OD, WOJEWODZTWO, POWIAT, GMINA, MIEJSCOWOSC,
                             KOD_POCZTOWY, POCZTA, ULICA, NR_DOMU, NR_LOKALU,
                             TELEFON, AKTYWNY, ID_PRACOWNIKA, KRAJ)
                        OUTPUT INSERTED.ID_ADRESY_PRAC
                        VALUES
                            (:data_od, :wojewodztwo, :powiat, :gmina, :miejscowosc,
                             :kod_pocztowy, :poczta, :ulica, :nr_domu, :nr_lokalu,
                             :telefon, 1, :id_pracownika, :kraj)
                        """
                    ),
                    address_params,
                ).scalar()
                stats.created_addresses += 1
                if created_address_id is not None:
                    stats.created_address_ids.append(int(created_address_id))

                link_data_od = int(address_params["data_od"])
                existing_link = connection.execute(
                    text(
                        """
                        SELECT 1
                        FROM URZEDY_PRACOWNIKA
                        WHERE ID_PRACOWNIKA = :employee_id
                          AND ID_URZEDU = :urzad_id
                          AND DATA_OD = :data_od
                        """
                    ),
                    {
                        "employee_id": int(employee_id),
                        "urzad_id": int(urzad_id),
                        "data_od": link_data_od,
                    },
                ).first()
                if existing_link is not None:
                    link_data_od = self._next_free_data_od_for_link(
                        connection=connection,
                        employee_id=int(employee_id),
                        urzad_id=int(urzad_id),
                        proposed_data_od=link_data_od,
                    )
                    stats.shifted_link_dates += 1

                new_link_id = self._allocate_next_free_id(used_link_ids, start_value=1)
                connection.execute(
                    text(
                        """
                        INSERT INTO URZEDY_PRACOWNIKA
                            (IDENTYFIKATOR, ID_URZEDU, ID_PRACOWNIKA, DATA_OD)
                        VALUES
                            (:identyfikator, :id_urzedu, :id_pracownika, :data_od)
                        """
                    ),
                    {
                        "identyfikator": new_link_id,
                        "id_urzedu": int(urzad_id),
                        "id_pracownika": int(employee_id),
                        "data_od": link_data_od,
                    },
                )
                stats.created_links += 1
                stats.created_link_ids.append(int(new_link_id))
                _notify_progress(progress_callback, idx, total)
        return stats

    def next_employee_id(self) -> int:
        with self.engine.begin() as connection:
            used_ids = self._load_used_ints_locked(
                connection=connection,
                table_name="PRACOWNIK",
                column_name="ID_PRACOWNIKA",
            )
            return self._allocate_next_free_id(used_ids, start_value=1)

    def next_nr_ewidencyjny(self, id_firmy: int = 1) -> str:
        with self.engine.begin() as connection:
            used_numbers = self._load_used_ints_locked(
                connection=connection,
                table_name="PRACOWNIK",
                column_name="NR_EWIDENCYJNY",
                where_sql=(
                    "ID_FIRMY = :id_firmy "
                    "AND NR_EWIDENCYJNY IS NOT NULL "
                    "AND TRY_CAST(NR_EWIDENCYJNY AS int) IS NOT NULL"
                ),
                params={"id_firmy": id_firmy},
                use_try_cast=True,
            )
            return str(self._allocate_next_free_id(used_numbers, start_value=1))

    def _max_numeric_id_locked(self, connection, table_name: str, column_name: str) -> int:
        query = text(
            f"""
            SELECT ISNULL(MAX({column_name}), 0)
            FROM {table_name} WITH (UPDLOCK, HOLDLOCK)
            """
        )
        return int(connection.execute(query).scalar_one())

    def _resolve_or_create_urzad(
        self,
        connection,
        urzad_name: str,
        urzad_code: str,
        used_urzedy_ids: set[int],
        start_urzad_id: int,
        create_urzad_name: str = "",
        urzad_cache: list[tuple[int, str, str]] | None = None,
        urzad_code_index: dict[str, int] | None = None,
    ) -> tuple[int, bool]:
        if urzad_code:
            found_id: int | None = None
            if urzad_code_index is not None:
                found_id = urzad_code_index.get(urzad_code.strip())
            if found_id is None and urzad_code_index is None:
                row = connection.execute(
                    text(
                        """
                        SELECT TOP 1 ID_URZEDU
                        FROM URZEDY
                        WHERE KOD_US = :kod_us
                        ORDER BY ID_URZEDU
                        """
                    ),
                    {"kod_us": urzad_code},
                ).first()
                if row:
                    found_id = int(row[0])
            if found_id is not None:
                return int(found_id), False

        if urzad_name:
            existing_id = self._find_urzad_id_by_name_connection(
                connection,
                urzad_name,
                urzad_cache=urzad_cache,
            )
            if existing_id is not None:
                existing_code_row = connection.execute(
                    text(
                        """
                        SELECT KOD_US
                        FROM URZEDY
                        WHERE ID_URZEDU = :id_urzedu
                        """
                    ),
                    {"id_urzedu": int(existing_id)},
                ).first()
                existing_code = (
                    str(existing_code_row[0]).strip()
                    if existing_code_row is not None and existing_code_row[0] is not None
                    else ""
                )
                if urzad_code and not existing_code:
                    connection.execute(
                        text(
                            """
                            UPDATE URZEDY
                            SET KOD_US = :kod_us
                            WHERE ID_URZEDU = :id_urzedu
                            """
                        ),
                        {
                            "kod_us": _fit(urzad_code, "URZEDY.KOD_US"),
                            "id_urzedu": existing_id,
                        },
                    )
                    if urzad_code_index is not None:
                        urzad_code_index[urzad_code.strip()] = int(existing_id)
                return existing_id, False

        new_id = self._allocate_next_free_id(used_ids=used_urzedy_ids, start_value=start_urzad_id)
        new_name = _fit(
            create_urzad_name or urzad_name or f"US {urzad_code}",
            "URZEDY.NAZWA",
        )
        connection.execute(
            text(
                """
                INSERT INTO URZEDY (ID_URZEDU, NAZWA, TYP_URZEDU, KOD_US, FLAGA_STANU)
                VALUES (:id_urzedu, :nazwa, :typ_urzedu, :kod_us, 0)
                """
            ),
            {
                "id_urzedu": new_id,
                "nazwa": new_name,
                "typ_urzedu": _fit("US", "URZEDY.TYP_URZEDU"),
                "kod_us": _fit(urzad_code, "URZEDY.KOD_US") if urzad_code else None,
            },
        )
        if urzad_cache is not None:
            urzad_cache.append(
                (int(new_id), new_name, self._normalize_urzad_name_for_match(new_name))
            )
        if urzad_code_index is not None and urzad_code:
            urzad_code_index[urzad_code.strip()] = int(new_id)
        return new_id, True

    def _load_used_pesels(self, connection, id_firmy: int) -> set[str]:
        rows = connection.execute(
            text(
                """
                SELECT PESEL
                FROM PRACOWNIK
                WHERE ID_FIRMY = :id_firmy
                  AND PESEL IS NOT NULL
                  AND LTRIM(RTRIM(PESEL)) <> ''
                """
            ),
            {"id_firmy": int(id_firmy)},
        ).fetchall()
        return {str(row[0]).strip() for row in rows if row[0] is not None}

    def _resolve_firm_insert_params(
        self, connection, id_firmy: int
    ) -> tuple[int, str, int, int]:
        """Return (id_struktury, tree_struktury, id_kalendarza, id_schematu) for a firm.

        Looks up the root node of STRUKTURA_ORGANIZACYJNA, the calendar, and the
        accounting scheme that belong to the given firm.  Falls back to firm-1 values
        when the given firm has no calendar configured yet.
        """
        strukt = connection.execute(
            text(
                "SELECT TOP 1 ID_STRUKTURY, TREE "
                "FROM dbo.STRUKTURA_ORGANIZACYJNA "
                "WHERE ID_FIRMY = :id_firmy "
                "ORDER BY ID_STRUKTURY"
            ),
            {"id_firmy": id_firmy},
        ).first()
        id_struktury = int(strukt[0]) if strukt else 1
        tree_struktury = str(strukt[1]).strip() if strukt else "00001"

        kal = connection.execute(
            text(
                "SELECT TOP 1 ID_KALENDARZA FROM dbo.KALENDARZ "
                "WHERE ID_FIRMY = :id_firmy ORDER BY ID_KALENDARZA"
            ),
            {"id_firmy": id_firmy},
        ).first()
        if not kal:
            kal = connection.execute(
                text("SELECT TOP 1 ID_KALENDARZA FROM dbo.KALENDARZ ORDER BY ID_KALENDARZA")
            ).first()
        id_kalendarza = int(kal[0]) if kal else 1

        schema = connection.execute(
            text(
                "SELECT TOP 1 ID_SCHEMATU FROM dbo.G_SCHEMAT_KSIEGOWANIA "
                "WHERE ID_FIRMY = :id_firmy ORDER BY ID_SCHEMATU"
            ),
            {"id_firmy": id_firmy},
        ).first()
        id_schematu = int(schema[0]) if schema else 1

        return id_struktury, tree_struktury, id_kalendarza, id_schematu

    def _build_urzad_code_index(self, connection) -> dict[str, int]:
        rows = connection.execute(
            text(
                """
                SELECT ID_URZEDU, KOD_US
                FROM URZEDY
                WHERE KOD_US IS NOT NULL AND LTRIM(RTRIM(KOD_US)) <> ''
                """
            )
        ).fetchall()
        index: dict[str, int] = {}
        for row in rows:
            code = str(row[1] or "").strip()
            if not code:
                continue
            index.setdefault(code, int(row[0]))
        return index

    def _normalize_urzad_name_for_match(self, value: str) -> str:
        upper = str(value or "").upper().strip().replace("’", "'")
        no_accents = "".join(
            ch for ch in unicodedata.normalize("NFKD", upper) if not unicodedata.combining(ch)
        )
        no_accents = no_accents.replace("WARSZAWIE", "WARSZAWA").replace("PRADZE", "PRAGA")
        cleaned = re.sub(r"\b(URZAD|SKARBOWY|PIERWSZY|DRUGI|TRZECI|MAZOWIECKI|DOLNOSLASKI)\b", " ", no_accents)
        return re.sub(r"[^A-Z0-9]", "", cleaned)

    def _load_urzedy_cache(self, connection) -> list[tuple[int, str, str]]:
        """Preload all URZEDY once per transaction to avoid N*scan fuzzy-match.

        Returns list of tuples: (id_urzedu, exact_name, normalized_key)
        """
        rows = connection.execute(
            text("SELECT ID_URZEDU, NAZWA FROM URZEDY")
        ).fetchall()
        cache: list[tuple[int, str, str]] = []
        for row in rows:
            urzad_id = int(row[0])
            name = str(row[1] or "")
            normalized = self._normalize_urzad_name_for_match(name)
            cache.append((urzad_id, name, normalized))
        return cache

    def _find_urzad_id_in_cache(
        self,
        urzad_cache: list[tuple[int, str, str]] | None,
        urzad_name: str,
    ) -> int | None:
        if not urzad_cache:
            return None
        stripped = str(urzad_name or "").strip()
        if not stripped:
            return None
        # exact name match (case/space-insensitive, accents-sensitive enough for DB)
        lowered = stripped.casefold()
        for urzad_id, exact_name, _ in urzad_cache:
            if exact_name.strip().casefold() == lowered:
                return urzad_id

        target = self._normalize_urzad_name_for_match(stripped)
        if not target:
            return None
        best_id: int | None = None
        best_ratio = 0.0
        for urzad_id, _, normalized in urzad_cache:
            if not normalized:
                continue
            if normalized == target:
                return urzad_id
            ratio = SequenceMatcher(None, target, normalized).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_id = urzad_id
        if best_id is not None and best_ratio >= 0.9:
            return best_id
        return None

    def _find_urzad_id_by_name_connection(
        self,
        connection,
        urzad_name: str,
        urzad_cache: list[tuple[int, str, str]] | None = None,
    ) -> int | None:
        if urzad_cache is not None:
            return self._find_urzad_id_in_cache(urzad_cache, urzad_name)

        exact = connection.execute(
            text(
                """
                SELECT TOP 1 ID_URZEDU
                FROM URZEDY
                WHERE UPPER(LTRIM(RTRIM(NAZWA))) COLLATE Polish_CI_AI
                      = UPPER(LTRIM(RTRIM(:urzad_name))) COLLATE Polish_CI_AI
                ORDER BY ID_URZEDU
                """
            ),
            {"urzad_name": urzad_name},
        ).first()
        if exact:
            return int(exact[0])

        target = self._normalize_urzad_name_for_match(urzad_name)
        if not target:
            return None
        rows = connection.execute(text("SELECT ID_URZEDU, NAZWA FROM URZEDY")).fetchall()
        best_id: int | None = None
        best_ratio = 0.0
        for row in rows:
            candidate_id = int(row[0])
            candidate_name = str(row[1] or "")
            candidate_norm = self._normalize_urzad_name_for_match(candidate_name)
            if not candidate_norm:
                continue
            if candidate_norm == target:
                return candidate_id
            ratio = SequenceMatcher(None, target, candidate_norm).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_id = candidate_id
        if best_id is not None and best_ratio >= 0.9:
            return best_id
        return None

    def _ensure_urzad_code(self, connection, urzad_id: int, urzad_code: str) -> None:
        code = _fit(str(urzad_code or "").strip(), "URZEDY.KOD_US")
        if not code:
            return
        row = connection.execute(
            text(
                """
                SELECT KOD_US
                FROM URZEDY
                WHERE ID_URZEDU = :id_urzedu
                """
            ),
            {"id_urzedu": int(urzad_id)},
        ).first()
        if row is None:
            return
        existing_code = str(row[0]).strip() if row[0] is not None else ""
        if existing_code:
            return
        connection.execute(
            text(
                """
                UPDATE URZEDY
                SET KOD_US = :kod_us
                WHERE ID_URZEDU = :id_urzedu
                """
            ),
            {"kod_us": code, "id_urzedu": int(urzad_id)},
        )

    def _load_used_ints_locked(
        self,
        connection,
        table_name: str,
        column_name: str,
        where_sql: str | None = None,
        params: dict | None = None,
        min_value: int | None = None,
        use_try_cast: bool = False,
    ) -> set[int]:
        if use_try_cast:
            value_expr = f"TRY_CAST({column_name} AS int)"
            not_null_check = f"TRY_CAST({column_name} AS int) IS NOT NULL"
        else:
            value_expr = f"CAST({column_name} AS int)"
            not_null_check = f"{column_name} IS NOT NULL"

        clauses = [not_null_check]
        if where_sql:
            clauses.append(where_sql)
        if min_value is not None:
            clauses.append(f"{value_expr} >= :_min_value")

        query = text(
            f"""
            SELECT {value_expr} AS value_id
            FROM {table_name} WITH (UPDLOCK, HOLDLOCK)
            WHERE {' AND '.join(clauses)}
            """
        )
        query_params = dict(params or {})
        if min_value is not None:
            query_params["_min_value"] = int(min_value)

        values = connection.execute(query, query_params).fetchall()
        return {int(row[0]) for row in values if row[0] is not None}

    def _allocate_next_free_id(self, used_ids: set[int], start_value: int = 1) -> int:
        candidate = max(1, int(start_value))
        while candidate in used_ids:
            candidate += 1
        used_ids.add(candidate)
        return candidate

    def _next_free_data_od_for_address(
        self,
        connection,
        employee_id: int,
        proposed_data_od: int,
    ) -> int:
        rows = connection.execute(
            text(
                """
                SELECT DATA_OD
                FROM ADRESY_PRACOWNIKA WITH (UPDLOCK, HOLDLOCK)
                WHERE ID_PRACOWNIKA = :employee_id
                """
            ),
            {"employee_id": employee_id},
        ).fetchall()
        used = {int(r[0]) for r in rows if r[0] is not None}
        candidate = int(proposed_data_od)
        while candidate in used:
            candidate += 1
        return candidate

    def _next_free_data_od_for_link(
        self,
        connection,
        employee_id: int,
        urzad_id: int,
        proposed_data_od: int,
    ) -> int:
        rows = connection.execute(
            text(
                """
                SELECT DATA_OD
                FROM URZEDY_PRACOWNIKA WITH (UPDLOCK, HOLDLOCK)
                WHERE ID_PRACOWNIKA = :employee_id
                  AND ID_URZEDU = :urzad_id
                """
            ),
            {"employee_id": employee_id, "urzad_id": urzad_id},
        ).fetchall()
        used = {int(r[0]) for r in rows if r[0] is not None}
        candidate = int(proposed_data_od)
        while candidate in used:
            candidate += 1
        return candidate

    def _split_full_name(self, full_name: str) -> tuple[str, str]:
        parts = [p for p in full_name.split() if p]
        if not parts:
            return ("BRAK", "BRAK")
        if len(parts) == 1:
            return (_fit(parts[0], "PRACOWNIK.NAZWISKO"), "")
        return (
            _fit(" ".join(parts[:-1]), "PRACOWNIK.NAZWISKO"),
            _fit(parts[-1], "PRACOWNIK.IMIE_1"),
        )

    def _parse_excel_date_to_clarion(self, value: str) -> Optional[int]:
        if not value:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        # Reuse the hardened parser from importer.utils so a bare integer
        # (e.g. an Excel serial like "46068") can't be silently mis-classified
        # as an already-Clarion day count and shift the date by ~99 years.
        from importer.utils import _to_clarion_date

        return _to_clarion_date(stripped)

    def undo_import(self, created_link_ids: list[int], created_urzedy_ids: list[int]) -> UndoStats:
        return self.undo_import_record(
            {
                "created_link_ids": created_link_ids,
                "created_urzedy_ids": created_urzedy_ids,
            }
        )

    def undo_import_record(self, history_record: dict) -> UndoStats:
        stats = UndoStats()
        created_link_ids = history_record.get("created_link_ids", []) or []
        created_urzedy_ids = history_record.get("created_urzedy_ids", []) or []
        created_address_ids = history_record.get("created_address_ids", []) or []
        created_employee_ids = history_record.get("created_employee_ids", []) or []
        created_contract_ids = history_record.get("created_contract_ids", []) or []
        created_insurance_ids = history_record.get("created_insurance_ids", []) or []
        with self.engine.begin() as connection:
            if created_insurance_ids:
                for insurance_id in created_insurance_ids:
                    deleted = connection.execute(
                        text(
                            """
                            DELETE FROM GANG_UBEZPIECZENIA_OBOWIAZKOWE
                            WHERE IDENTYFIKATOR = :id
                            """
                        ),
                        {"id": int(insurance_id)},
                    ).rowcount or 0
                    stats.deleted_insurance_rows += int(deleted)

            if created_contract_ids:
                for contract_id in created_contract_ids:
                    deleted = connection.execute(
                        text(
                            """
                            DELETE FROM GANG_UMOWY_CYWILNO_PRAWNE
                            WHERE IDENTYFIKATOR = :id
                            """
                        ),
                        {"id": int(contract_id)},
                    ).rowcount or 0
                    stats.deleted_contracts += int(deleted)

            if created_link_ids:
                for link_id in created_link_ids:
                    deleted = connection.execute(
                        text(
                            """
                            DELETE FROM URZEDY_PRACOWNIKA
                            WHERE IDENTYFIKATOR = :link_id
                            """
                        ),
                        {"link_id": int(link_id)},
                    ).rowcount or 0
                    stats.deleted_links += int(deleted)

            if created_address_ids:
                for address_id in created_address_ids:
                    deleted = connection.execute(
                        text(
                            """
                            DELETE FROM ADRESY_PRACOWNIKA
                            WHERE ID_ADRESY_PRAC = :id
                            """
                        ),
                        {"id": int(address_id)},
                    ).rowcount or 0
                    stats.deleted_addresses += int(deleted)

            if created_employee_ids:
                for employee_id in created_employee_ids:
                    deleted = connection.execute(
                        text(
                            """
                            DELETE FROM PRACOWNIK
                            WHERE ID_PRACOWNIKA = :id
                            """
                        ),
                        {"id": int(employee_id)},
                    ).rowcount or 0
                    stats.deleted_employees += int(deleted)

            if created_urzedy_ids:
                for urzad_id in created_urzedy_ids:
                    has_links = connection.execute(
                        text(
                            """
                            SELECT 1
                            FROM URZEDY_PRACOWNIKA
                            WHERE ID_URZEDU = :urzad_id
                            """
                        ),
                        {"urzad_id": int(urzad_id)},
                    ).first()
                    has_employees = connection.execute(
                        text(
                            """
                            SELECT 1
                            FROM PRACOWNIK
                            WHERE ID_US = :urzad_id
                            """
                        ),
                        {"urzad_id": int(urzad_id)},
                    ).first()
                    has_addresses = connection.execute(
                        text(
                            """
                            SELECT 1
                            FROM ADRESY_PRACOWNIKA
                            WHERE KP_ID_URZEDU_SKARBOWEGO = :urzad_id
                            """
                        ),
                        {"urzad_id": int(urzad_id)},
                    ).first()
                    if has_links or has_employees or has_addresses:
                        stats.skipped_urzedy += 1
                        continue
                    deleted = connection.execute(
                        text(
                            """
                            DELETE FROM URZEDY
                            WHERE ID_URZEDU = :urzad_id
                            """
                        ),
                        {"urzad_id": int(urzad_id)},
                    ).rowcount or 0
                    stats.deleted_urzedy += int(deleted)
        return stats

    def verify_umowy_financials(
        self,
        tolerance: float = 0.005,
        progress_callback: ProgressCallback | None = None,
        cancel_token: CancelToken | None = None,
    ) -> UmowyVerificationReport:
        """Weryfikuje poprawność kwot finansowych w całej tabeli GANG_UMOWY_CYWILNO_PRAWNE.

        Algorytm:
          1. Odczytuje wszystkie rekordy umów cywilnoprawnych z BD.
          2. Dla każdego rekordu przelicza wartości finansowe (emerytalne, rentowe,
             chorobowe, zdrowotne, KUP, PIT, netto) od nowa — używając stawek
             procent przechowywanych w BD i precyzji Decimal (moduł tax_calc_2026).
          3. Porównuje przeliczone wartości z wartościami zapisanymi w BD.
             Rozbieżność ≥ tolerance PLN jest klasyfikowana jako błąd.
          4. Opcjonalnie zgłasza ostrzeżenia, gdy stawki w BD odbiegają
             od standardowych wartości 2026 (np. emerytalne ≠ 19,52%).

        Parametry:
            tolerance: maksymalna dopuszczalna różnica (PLN) — domyślnie 0,005 PLN
                (pół grosza), co odpowiada granicy precyzji groszowej.
            progress_callback: opcjonalny callback (done, total).

        Zwraca:
            UmowyVerificationReport z listą wszystkich niezgodności.
        """
        report = UmowyVerificationReport()

        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT
                        IDENTYFIKATOR,
                        ID_NADRZEDNEGO,
                        NUMER_UMOWY,
                        RODZAJ_UMOWY,
                        WYNAGRODZENIE_BRUTTO,
                        KOSZTY_UZYSKANIA____,
                        STAWKA_PODATKU____,
                        EMERYTALNE____,
                        RENTOWE_U____,
                        RENTOWE____,
                        CHOROBOWE____,
                        WYPADKOWE____,
                        ZDROWOTNE____,
                        FP____,
                        FGSP____,
                        KWOTA_PODATKU,
                        KWOTA_DO_WYPLATY,
                        KOSZTY_UZYSKANIA__KWOTA_,
                        DOCHOD,
                        EMERYTALNE_ZLECENIOBIORCA,
                        EMERYTALNE_ZLECENIODAWCA,
                        RENTOWE_ZLECENIOBIORCA,
                        RENTOWE_ZLECENIODAWCA,
                        CHOROBOWE_ZLECENIOBIORCA,
                        WYPADKOWE_ZLECENIODAWCA,
                        ZDROWOTNE_ZLECENIOBIORCA,
                        FP,
                        FGSP
                    FROM GANG_UMOWY_CYWILNO_PRAWNE
                    ORDER BY IDENTYFIKATOR
                    """
                )
            ).fetchall()

        total = len(rows)
        _notify_progress(progress_callback, 0, total)

        for idx, row in enumerate(rows, start=1):
            _raise_if_cancelled(cancel_token)
            report.checked += 1

            identyfikator = int(row[0])
            employee_id = int(row[1])
            numer_umowy = str(row[2] or "")
            rodzaj_umowy = str(row[3] or "1")
            brutto = float(row[4] or 0)
            kup_proc = float(row[5] or 0)
            stawka_pit = float(row[6] or 0)
            emer_proc = float(row[7] or 0)
            rent_u_proc = float(row[8] or 0)
            rent_p_proc = float(row[9] or 0)
            chor_proc = float(row[10] or 0)
            wypad_proc = float(row[11] or 0)
            zdr_proc = float(row[12] or 0)
            fp_proc = float(row[13] or 0)
            fgsp_proc = float(row[14] or 0)

            # Wartości przechowywane w BD (kolumny 15–27)
            stored: dict[str, float] = {
                "KWOTA_PODATKU":             float(row[15] or 0),
                "KWOTA_DO_WYPLATY":          float(row[16] or 0),
                "KOSZTY_UZYSKANIA__KWOTA_":  float(row[17] or 0),
                "DOCHOD":                    float(row[18] or 0),
                "EMERYTALNE_ZLECENIOBIORCA": float(row[19] or 0),
                "EMERYTALNE_ZLECENIODAWCA":  float(row[20] or 0),
                "RENTOWE_ZLECENIOBIORCA":    float(row[21] or 0),
                "RENTOWE_ZLECENIODAWCA":     float(row[22] or 0),
                "CHOROBOWE_ZLECENIOBIORCA":  float(row[23] or 0),
                "WYPADKOWE_ZLECENIODAWCA":   float(row[24] or 0),
                "ZDROWOTNE_ZLECENIOBIORCA":  float(row[25] or 0),
                "FP":                        float(row[26] or 0),
                "FGSP":                      float(row[27] or 0),
            }

            # Przelicz od nowa używając modułu tax_calc_2026
            try:
                is_dzielo = rodzaj_umowy.strip() == "2"
                if is_dzielo:
                    recalc = recalculate_umowa_dzielo_from_rates(
                        brutto=brutto,
                        kup_proc=kup_proc,
                        stawka_podatku_proc=stawka_pit if stawka_pit > 0 else 12.0,
                    )
                else:
                    recalc = recalculate_umowa_zlecenie_from_rates(
                        brutto=brutto,
                        kup_proc=kup_proc,
                        stawka_podatku_proc=stawka_pit,
                        emerytalne_proc=emer_proc,
                        rentowe_u_proc=rent_u_proc,
                        rentowe_p_proc=rent_p_proc,
                        chorobowe_proc=chor_proc,
                        wypadkowe_proc=wypad_proc,
                        zdrowotne_proc=zdr_proc,
                        fp_proc=fp_proc,
                        fgsp_proc=fgsp_proc,
                    )
            except Exception:
                # Błąd przeliczenia → traktujemy jako issue bez deltas
                issue = UmowaVerificationIssue(
                    identyfikator=identyfikator,
                    employee_id=employee_id,
                    numer_umowy=numer_umowy,
                    brutto=brutto,
                    rodzaj_umowy=rodzaj_umowy,
                    rate_warnings=["Błąd przeliczenia (wyjątek w recalculate)"],
                    stored_emerytalne_proc=emer_proc,
                    stored_zdrowotne_proc=zdr_proc,
                    stored_stawka_pit=stawka_pit,
                )
                report.with_issues += 1
                report.issues.append(issue)
                _notify_progress(progress_callback, idx, total)
                continue

            # Porównaj pole po polu
            deltas: list[UmowaFieldDelta] = []
            for db_col, recalc_field in DB_TO_RECALC_FIELD_MAP.items():
                stored_val = stored[db_col]
                expected_val = float(getattr(recalc, recalc_field))
                delta = stored_val - expected_val
                if abs(delta) >= tolerance:
                    deltas.append(
                        UmowaFieldDelta(
                            field=db_col,
                            stored=stored_val,
                            expected=expected_val,
                            delta=delta,
                        )
                    )

            # Sprawdź odchylenia stawek od standardowych wartości 2026
            rate_warnings: list[str] = []
            if not is_dzielo:
                stored_rates = {
                    "EMERYTALNE____": emer_proc,
                    "RENTOWE_U____": rent_u_proc,
                    "RENTOWE____": rent_p_proc,
                    "CHOROBOWE____": chor_proc,
                    "ZDROWOTNE____": zdr_proc,
                }
                for col, standard in STANDARD_RATES_2026.items():
                    actual = stored_rates.get(col, 0.0)
                    # Pomiń stawki = 0 (student / ulga na start / zbieg tytułów)
                    if actual == 0.0:
                        continue
                    if abs(actual - standard) >= 0.01:
                        rate_warnings.append(
                            f"{col}: BD={actual:.4f}%, standard 2026={standard:.2f}%"
                        )

            if deltas or rate_warnings:
                report.with_issues += 1
                report.issues.append(
                    UmowaVerificationIssue(
                        identyfikator=identyfikator,
                        employee_id=employee_id,
                        numer_umowy=numer_umowy,
                        brutto=brutto,
                        rodzaj_umowy=rodzaj_umowy,
                        deltas=deltas,
                        rate_warnings=rate_warnings,
                        stored_emerytalne_proc=emer_proc,
                        stored_zdrowotne_proc=zdr_proc,
                        stored_stawka_pit=stawka_pit,
                    )
                )
            else:
                report.ok += 1

            _notify_progress(progress_callback, idx, total)

        return report

    def update_employee_status_all(self, from_status: int, to_status: int) -> StatusUpdateStats:
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE PRACOWNIK
                    SET RODZAJ_PRACOWNIKA = :to_status
                    WHERE RODZAJ_PRACOWNIKA = :from_status
                    """
                ),
                {"from_status": from_status, "to_status": to_status},
            )
            return StatusUpdateStats(updated=int(result.rowcount or 0))

    def count_employee_status_all(self, from_status: int) -> int:
        with self.engine.connect() as connection:
            value = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM PRACOWNIK
                    WHERE RODZAJ_PRACOWNIKA = :from_status
                    """
                ),
                {"from_status": from_status},
            ).scalar_one()
        return int(value or 0)

    def preview_employee_status_by_numbers(
        self,
        nr_ewidencyjne: list[str],
        from_status: int,
    ) -> StatusUpdateStats:
        stats = StatusUpdateStats()
        if not nr_ewidencyjne:
            return stats
        with self.engine.connect() as connection:
            for number in nr_ewidencyjne:
                row = connection.execute(
                    text(
                        """
                        SELECT TOP 1 RODZAJ_PRACOWNIKA
                        FROM PRACOWNIK
                        WHERE NR_EWIDENCYJNY = :nr
                        """
                    ),
                    {"nr": number},
                ).first()
                if row is None:
                    stats.not_found += 1
                    continue
                current_status = int(row[0]) if row[0] is not None else 0
                if current_status != from_status:
                    stats.unchanged += 1
                    continue
                stats.updated += 1
        return stats

    def update_employee_status_by_numbers(
        self, nr_ewidencyjne: list[str], from_status: int, to_status: int
    ) -> StatusUpdateStats:
        stats = StatusUpdateStats()
        if not nr_ewidencyjne:
            return stats

        with self.engine.begin() as connection:
            for number in nr_ewidencyjne:
                row = connection.execute(
                    text(
                        """
                        SELECT TOP 1 RODZAJ_PRACOWNIKA
                        FROM PRACOWNIK
                        WHERE NR_EWIDENCYJNY = :nr
                        """
                    ),
                    {"nr": number},
                ).first()
                if row is None:
                    stats.not_found += 1
                    continue

                current_status = int(row[0]) if row[0] is not None else 0
                if current_status != from_status:
                    stats.unchanged += 1
                    continue

                result = connection.execute(
                    text(
                        """
                        UPDATE PRACOWNIK
                        SET RODZAJ_PRACOWNIKA = :to_status
                        WHERE NR_EWIDENCYJNY = :nr
                          AND RODZAJ_PRACOWNIKA = :from_status
                        """
                    ),
                    {"to_status": to_status, "nr": number, "from_status": from_status},
                )
                stats.updated += int(result.rowcount or 0)
        return stats
