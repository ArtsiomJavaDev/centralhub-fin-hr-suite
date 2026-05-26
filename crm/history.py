"""Import history store — JSON-based, one record per automation run.

File location: LogsAutomatization/import_history.json
Each record captures all information needed to:
  - audit past runs
  - rollback any single import (via contract IDs)
  - detect duplicate period imports
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

_HISTORY_DIR = Path(__file__).resolve().parent.parent / "LogsAutomatization"
_HISTORY_FILE = _HISTORY_DIR / "import_history.json"


@dataclass
class ImportHistoryRecord:
    """One automation-pipeline run."""

    record_id: str
    timestamp: str                  # ISO-8601
    source_file: str                # original filename (basename)
    period_wyplaty_min: str         # "MM/YYYY" or ""
    period_wyplaty_max: str
    total_rows: int
    ud_count: int
    uz_count: int
    created_contracts: int
    skipped_duplicates: int
    missing_employees: int
    verify_ok: int
    verify_marginal: int
    verify_discrepancy: int
    contract_ids: list[int] = field(default_factory=list)
    log_file_path: str = ""
    rolledback: bool = False        # True after undo_import_record succeeds

    @staticmethod
    def new(
        source_file: str,
        period_wyplaty_min: str,
        period_wyplaty_max: str,
        total_rows: int,
        ud_count: int,
        uz_count: int,
        created_contracts: int,
        skipped_duplicates: int,
        missing_employees: int,
        verify_ok: int,
        verify_marginal: int,
        verify_discrepancy: int,
        contract_ids: list[int],
        log_file_path: str,
    ) -> "ImportHistoryRecord":
        return ImportHistoryRecord(
            record_id=str(uuid.uuid4()),
            timestamp=datetime.now().isoformat(timespec="seconds"),
            source_file=source_file,
            period_wyplaty_min=period_wyplaty_min,
            period_wyplaty_max=period_wyplaty_max,
            total_rows=total_rows,
            ud_count=ud_count,
            uz_count=uz_count,
            created_contracts=created_contracts,
            skipped_duplicates=skipped_duplicates,
            missing_employees=missing_employees,
            verify_ok=verify_ok,
            verify_marginal=verify_marginal,
            verify_discrepancy=verify_discrepancy,
            contract_ids=contract_ids,
            log_file_path=log_file_path,
            rolledback=False,
        )

    @property
    def timestamp_display(self) -> str:
        try:
            dt = datetime.fromisoformat(self.timestamp)
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return self.timestamp

    @property
    def period_display(self) -> str:
        if self.period_wyplaty_min and self.period_wyplaty_max:
            if self.period_wyplaty_min == self.period_wyplaty_max:
                return self.period_wyplaty_min
            return f"{self.period_wyplaty_min} – {self.period_wyplaty_max}"
        return "—"

    @property
    def verify_status(self) -> str:
        if self.verify_discrepancy > 0:
            return f"⚠ {self.verify_discrepancy} niezgodności"
        if self.verify_marginal > 0:
            return f"~ {self.verify_marginal} marginalne"
        if self.verify_ok > 0:
            return "✔ OK"
        return "—"

    @property
    def can_rollback(self) -> bool:
        return not self.rolledback and bool(self.contract_ids)


# ─── Persistence ──────────────────────────────────────────────────────────────

def _load_raw() -> list[dict]:
    if not _HISTORY_FILE.exists():
        return []
    try:
        with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_raw(records: list[dict]) -> None:
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def _dict_to_record(d: dict) -> Optional[ImportHistoryRecord]:
    try:
        return ImportHistoryRecord(
            record_id=d.get("record_id", ""),
            timestamp=d.get("timestamp", ""),
            source_file=d.get("source_file", ""),
            period_wyplaty_min=d.get("period_wyplaty_min", ""),
            period_wyplaty_max=d.get("period_wyplaty_max", ""),
            total_rows=int(d.get("total_rows", 0)),
            ud_count=int(d.get("ud_count", 0)),
            uz_count=int(d.get("uz_count", 0)),
            created_contracts=int(d.get("created_contracts", 0)),
            skipped_duplicates=int(d.get("skipped_duplicates", 0)),
            missing_employees=int(d.get("missing_employees", 0)),
            verify_ok=int(d.get("verify_ok", 0)),
            verify_marginal=int(d.get("verify_marginal", 0)),
            verify_discrepancy=int(d.get("verify_discrepancy", 0)),
            contract_ids=list(d.get("contract_ids", [])),
            log_file_path=d.get("log_file_path", ""),
            rolledback=bool(d.get("rolledback", False)),
        )
    except Exception:
        return None


def save_record(record: ImportHistoryRecord) -> None:
    """Append a new record (or update existing by record_id) to history file."""
    raw = _load_raw()
    # Update if already present (e.g. after mark_rolledback)
    for i, item in enumerate(raw):
        if item.get("record_id") == record.record_id:
            raw[i] = asdict(record)
            _save_raw(raw)
            return
    raw.insert(0, asdict(record))  # newest first
    _save_raw(raw)


def load_records() -> list[ImportHistoryRecord]:
    """Load all records, newest first. Skips malformed entries."""
    raw = _load_raw()
    result = []
    for d in raw:
        rec = _dict_to_record(d)
        if rec is not None:
            result.append(rec)
    return result


def get_record(record_id: str) -> Optional[ImportHistoryRecord]:
    for d in _load_raw():
        if d.get("record_id") == record_id:
            return _dict_to_record(d)
    return None


def mark_rolledback(record_id: str) -> None:
    """Mark a history record as rolled back so it can't be rolled back twice."""
    raw = _load_raw()
    for item in raw:
        if item.get("record_id") == record_id:
            item["rolledback"] = True
            item["contract_ids"] = []
            break
    _save_raw(raw)
