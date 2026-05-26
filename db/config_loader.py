"""Load WaPro SQL Server settings from config.ini."""
from __future__ import annotations

import configparser
from pathlib import Path

from db.config import DbConfig
from secrets_store import decrypt_secret

_APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = _APP_DIR / "config.ini"


def load_db_config(config_path: Path | None = None) -> DbConfig:
    path = config_path or CONFIG_PATH
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path, encoding="utf-8")
    if "database" not in cfg:
        raise RuntimeError(f"Brak sekcji [database] w {path}")
    db = cfg["database"]
    trusted_raw = db.get("trusted_connection", "yes").strip().lower()
    trusted = trusted_raw in ("yes", "true", "1", "y")
    return DbConfig(
        driver=db.get("driver", "ODBC Driver 17 for SQL Server").strip(),
        server=db.get("server", "localhost").strip(),
        database=db.get("database", "WAPRO").strip(),
        username=db.get("username", "").strip(),
        password=decrypt_secret(db.get("password", "").strip()),
        trusted_connection=trusted,
    )
