"""Top-level importer package.

This package replaces the old monolithic importer.py. Structure:
- importer.types      — RowStatus, ValidationRow, CheckInResult
- importer.profiles   — ImportProfile + all profile constants
- importer.utils      — normalizers, parsers, urzedy reference, ADDRESS_FIELD_LIMITS
- importer.mapping    — Excel -> DataFrame mapping per profile
- importer.checkin    — per-profile check-in + dispatcher `check_in`

Public API here is kept backwards-compatible with old
`from importer import ...` usages.
"""

from __future__ import annotations

from .checkin import (
    check_in,
    check_in_employee_addresses,
    check_in_employees,
    check_in_przeprowadzki,
    check_in_ubezpieczenia_obowiazkowe,
    check_in_umowy,
    check_in_umowy_dzielo,
    check_in_urzedy_links,
    summarize_result,
)
from .mapping import (
    map_columns,
    map_employee_address_columns,
    map_employee_columns,
    map_legacy_urzedy_columns,
    map_przeprowadzki_columns,
    map_ubezpieczenia_obowiazkowe_columns,
    map_umowy_columns,
    map_umowy_dzielo_columns,
    preview_dataframe,
    read_excel,
    read_excel_umowy_format,
)
from .profiles import (
    AVAILABLE_PROFILES,
    EMPLOYEE_ADDRESS_IMPORT_PROFILE,
    EMPLOYEE_IMPORT_PROFILE,
    ImportProfile,
    LEGACY_URZEDY_PROFILE,
    PRZEPROWADZKI_IMPORT_PROFILE,
    UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE,
    UMOWY_DZIELO_IMPORT_PROFILE,
    UMOWY_IMPORT_PROFILE,
    UMOWY_MIXED_IMPORT_PROFILE,
    _effective_required_fields,
)
from .types import CheckInResult, RowStatus, ValidationRow
from .utils import (
    ADDRESS_FIELD_LIMITS,
    URZEDY_REFERENCE_PATH,
    _address_key_to_field_name,
    _clarion_year,
    _normalize_typ_ubezpieczenia,
    _normalize_typ_umowy,
    _normalize_urzad_name,
    _resolve_data_od,
    _to_bool_int,
    _to_clarion_date,
    _to_float,
    _to_int,
    _urzad_match_key,
    _urzad_name_variants,
    load_urzedy_reference,
    load_urzedy_reference_entries,
    resolve_urzad_code,
    resolve_urzad_reference_entry,
)

__all__ = [
    # types
    "RowStatus",
    "ValidationRow",
    "CheckInResult",
    # profiles
    "ImportProfile",
    "AVAILABLE_PROFILES",
    "EMPLOYEE_IMPORT_PROFILE",
    "EMPLOYEE_ADDRESS_IMPORT_PROFILE",
    "LEGACY_URZEDY_PROFILE",
    "UMOWY_IMPORT_PROFILE",
    "UMOWY_DZIELO_IMPORT_PROFILE",
    "UMOWY_MIXED_IMPORT_PROFILE",
    "UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE",
    "PRZEPROWADZKI_IMPORT_PROFILE",
    # mapping
    "read_excel",
    "read_excel_umowy_format",
    "preview_dataframe",
    "map_columns",
    "map_employee_columns",
    "map_legacy_urzedy_columns",
    "map_employee_address_columns",
    "map_umowy_columns",
    "map_umowy_dzielo_columns",
    "map_ubezpieczenia_obowiazkowe_columns",
    "map_przeprowadzki_columns",
    # checkin
    "check_in",
    "check_in_employees",
    "check_in_urzedy_links",
    "check_in_employee_addresses",
    "check_in_umowy",
    "check_in_umowy_dzielo",
    "check_in_ubezpieczenia_obowiazkowe",
    "check_in_przeprowadzki",
    "summarize_result",
    # utils / constants
    "ADDRESS_FIELD_LIMITS",
    "URZEDY_REFERENCE_PATH",
    "load_urzedy_reference",
    "load_urzedy_reference_entries",
    "resolve_urzad_code",
    "resolve_urzad_reference_entry",
    "_normalize_urzad_name",
    "_urzad_name_variants",
    "_urzad_match_key",
    "_to_clarion_date",
    "_clarion_year",
    "_resolve_data_od",
    "_to_float",
    "_to_int",
    "_normalize_typ_ubezpieczenia",
    "_normalize_typ_umowy",
    "_to_bool_int",
    "_address_key_to_field_name",
    "_effective_required_fields",
]
