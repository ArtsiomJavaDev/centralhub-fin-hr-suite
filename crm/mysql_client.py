"""MySQL client for CRM (via SSH tunnel)."""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd
import pymysql

from crm.settings import CrmSettings
from crm.tunnel import SshTunnel


class CrmMysqlError(RuntimeError):
    pass


def connect_mysql(settings: CrmSettings):
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.local_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_database,
        charset="utf8mb4",
        connect_timeout=15,
        read_timeout=120,
        cursorclass=pymysql.cursors.DictCursor,
    )


def test_connection(settings: CrmSettings) -> tuple[bool, str]:
    """Open SSH tunnel, ping MySQL, return (ok, message)."""
    errors = settings.validate()
    if errors:
        return False, "; ".join(errors)

    tunnel = SshTunnel(settings)
    try:
        tunnel.start()
        with connect_mysql(settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DATABASE() AS db, VERSION() AS ver")
                row = cur.fetchone() or {}
        return True, f"OK — baza={row.get('db')}, MySQL {row.get('ver', '')[:20]}"
    except Exception as exc:
        return False, str(exc)
    finally:
        tunnel.stop()


def list_tables(settings: CrmSettings) -> list[str]:
    tunnel = SshTunnel(settings)
    try:
        tunnel.start()
        with connect_mysql(settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES")
                rows = cur.fetchall()
        if not rows:
            return []
        key = next(iter(rows[0]))
        return sorted(str(r[key]) for r in rows)
    finally:
        tunnel.stop()


def execute_query(
    settings: CrmSettings,
    sql: str,
    params: Optional[tuple | dict] = None,
    *,
    manage_tunnel: bool = True,
) -> pd.DataFrame:
    if not sql.strip():
        raise CrmMysqlError("report_sql пустой — укажите SQL в config.ini [crm]")

    tunnel = SshTunnel(settings) if manage_tunnel else None
    try:
        if tunnel is not None:
            tunnel.start()
        with connect_mysql(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                rows = cur.fetchall()
            return pd.DataFrame(rows) if rows else pd.DataFrame()
    finally:
        if tunnel is not None:
            tunnel.stop()


def fetch_report_dataframe(
    settings: CrmSettings,
    year: int,
    month: int,
) -> pd.DataFrame:
    """Run report_sql and return raw DataFrame for format_crm_report."""
    sql = settings.report_sql.strip()
    if not sql:
        from crm.report_sql import DEFAULT_REPORT_SQL
        sql = DEFAULT_REPORT_SQL
    df = execute_query(
        settings,
        sql,
        {"year": year, "month": month},
    )
    return _to_uduz04_layout(df)


def _to_uduz04_layout(df: pd.DataFrame) -> pd.DataFrame:
    """Convert query result to UDUZ04 Excel layout (columns 0..12, no header)."""
    if df.empty:
        raise CrmMysqlError("Запрос вернул 0 строк.")

    # Already positional 0..12
    numeric_cols = [c for c in df.columns if isinstance(c, int) or (isinstance(c, str) and str(c).isdigit())]
    if len(numeric_cols) >= 8:
        out = df.copy()
        for i in range(13):
            if i not in out.columns:
                out[i] = None
        ordered = sorted((int(c) for c in numeric_cols if int(c) < 13))
        return out[ordered]

    # Named columns → map aliases
    alias_map = {
        "lp": 0,
        "numer_umowy": 1,
        "number_umowy": 1,
        "nr_umowy": 1,
        "numer_rachunku": 2,
        "nr_rachunku": 2,
        "number": 2,
        "typ": 3,
        "rodzaj": 3,
        "pracownik": 4,
        "pesel": 5,
        "kwota_netto": 6,
        "netto": 6,
        "kwota_brutto": 7,
        "brutto": 7,
        "kup": 8,
        "kup_proc": 8,
        "data_zawarcia": 9,
        "podatek": 10,
        "data_wyplaty": 11,
        "data_akceptacji": 11,
        "ppk": 12,
    }
    lower_cols = {str(c).strip().lower(): c for c in df.columns}
    rows_out: list[list] = []
    for _, row in df.iterrows():
        out_row: list[Any] = [None] * 13
        for name, idx in alias_map.items():
            if name in lower_cols:
                out_row[idx] = row[lower_cols[name]]
        rows_out.append(out_row)
    return pd.DataFrame(rows_out)
