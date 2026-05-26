from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .config import DbConfig
from .service import DatabaseService

WORKING_TABLES: tuple[str, ...] = (
    "PRACOWNIK",
    "URZEDY",
    "URZEDY_PRACOWNIKA",
    "ADRESY_PRACOWNIKA",
    "GANG_UMOWY_CYWILNO_PRAWNE",
)


@dataclass(frozen=True)
class WorkingTableInfo:
    schema_name: str
    table_name: str
    row_count: int


@dataclass(frozen=True)
class WorkingRelationInfo:
    fk_name: str
    parent_table: str
    child_table: str
    parent_column: str
    child_column: str


@dataclass(frozen=True)
class WorkingSchemaSnapshot:
    tables: list[WorkingTableInfo]
    relations: list[WorkingRelationInfo]


class DatabaseIntrospectionService:
    def __init__(self, config: DbConfig) -> None:
        self._db_service = DatabaseService(config)

    @property
    def engine(self) -> Engine:
        return self._db_service.engine

    def load_working_snapshot(self) -> WorkingSchemaSnapshot:
        return WorkingSchemaSnapshot(
            tables=self._load_working_tables(),
            relations=self._load_working_relations(),
        )

    def load_table_preview(
        self,
        schema_name: str,
        table_name: str,
        limit: int = 100,
    ) -> tuple[list[str], list[tuple]]:
        if not _is_safe_sql_identifier(schema_name):
            raise ValueError(f"Недопустимое имя схемы: {schema_name}")
        if not _is_safe_sql_identifier(table_name):
            raise ValueError(f"Недопустимое имя таблицы: {table_name}")

        safe_limit = max(1, min(int(limit), 500))
        query = text(f"SELECT TOP ({safe_limit}) * FROM [{schema_name}].[{table_name}]")
        with self.engine.connect() as connection:
            result = connection.execute(query)
            columns = list(result.keys())
            rows = [tuple(row) for row in result.fetchall()]
        return columns, rows

    def _load_working_tables(self) -> list[WorkingTableInfo]:
        sql_list = _quoted_sql_list(WORKING_TABLES)
        query = text(
            f"""
            SELECT
                s.name AS schema_name,
                t.name AS table_name,
                CAST(ISNULL(SUM(ps.row_count), 0) AS bigint) AS row_count
            FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            LEFT JOIN sys.dm_db_partition_stats ps
                ON ps.object_id = t.object_id
               AND ps.index_id IN (0, 1)
            WHERE t.name IN ({sql_list})
            GROUP BY s.name, t.name
            ORDER BY t.name
            """
        )
        with self.engine.connect() as connection:
            rows = connection.execute(query).all()
        return [
            WorkingTableInfo(
                schema_name=str(row.schema_name),
                table_name=str(row.table_name),
                row_count=int(row.row_count or 0),
            )
            for row in rows
        ]

    def _load_working_relations(self) -> list[WorkingRelationInfo]:
        sql_list = _quoted_sql_list(WORKING_TABLES)
        query = text(
            f"""
            SELECT
                fk.name AS fk_name,
                rs.name AS parent_schema,
                rt.name AS parent_table,
                rc.name AS parent_column,
                cs.name AS child_schema,
                ct.name AS child_table,
                cc.name AS child_column
            FROM sys.foreign_key_columns fkc
            JOIN sys.foreign_keys fk
                ON fk.object_id = fkc.constraint_object_id
            JOIN sys.tables ct
                ON ct.object_id = fkc.parent_object_id
            JOIN sys.schemas cs
                ON cs.schema_id = ct.schema_id
            JOIN sys.columns cc
                ON cc.object_id = ct.object_id
               AND cc.column_id = fkc.parent_column_id
            JOIN sys.tables rt
                ON rt.object_id = fkc.referenced_object_id
            JOIN sys.schemas rs
                ON rs.schema_id = rt.schema_id
            JOIN sys.columns rc
                ON rc.object_id = rt.object_id
               AND rc.column_id = fkc.referenced_column_id
            WHERE ct.name IN ({sql_list})
              AND rt.name IN ({sql_list})
            ORDER BY fk.name, ct.name, cc.column_id
            """
        )
        with self.engine.connect() as connection:
            rows = connection.execute(query).all()
        return [
            WorkingRelationInfo(
                fk_name=str(row.fk_name),
                parent_table=f"{row.parent_schema}.{row.parent_table}",
                child_table=f"{row.child_schema}.{row.child_table}",
                parent_column=str(row.parent_column),
                child_column=str(row.child_column),
            )
            for row in rows
        ]


def _quoted_sql_list(values: tuple[str, ...]) -> str:
    if not values:
        return "''"
    quoted_values = []
    for value in values:
        escaped = value.replace("'", "''")
        quoted_values.append("'" + escaped + "'")
    return ", ".join(quoted_values)


def _is_safe_sql_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_]+", value or ""))
