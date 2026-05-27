"""CRM integration package — report loading, formatting, verification and history."""
from .formatter import format_crm_report, FormatterResult
from .checker import check_pesels_in_db, verify_financials, CheckPeselResult, VerifyResult
from . import history
from .settings import load_crm_settings, load_crm_api_settings, CrmSettings, CrmApiSettings
from .mysql_client import test_connection as crm_test_connection, fetch_report_dataframe
from .api_client import CrmApiClient, fetch_report_dataframe_api

__all__ = [
    "format_crm_report",
    "FormatterResult",
    "check_pesels_in_db",
    "verify_financials",
    "CheckPeselResult",
    "VerifyResult",
    "history",
    "load_crm_settings",
    "load_crm_api_settings",
    "CrmSettings",
    "CrmApiSettings",
    "crm_test_connection",
    "fetch_report_dataframe",
    "CrmApiClient",
    "fetch_report_dataframe_api",
]
