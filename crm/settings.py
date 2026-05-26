"""Load CRM MySQL + SSH tunnel settings from config.ini."""
from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

from secrets_store import decrypt_secret

_APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = _APP_DIR / "config.ini"

CRM_DEFAULTS: dict[str, str] = {
    "ssh_host": "",
    "ssh_user": "",
    "ssh_key_path": str(Path.home() / ".ssh" / "id_ed25519"),
    "rds_host": "",
    "rds_port": "3306",
    "local_port": "3307",
    "mysql_host": "127.0.0.1",
    "mysql_user": "",
    "mysql_password": "",
    "mysql_database": "",
    # SQL returning UDUZ04-like columns (0..12) or named columns mapped below.
    # Use %(year)s and %(month)s if filtering by period.
    "report_sql": "",
}

CRM_API_DEFAULTS: dict[str, str] = {
    "base_url": "",
    "token": "",
    # 0 means both tenants. 1 = FBA, 2 = FBA Payroll.
    "tenant_id": "0",
    "per_page": "500",
    "timeout_seconds": "60",
}


@dataclass(frozen=True)
class CrmSettings:
    ssh_host: str
    ssh_user: str
    ssh_key_path: Path
    rds_host: str
    rds_port: int
    local_port: int
    mysql_host: str
    mysql_user: str
    mysql_password: str
    mysql_database: str
    report_sql: str

    @property
    def tunnel_spec(self) -> str:
        return f"{self.local_port}:{self.rds_host}:{self.rds_port}"

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.ssh_host:
            errors.append("ssh_host пустой")
        if not self.ssh_user:
            errors.append("ssh_user пустой")
        if not self.ssh_key_path.is_file():
            errors.append(f"SSH ключ не найден: {self.ssh_key_path}")
        if not self.mysql_user:
            errors.append("mysql_user не задан (config.ini → [crm])")
        if not self.mysql_password:
            errors.append("mysql_password не задан (запустите: python tools/crm_configure.py)")
        if not self.mysql_database:
            errors.append("mysql_database пустой")
        return errors

    def ssh_command_line(self) -> list[str]:
        return [
            "ssh",
            "-i",
            str(self.ssh_key_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-L",
            self.tunnel_spec,
            f"{self.ssh_user}@{self.ssh_host}",
            "-N",
        ]


@dataclass(frozen=True)
class CrmApiSettings:
    base_url: str
    token: str
    tenant_id: int
    per_page: int
    timeout_seconds: int

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.base_url:
            errors.append("crm_api.base_url пустой")
        if not self.token:
            errors.append(
                "crm_api.token не задан (запустите: python tools/crm_api_configure.py --set-token)"
            )
        if self.per_page < 1 or self.per_page > 500:
            errors.append("crm_api.per_page должен быть 1..500")
        if self.tenant_id not in (0, 1, 2):
            errors.append("crm_api.tenant_id должен быть 0, 1 или 2")
        return errors


def ensure_crm_section(cfg: configparser.ConfigParser) -> bool:
    changed = False
    if "crm" not in cfg:
        cfg["crm"] = {}
        changed = True
    for key, default in CRM_DEFAULTS.items():
        if key not in cfg["crm"]:
            cfg["crm"][key] = default
            changed = True
    return changed


def ensure_crm_api_section(cfg: configparser.ConfigParser) -> bool:
    changed = False
    if "crm_api" not in cfg:
        cfg["crm_api"] = {}
        changed = True
    for key, default in CRM_API_DEFAULTS.items():
        if key not in cfg["crm_api"]:
            cfg["crm_api"][key] = default
            changed = True
    return changed


def load_crm_settings(config_path: Path | None = None) -> CrmSettings:
    path = config_path or CONFIG_PATH
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path, encoding="utf-8")
    ensure_crm_section(cfg)

    sec = cfg["crm"]
    key_path = Path(sec.get("ssh_key_path", CRM_DEFAULTS["ssh_key_path"]).strip())

    return CrmSettings(
        ssh_host=sec.get("ssh_host", CRM_DEFAULTS["ssh_host"]).strip(),
        ssh_user=sec.get("ssh_user", CRM_DEFAULTS["ssh_user"]).strip(),
        ssh_key_path=key_path,
        rds_host=sec.get("rds_host", CRM_DEFAULTS["rds_host"]).strip(),
        rds_port=int(sec.get("rds_port", CRM_DEFAULTS["rds_port"])),
        local_port=int(sec.get("local_port", CRM_DEFAULTS["local_port"])),
        mysql_host=sec.get("mysql_host", CRM_DEFAULTS["mysql_host"]).strip(),
        mysql_user=sec.get("mysql_user", "").strip(),
        mysql_password=decrypt_secret(sec.get("mysql_password", "").strip()),
        mysql_database=sec.get("mysql_database", CRM_DEFAULTS["mysql_database"]).strip(),
        report_sql=sec.get("report_sql", "").strip(),
    )


def load_crm_api_settings(config_path: Path | None = None) -> CrmApiSettings:
    path = config_path or CONFIG_PATH
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path, encoding="utf-8")
    ensure_crm_api_section(cfg)
    sec = cfg["crm_api"]
    per_page = int(sec.get("per_page", CRM_API_DEFAULTS["per_page"]))
    per_page = max(1, min(per_page, 500))
    tenant_id = int(sec.get("tenant_id", CRM_API_DEFAULTS["tenant_id"]))
    if tenant_id not in (0, 1, 2):
        tenant_id = 0
    return CrmApiSettings(
        base_url=sec.get("base_url", CRM_API_DEFAULTS["base_url"]).strip().rstrip("/"),
        token=decrypt_secret(sec.get("token", "").strip()),
        tenant_id=tenant_id,
        per_page=per_page,
        timeout_seconds=int(sec.get("timeout_seconds", CRM_API_DEFAULTS["timeout_seconds"])),
    )


def save_crm_api_tenant_id(
    tenant_id: int,
    config_path: Path | None = None,
) -> None:
    path = config_path or CONFIG_PATH
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path, encoding="utf-8")
    ensure_crm_api_section(cfg)
    cfg["crm_api"]["tenant_id"] = str(int(tenant_id))
    with path.open("w", encoding="utf-8") as f:
        cfg.write(f)


def save_crm_api_token(
    plain_token: str,
    config_path: Path | None = None,
) -> None:
    """Encrypt and store CRM API token in config.ini [crm_api]."""
    from secrets_store import encrypt_secret

    path = config_path or CONFIG_PATH
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path, encoding="utf-8")
    ensure_crm_api_section(cfg)
    cfg["crm_api"]["token"] = encrypt_secret(plain_token)
    with path.open("w", encoding="utf-8") as f:
        cfg.write(f)


def save_crm_mysql_password(
    plain_password: str,
    config_path: Path | None = None,
) -> None:
    """Encrypt and store MySQL password in config.ini [crm]."""
    from secrets_store import encrypt_secret

    path = config_path or CONFIG_PATH
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path, encoding="utf-8")
    ensure_crm_section(cfg)
    cfg["crm"]["mysql_password"] = encrypt_secret(plain_password)
    with path.open("w", encoding="utf-8") as f:
        cfg.write(f)


def save_crm_mysql_user(
    user: str,
    config_path: Path | None = None,
) -> None:
    path = config_path or CONFIG_PATH
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path, encoding="utf-8")
    ensure_crm_section(cfg)
    cfg["crm"]["mysql_user"] = user.strip()
    with path.open("w", encoding="utf-8") as f:
        cfg.write(f)
