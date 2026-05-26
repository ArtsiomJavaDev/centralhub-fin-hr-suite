from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PreflightReport:
    missing_tables: list[str] = field(default_factory=list)
    permission_warnings: list[str] = field(default_factory=list)

    @property
    def is_ok(self) -> bool:
        return not self.missing_tables
