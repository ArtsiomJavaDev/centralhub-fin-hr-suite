"""
Central secrets router.

Priority chain: private.py  →  config.ini  →  code defaults

Usage in any module:
    from _secrets import get_merged_config
    cfg = get_merged_config()          # ConfigParser with all secrets merged in
    value = cfg["crm"]["ssh_host"]

All sensitive configuration lives in private.py (gitignored).
config.ini is used as fallback and for values written by the app at runtime
(encrypted passwords, per_page, tenant_id, etc.).
"""
from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

_APP_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _APP_DIR / "config.ini"

# ── Load private.py overrides ──────────────────────────────────────────────
try:
    import private as _pm  # type: ignore[import]

    _PRIVATE: dict[str, dict[str, Any]] = {
        "database": getattr(_pm, "DATABASE", {}) or {},
        "crm":      getattr(_pm, "CRM",      {}) or {},
        "crm_api":  getattr(_pm, "CRM_API",  {}) or {},
        "app":      getattr(_pm, "APP",      {}) or {},
    }
    _PRIVATE_LOADED = True
except ImportError:
    _PRIVATE = {}
    _PRIVATE_LOADED = False


# ── Public API ─────────────────────────────────────────────────────────────

def get_merged_config(config_path: Path | None = None) -> configparser.ConfigParser:
    """
    Return a ConfigParser with secrets merged in from private.py.

    Values from private.py override config.ini.
    Empty strings in private.py are treated as "not set" (config.ini wins).
    """
    path = config_path or _CONFIG_PATH
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path, encoding="utf-8")

    for section, overrides in _PRIVATE.items():
        if not overrides:
            continue
        if section not in cfg:
            cfg[section] = {}
        for key, value in overrides.items():
            str_val = str(value).strip()
            if str_val:  # only override when private.py actually has a value
                cfg[section][key] = str_val

    return cfg


def private_loaded() -> bool:
    """True if private.py was found and imported successfully."""
    return _PRIVATE_LOADED
