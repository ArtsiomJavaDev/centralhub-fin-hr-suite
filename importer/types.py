from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RowStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class ValidationRow:
    index: int
    status: RowStatus
    message: str
    field_name: str | None = None


@dataclass
class CheckInResult:
    rows: list[ValidationRow]
    to_create_urzedy: int
    to_create_links: int
    skipped_links: int
    errors: int
    importable_rows: list[dict[str, Any]]
    # Urząd names that appeared in the Excel file but were found neither in
    # the database nor in urzedy_reference.json. Populated by check-ins that
    # handle a "nazwa Urząd Skarbowy" column so the UI can show one
    # consolidated hint instead of many per-row errors.
    missing_urzedy: list[str] = field(default_factory=list)
