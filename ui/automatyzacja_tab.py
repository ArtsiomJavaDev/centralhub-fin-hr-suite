"""Automatyzacja tab — full automation pipeline for CRM report import.

Pipeline steps:
  1. Load & format CRM UDUZ04-type file → batch_merged DataFrame
     - Student/ZUS-exempt detection (brutto≈netto → składki=0)
     - Special-PESEL chorobowe 2.45%
  2. Check employees in payroll database (batch PESEL lookup)
  3. Verify netto/brutto calculations against source values
  4. Dry-run — check-in without writing to DB (shows per-row table)
  5. Pre-import duplicate guard — detect if period was already imported
  6. Import umowy to payroll database with progress bar + cancel
  7. Rollback: works from current session OR from import history

Import history is persisted to LogsAutomatization/import_history.json.
Logs are written to LogsAutomatization/ directory.
"""
from __future__ import annotations

import os
import subprocess
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

import pandas as pd
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from crm.formatter import format_crm_report, df_to_export, AUTO_MAPPING, FormatterResult
from crm.settings import load_crm_settings, load_crm_api_settings, save_crm_api_tenant_id
from crm.mysql_client import test_connection as crm_test_connection, fetch_report_dataframe
from crm.api_client import CrmApiClient, fetch_report_dataframe_api, save_api_audit_files
from crm.checker import check_pesels_in_db, verify_financials, CheckPeselResult, VerifyResult
from crm.reconciliation import reconcile_rachunki
from crm import history as _hist
from crm.history import ImportHistoryRecord
from database import DatabaseService, DbConfig
from importer.mapping import map_columns
from importer.checkin import check_in
from importer.profiles import UMOWY_MIXED_IMPORT_PROFILE
from importer.types import RowStatus
from ui.import_worker import run_in_thread


_LOGS_DIR = Path(__file__).resolve().parent.parent / "LogsAutomatization"
_IMPORT_FILES_DIR = Path(__file__).resolve().parent.parent / "ImportFiles"

_STATUS_OK = "✔"
_STATUS_WAIT = "–"
_STATUS_ERR = "✘"
_STATUS_WARN = "!"

_COLOR_OK = "#4ade80"
_COLOR_WARN = "#fbbf24"
_COLOR_ERR = "#f87171"
_COLOR_MUTED = "#94a3b8"

# CRM onboarding defaults
_ONBOARD_ID_FIRMY = 1
_ONBOARD_START_URZAD_ID = 1000
_COLOR_ROLLBACK = "#f472b6"

_CHECKIN_TABLE_COLS = ["#", "Status", "Wiersz", "Pole", "Komunikat"]
_HISTORY_COLS_KEYS = [
    "auto.history.col_date",
    "auto.history.col_file",
    "auto.history.col_period",
    "auto.history.col_created",
    "auto.history.col_skipped",
    "auto.history.col_missing",
    "auto.history.col_verify",
    "auto.history.col_ids",
    "auto.history.col_status",
]


def _status_color(status: RowStatus) -> str:
    if status == RowStatus.OK:
        return _COLOR_OK
    if status == RowStatus.WARNING:
        return _COLOR_WARN
    return _COLOR_ERR


def _item(text: str, color: Optional[str] = None, bold: bool = False) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    if color:
        it.setForeground(QColor(color))
    if bold:
        font = it.font()
        font.setBold(True)
        it.setFont(font)
    return it


class AutomatyzacjaTab(QWidget):
    """Automatyzacja — CRM report → payroll system import pipeline."""

    def __init__(
        self,
        db_config_provider: Callable[[], DbConfig],
        log_callback: Callable[[str], None],
        translate: Callable[[str], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db_config_provider = db_config_provider
        self._ext_log = log_callback
        self._t = translate or (lambda key: key)

        # Pipeline state
        self._source_path: Optional[str] = None
        self._df_formatted: Optional[pd.DataFrame] = None
        self._fmt_result: Optional[FormatterResult] = None
        self._check_result: Optional[CheckPeselResult] = None
        self._verify_result: Optional[VerifyResult] = None
        self._last_import_ids: List[int] = []
        self._last_history_id: Optional[str] = None
        self._log_lines: list[str] = []
        self._auto_onboard_in_progress = False

        # Worker references (prevent GC)
        self._thread = None
        self._worker = None

        self._build_ui()

    # ─── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.frameShape().NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        title = QLabel(self._t("auto.title"))
        title.setStyleSheet("font-size:12pt;font-weight:700;color:#e2e8f0;")
        layout.addWidget(title)

        hint = QLabel(self._t("auto.hint"))
        hint.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # ── CRM API ─────────────────────────────────────────────────────────
        g_api = QGroupBox(self._t("auto.api.title"))
        g_api_l = QVBoxLayout(g_api)

        api_hint = QLabel(self._t("auto.api.hint"))
        api_hint.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        api_hint.setWordWrap(True)
        g_api_l.addWidget(api_hint)

        api_period_row = QHBoxLayout()
        api_period_row.addWidget(QLabel(self._t("auto.crm.year")))
        self._api_year = QSpinBox()
        self._api_year.setRange(2020, 2035)
        self._api_year.setValue(datetime.now().year)
        api_period_row.addWidget(self._api_year)
        api_period_row.addWidget(QLabel(self._t("auto.crm.month")))
        self._api_month = QSpinBox()
        self._api_month.setRange(1, 12)
        self._api_month.setValue(datetime.now().month)
        api_period_row.addWidget(self._api_month)
        api_period_row.addStretch()
        g_api_l.addLayout(api_period_row)

        api_tenant_row = QHBoxLayout()
        api_tenant_row.addWidget(QLabel(self._t("auto.api.tenant")))
        self._api_tenant = QComboBox()
        self._api_tenant.addItem(self._t("auto.api.tenant_all"), 0)
        self._api_tenant.addItem(self._t("auto.api.tenant_fba"), 1)
        self._api_tenant.addItem(self._t("auto.api.tenant_payroll"), 2)
        api_settings = load_crm_api_settings()
        for idx in range(self._api_tenant.count()):
            if self._api_tenant.itemData(idx) == api_settings.tenant_id:
                self._api_tenant.setCurrentIndex(idx)
                break
        self._api_tenant.currentIndexChanged.connect(self._on_api_tenant_changed)
        api_tenant_row.addWidget(self._api_tenant)
        api_tenant_row.addStretch()
        g_api_l.addLayout(api_tenant_row)

        self._api_progress = QProgressBar()
        self._api_progress.setMinimum(0)
        self._api_progress.setMaximum(100)
        self._api_progress.setValue(0)
        self._api_progress.setTextVisible(True)
        self._api_progress.setVisible(False)
        g_api_l.addWidget(self._api_progress)

        api_btns = QHBoxLayout()
        self._btn_api_test = QPushButton(self._t("auto.api.test_btn"))
        self._btn_api_test.clicked.connect(self._on_api_test)
        api_btns.addWidget(self._btn_api_test)

        self._btn_api_fetch = QPushButton(self._t("auto.api.fetch_btn"))
        self._btn_api_fetch.clicked.connect(self._on_api_fetch)
        self._btn_api_fetch.setObjectName("PrimaryAction")
        api_btns.addWidget(self._btn_api_fetch)
        api_btns.addStretch()
        g_api_l.addLayout(api_btns)

        self._lbl_api_status = QLabel("")
        self._lbl_api_status.setWordWrap(True)
        self._lbl_api_status.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        g_api_l.addWidget(self._lbl_api_status)
        layout.addWidget(g_api)

        # ── CRM MySQL (SSH tunnel) fallback/diagnostics ──────────────────────
        g_crm = QGroupBox(self._t("auto.crm.title"))
        g_crm_l = QVBoxLayout(g_crm)

        crm_hint = QLabel(self._t("auto.crm.hint"))
        crm_hint.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        crm_hint.setWordWrap(True)
        g_crm_l.addWidget(crm_hint)

        period_row = QHBoxLayout()
        period_row.addWidget(QLabel(self._t("auto.crm.year")))
        self._crm_year = QSpinBox()
        self._crm_year.setRange(2020, 2035)
        self._crm_year.setValue(datetime.now().year)
        period_row.addWidget(self._crm_year)
        period_row.addWidget(QLabel(self._t("auto.crm.month")))
        self._crm_month = QSpinBox()
        self._crm_month.setRange(1, 12)
        self._crm_month.setValue(datetime.now().month)
        period_row.addWidget(self._crm_month)
        period_row.addStretch()
        g_crm_l.addLayout(period_row)

        crm_btns = QHBoxLayout()
        self._btn_crm_test = QPushButton(self._t("auto.crm.test_btn"))
        self._btn_crm_test.clicked.connect(self._on_crm_test)
        crm_btns.addWidget(self._btn_crm_test)

        self._btn_crm_fetch = QPushButton(self._t("auto.crm.fetch_btn"))
        self._btn_crm_fetch.clicked.connect(self._on_crm_fetch)
        crm_btns.addWidget(self._btn_crm_fetch)
        crm_btns.addStretch()
        g_crm_l.addLayout(crm_btns)

        self._lbl_crm_status = QLabel("")
        self._lbl_crm_status.setWordWrap(True)
        self._lbl_crm_status.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        g_crm_l.addWidget(self._lbl_crm_status)
        layout.addWidget(g_crm)

        # ── ① Plik źródłowy ─────────────────────────────────────────────────
        g1 = QGroupBox(f"① {self._t('auto.step1.title')}")
        g1l = QVBoxLayout(g1)

        row_file = QHBoxLayout()
        self._file_label = QLabel(self._t("auto.no_file"))
        self._file_label.setStyleSheet(f"color:{_COLOR_MUTED};font-style:italic;")
        self._file_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row_file.addWidget(self._file_label)

        btn_pick = QPushButton(self._t("auto.choose_file"))
        btn_pick.setFixedWidth(160)
        btn_pick.clicked.connect(self._on_pick_file)
        row_file.addWidget(btn_pick)
        g1l.addLayout(row_file)

        self._btn_format = QPushButton(self._t("auto.step1.btn"))
        self._btn_format.setEnabled(False)
        self._btn_format.clicked.connect(self._on_format)
        g1l.addWidget(self._btn_format)

        self._lbl_format_status = QLabel("")
        self._lbl_format_status.setWordWrap(True)
        self._lbl_format_status.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        g1l.addWidget(self._lbl_format_status)
        layout.addWidget(g1)

        # ── Podgląd ──────────────────────────────────────────────────────────
        g_prev = QGroupBox(self._t("auto.preview_title"))
        g_prev_l = QVBoxLayout(g_prev)
        self._preview_table = QTableWidget(0, 0)
        self._preview_table.setAlternatingRowColors(True)
        self._preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._preview_table.setMinimumHeight(160)
        g_prev_l.addWidget(self._preview_table)
        layout.addWidget(g_prev)

        # ── ② Sprawdź pracowników ────────────────────────────────────────────
        g2 = QGroupBox(f"② {self._t('auto.step2.title')}")
        g2l = QVBoxLayout(g2)
        self._btn_check = QPushButton(self._t("auto.step2.btn"))
        self._btn_check.setEnabled(False)
        self._btn_check.clicked.connect(self._on_check_pesels)
        g2l.addWidget(self._btn_check)
        self._lbl_check_status = QLabel("")
        self._lbl_check_status.setWordWrap(True)
        self._lbl_check_status.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        g2l.addWidget(self._lbl_check_status)

        self._missing_table = QTableWidget(0, 3)
        self._missing_table.setHorizontalHeaderLabels(["PESEL", "Pracownik", "Nr rachunku"])
        self._missing_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._missing_table.setAlternatingRowColors(True)
        self._missing_table.setMaximumHeight(120)
        self._missing_table.setVisible(False)
        g2l.addWidget(self._missing_table)

        self._btn_onboard = QPushButton(self._t("auto.step2.onboard_btn"))
        self._btn_onboard.setEnabled(False)
        self._btn_onboard.setVisible(False)
        self._btn_onboard.clicked.connect(lambda: self._on_onboard_missing(auto=False))
        g2l.addWidget(self._btn_onboard)

        self._lbl_onboard_status = QLabel("")
        self._lbl_onboard_status.setWordWrap(True)
        self._lbl_onboard_status.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        g2l.addWidget(self._lbl_onboard_status)

        layout.addWidget(g2)

        # ── ③ Weryfikacja ────────────────────────────────────────────────────
        g3 = QGroupBox(f"③ {self._t('auto.step3.title')}")
        g3l = QVBoxLayout(g3)
        self._btn_verify = QPushButton(self._t("auto.step3.btn"))
        self._btn_verify.setEnabled(False)
        self._btn_verify.clicked.connect(self._on_verify)
        g3l.addWidget(self._btn_verify)
        self._lbl_verify_status = QLabel("")
        self._lbl_verify_status.setWordWrap(True)
        self._lbl_verify_status.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        g3l.addWidget(self._lbl_verify_status)
        layout.addWidget(g3)

        # ── ④ Dry-run ────────────────────────────────────────────────────────
        g_dry = QGroupBox(f"④ {self._t('auto.step_dryrun.title')}")
        g_dry_l = QVBoxLayout(g_dry)

        dry_hint = QLabel(self._t("auto.step_dryrun.hint"))
        dry_hint.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        dry_hint.setWordWrap(True)
        g_dry_l.addWidget(dry_hint)

        self._btn_dryrun = QPushButton(self._t("auto.step_dryrun.btn"))
        self._btn_dryrun.setEnabled(False)
        self._btn_dryrun.clicked.connect(self._on_dryrun)
        g_dry_l.addWidget(self._btn_dryrun)

        self._lbl_dryrun_status = QLabel("")
        self._lbl_dryrun_status.setWordWrap(True)
        self._lbl_dryrun_status.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        g_dry_l.addWidget(self._lbl_dryrun_status)

        self._checkin_table = QTableWidget(0, len(_CHECKIN_TABLE_COLS))
        self._checkin_table.setHorizontalHeaderLabels(_CHECKIN_TABLE_COLS)
        self._checkin_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._checkin_table.setAlternatingRowColors(True)
        self._checkin_table.setMaximumHeight(200)
        self._checkin_table.setVisible(False)
        g_dry_l.addWidget(self._checkin_table)
        layout.addWidget(g_dry)

        # ── ⑤ Import ─────────────────────────────────────────────────────────
        g4 = QGroupBox(f"⑤ {self._t('auto.step4.title')}")
        g4l = QVBoxLayout(g4)

        hint4 = QLabel(self._t("auto.step4.hint"))
        hint4.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        hint4.setWordWrap(True)
        g4l.addWidget(hint4)

        import_btns = QHBoxLayout()
        self._btn_import = QPushButton(self._t("auto.step4.btn"))
        self._btn_import.setEnabled(False)
        self._btn_import.clicked.connect(self._on_import)
        import_btns.addWidget(self._btn_import)

        self._btn_cancel = QPushButton(self._t("auto.cancel"))
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._on_cancel_import)
        self._btn_cancel.setStyleSheet(f"color:{_COLOR_ERR};")
        import_btns.addWidget(self._btn_cancel)
        g4l.addLayout(import_btns)

        self._progress_bar = QProgressBar()
        self._progress_bar.setMinimum(0)
        self._progress_bar.setMaximum(100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setVisible(False)
        g4l.addWidget(self._progress_bar)

        self._lbl_import_status = QLabel("")
        self._lbl_import_status.setWordWrap(True)
        self._lbl_import_status.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        g4l.addWidget(self._lbl_import_status)
        layout.addWidget(g4)

        # ── ⑥ Historia importów ──────────────────────────────────────────────
        g_hist = QGroupBox(f"⑥ {self._t('auto.history.title')}")
        g_hist_l = QVBoxLayout(g_hist)

        hist_btns = QHBoxLayout()
        btn_refresh = QPushButton(self._t("auto.history.refresh"))
        btn_refresh.clicked.connect(self._load_history_table)
        hist_btns.addWidget(btn_refresh)

        self._btn_hist_rollback = QPushButton(self._t("auto.history.rollback_btn"))
        self._btn_hist_rollback.clicked.connect(self._on_history_rollback)
        self._btn_hist_rollback.setStyleSheet(f"color:{_COLOR_ROLLBACK};font-weight:700;")
        hist_btns.addWidget(self._btn_hist_rollback)

        self._btn_hist_log = QPushButton(self._t("auto.history.open_log_btn"))
        self._btn_hist_log.clicked.connect(self._on_history_open_log)
        hist_btns.addWidget(self._btn_hist_log)
        hist_btns.addStretch()
        g_hist_l.addLayout(hist_btns)

        hist_col_labels = [self._t(k) for k in _HISTORY_COLS_KEYS]
        self._history_table = QTableWidget(0, len(hist_col_labels))
        self._history_table.setHorizontalHeaderLabels(hist_col_labels)
        self._history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._history_table.setAlternatingRowColors(True)
        self._history_table.setMinimumHeight(160)
        g_hist_l.addWidget(self._history_table)
        layout.addWidget(g_hist)

        # ── Log area ─────────────────────────────────────────────────────────
        g_log = QGroupBox(self._t("auto.log_title"))
        g_log_l = QVBoxLayout(g_log)
        self._log_area = QTextEdit()
        self._log_area.setReadOnly(True)
        self._log_area.setMinimumHeight(180)
        self._log_area.setStyleSheet(
            "background:#0f172a;color:#cbd5e1;font-family:Consolas,monospace;font-size:9pt;"
        )
        g_log_l.addWidget(self._log_area)

        btn_row = QHBoxLayout()
        btn_save_log = QPushButton(self._t("auto.save_log"))
        btn_save_log.clicked.connect(self._on_save_log)
        btn_open_logs = QPushButton(self._t("auto.open_logs_folder"))
        btn_open_logs.clicked.connect(self._on_open_logs_folder)
        btn_row.addStretch()
        btn_row.addWidget(btn_open_logs)
        btn_row.addWidget(btn_save_log)
        g_log_l.addLayout(btn_row)
        layout.addWidget(g_log)

        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)

        # Load history on startup
        self._load_history_table()

    # ─── Internal logging ─────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._log_lines.append(line)
        self._log_area.append(line)
        self._ext_log(f"[auto] {msg}")

    # ─── CRM API fetch ────────────────────────────────────────────────────────

    def _api_tenant_id(self) -> int:
        return int(self._api_tenant.currentData())

    def _on_api_tenant_changed(self) -> None:
        save_crm_api_tenant_id(self._api_tenant_id())

    def _on_api_test(self) -> None:
        self._log("CRM API: test połączenia…")
        self._btn_api_test.setEnabled(False)
        settings = load_crm_api_settings()
        tenant_id = self._api_tenant_id()

        def _job(_progress_cb, _cancel_token, _s=settings, _tid=tenant_id):
            from dataclasses import replace

            return CrmApiClient(replace(_s, tenant_id=_tid)).test_connection()

        self._thread, self._worker = run_in_thread(self, _job)
        self._worker.finished.connect(self._on_api_test_finished)
        self._worker.failed.connect(self._on_api_test_failed)
        self._thread.start()

    def _on_api_test_finished(self, result: object) -> None:
        ok, msg = result  # type: ignore[misc]
        self._btn_api_test.setEnabled(True)
        if ok:
            self._log(f"CRM API: {msg}")
            self._lbl_api_status.setText(msg)
            self._lbl_api_status.setStyleSheet(f"color:{_COLOR_OK};font-size:9pt;")
        else:
            self._log(f"CRM API BŁĄD: {msg}")
            self._lbl_api_status.setText(msg)
            self._lbl_api_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")

    def _on_api_test_failed(self, message: str) -> None:
        self._btn_api_test.setEnabled(True)
        self._log(f"CRM API test failed:\n{message}")
        self._lbl_api_status.setText("Błąd testu API — patrz log")
        self._lbl_api_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")

    def _on_api_fetch(self) -> None:
        settings = load_crm_api_settings()
        errors = settings.validate()
        if errors:
            QMessageBox.warning(
                self,
                self._t("auto.api.fetch_btn"),
                "\n".join(errors),
            )
            return

        year = self._api_year.value()
        month = self._api_month.value()
        tenant_id = self._api_tenant_id()
        save_crm_api_tenant_id(tenant_id)
        self._log(f"CRM API: pobieranie raportu {month:02d}/{year} (tenant={tenant_id})…")
        self._btn_api_fetch.setEnabled(False)
        self._btn_api_test.setEnabled(False)
        self._api_progress.setValue(0)
        self._api_progress.setVisible(True)

        def _job(progress_cb, _cancel_token, _s=settings, _y=year, _m=month, _tid=tenant_id):
            def api_progress(_step: str, done: int, total: int) -> None:
                progress_cb(done, max(total, 1))

            raw_df, stats = fetch_report_dataframe_api(
                _s, _y, _m, tenant_id=_tid, progress_cb=api_progress
            )
            formatted_df, fmt = format_crm_report(raw_df)
            audit_paths = save_api_audit_files(raw_df, formatted_df, _y, _m, _tid)
            return formatted_df, fmt, stats, audit_paths

        self._thread, self._worker = run_in_thread(self, _job)
        self._worker.progress.connect(self._on_api_fetch_progress)
        self._worker.finished.connect(self._on_api_fetch_finished)
        self._worker.failed.connect(self._on_api_fetch_failed)
        self._thread.start()

    def _on_api_fetch_progress(self, done: int, total: int) -> None:
        self._api_progress.setMaximum(max(total, 1))
        self._api_progress.setValue(done)

    def _on_api_fetch_finished(self, result: object) -> None:
        df, fmt_result, stats, audit_paths = result  # type: ignore[misc]
        self._btn_api_fetch.setEnabled(True)
        self._btn_api_test.setEnabled(True)
        self._api_progress.setValue(self._api_progress.maximum())
        self._api_progress.setVisible(False)

        self._df_formatted = df
        self._fmt_result = fmt_result
        self._source_path = (
            f"CRM API {self._api_month.value():02d}/{self._api_year.value()}"
        )
        self._file_label.setText(self._source_path)
        self._file_label.setStyleSheet("color:#e2e8f0;")

        self._log(
            self._t("auto.api.fetch_ok").format(
                total=fmt_result.total_rows,
                ud=fmt_result.ud_count,
                uz=fmt_result.uz_count,
                bills=stats.bills_in_period,
                skipped=stats.skipped_without_pesel,
            )
        )
        self._log(
            self._t("auto.api.stats").format(
                period=stats.bills_in_period,
                output=stats.output_rows,
                req=stats.api_requests,
                no_contract=stats.skipped_without_contract,
                no_person=stats.skipped_without_person,
                no_pesel=stats.skipped_without_pesel,
                bad_type=stats.skipped_bad_contract_type,
                legal=stats.skipped_legal_entity,
            )
        )
        raw_path, fmt_path = audit_paths
        self._log(f"CRM API: zapis audytu → {raw_path}")
        self._log(f"CRM API: zapis sformatowany → {fmt_path}")
        self._lbl_api_status.setText(
            self._t("auto.step1.ok").format(
                total=fmt_result.total_rows,
                ud=fmt_result.ud_count,
                uz=fmt_result.uz_count,
                wpl_min=fmt_result.date_wyplaty_min or "?",
                wpl_max=fmt_result.date_wyplaty_max or "?",
            )
        )
        self._lbl_api_status.setStyleSheet(f"color:{_COLOR_OK};font-size:9pt;font-weight:600;")

        self._lbl_format_status.setText(self._lbl_api_status.text())
        self._lbl_format_status.setStyleSheet(self._lbl_api_status.styleSheet())
        self._fill_preview(df)
        for btn in (self._btn_check, self._btn_verify, self._btn_dryrun):
            btn.setEnabled(True)
        for w in fmt_result.warnings:
            self._log(f"  OSTRZEŻENIE: {w}")

    def _on_api_fetch_failed(self, message: str) -> None:
        self._btn_api_fetch.setEnabled(True)
        self._btn_api_test.setEnabled(True)
        self._api_progress.setVisible(False)
        self._log(f"CRM API pobieranie nie powiodło się:\n{message}")
        self._lbl_api_status.setText("Błąd pobierania API — patrz log")
        self._lbl_api_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")
        QMessageBox.critical(self, self._t("auto.api.fetch_btn"), message[:2000])

    # ─── CRM MySQL fetch ──────────────────────────────────────────────────────

    def _on_crm_test(self) -> None:
        self._log("CRM: test połączenia SSH + MySQL…")
        self._btn_crm_test.setEnabled(False)
        settings = load_crm_settings()

        def _job(_progress_cb, _cancel_token, _s=settings):
            return crm_test_connection(_s)

        self._thread, self._worker = run_in_thread(self, _job)
        self._worker.finished.connect(self._on_crm_test_finished)
        self._worker.failed.connect(self._on_crm_test_failed)
        self._thread.start()

    def _on_crm_test_finished(self, result: object) -> None:
        ok, msg = result  # type: ignore[misc]
        self._btn_crm_test.setEnabled(True)
        if ok:
            self._log(f"CRM: {msg}")
            self._lbl_crm_status.setText(msg)
            self._lbl_crm_status.setStyleSheet(f"color:{_COLOR_OK};font-size:9pt;")
        else:
            self._log(f"CRM BŁĄD: {msg}")
            self._lbl_crm_status.setText(msg)
            self._lbl_crm_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")

    def _on_crm_test_failed(self, message: str) -> None:
        self._btn_crm_test.setEnabled(True)
        self._log(f"CRM test failed:\n{message}")
        self._lbl_crm_status.setText("Błąd testu — patrz log")
        self._lbl_crm_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")

    def _on_crm_fetch(self) -> None:
        settings = load_crm_settings()
        errors = settings.validate()
        if errors:
            QMessageBox.warning(
                self,
                self._t("auto.crm.fetch_btn"),
                "\n".join(errors),
            )
            return

        year = self._crm_year.value()
        month = self._crm_month.value()
        self._log(f"CRM: pobieranie raportu {month:02d}/{year}…")
        self._btn_crm_fetch.setEnabled(False)
        self._btn_crm_test.setEnabled(False)

        def _job(_progress_cb, _cancel_token, _s=settings, _y=year, _m=month):
            raw_df = fetch_report_dataframe(_s, _y, _m)
            return format_crm_report(raw_df)

        self._thread, self._worker = run_in_thread(self, _job)
        self._worker.finished.connect(self._on_crm_fetch_finished)
        self._worker.failed.connect(self._on_crm_fetch_failed)
        self._thread.start()

    def _on_crm_fetch_finished(self, result: object) -> None:
        df, fmt_result = result  # type: ignore[misc]
        self._btn_crm_fetch.setEnabled(True)
        self._btn_crm_test.setEnabled(True)

        self._df_formatted = df
        self._fmt_result = fmt_result
        self._source_path = (
            f"CRM MySQL {self._crm_month.value():02d}/{self._crm_year.value()}"
        )
        self._file_label.setText(self._source_path)
        self._file_label.setStyleSheet("color:#e2e8f0;")

        self._log(
            self._t("auto.crm.fetch_ok").format(
                total=fmt_result.total_rows,
                ud=fmt_result.ud_count,
                uz=fmt_result.uz_count,
            )
        )
        self._lbl_crm_status.setText(
            self._t("auto.step1.ok").format(
                total=fmt_result.total_rows,
                ud=fmt_result.ud_count,
                uz=fmt_result.uz_count,
                wpl_min=fmt_result.date_wyplaty_min or "?",
                wpl_max=fmt_result.date_wyplaty_max or "?",
            )
        )
        self._lbl_crm_status.setStyleSheet(f"color:{_COLOR_OK};font-size:9pt;font-weight:600;")

        self._lbl_format_status.setText(self._lbl_crm_status.text())
        self._lbl_format_status.setStyleSheet(self._lbl_crm_status.styleSheet())
        self._fill_preview(df)
        for btn in (self._btn_check, self._btn_verify, self._btn_dryrun):
            btn.setEnabled(True)

        for w in fmt_result.warnings:
            self._log(f"  OSTRZEŻENIE: {w}")

    def _on_crm_fetch_failed(self, message: str) -> None:
        self._btn_crm_fetch.setEnabled(True)
        self._btn_crm_test.setEnabled(True)
        self._log(f"CRM pobieranie nie powiodło się:\n{message}")
        self._lbl_crm_status.setText("Błąd pobierania — patrz log")
        self._lbl_crm_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")
        QMessageBox.critical(self, self._t("auto.crm.fetch_btn"), message[:2000])

    # ─── Step 1: Pick file + format ───────────────────────────────────────────

    def _on_pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, self._t("auto.choose_file"), str(Path.home()), "Excel (*.xlsx *.xls)"
        )
        if not path:
            return
        self._source_path = path
        self._file_label.setText(Path(path).name)
        self._file_label.setStyleSheet("color:#e2e8f0;")
        self._btn_format.setEnabled(True)
        self._reset_pipeline_state()

    def _reset_pipeline_state(self) -> None:
        self._df_formatted = None
        self._fmt_result = None
        self._check_result = None
        self._verify_result = None
        self._last_import_ids = []
        self._last_history_id = None
        for btn in (self._btn_check, self._btn_verify, self._btn_dryrun, self._btn_import):
            btn.setEnabled(False)
        for lbl in (
            self._lbl_format_status, self._lbl_check_status,
            self._lbl_verify_status, self._lbl_dryrun_status, self._lbl_import_status,
        ):
            lbl.setText("")
        self._preview_table.setRowCount(0)
        self._preview_table.setColumnCount(0)
        self._missing_table.setRowCount(0)
        self._missing_table.setVisible(False)
        self._checkin_table.setRowCount(0)
        self._checkin_table.setVisible(False)
        self._progress_bar.setVisible(False)
        self._progress_bar.setValue(0)
        self._btn_cancel.setEnabled(False)

    def _on_format(self) -> None:
        if not self._source_path:
            return
        self._log(f"Formatowanie: {Path(self._source_path).name}")
        try:
            df, result = format_crm_report(self._source_path)
        except Exception as exc:
            self._log(f"BŁĄD formatowania: {exc}")
            self._lbl_format_status.setText(f"Błąd: {exc}")
            self._lbl_format_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")
            return

        self._df_formatted = df
        self._fmt_result = result

        self._log(
            f"Załadowano {result.total_rows} umów "
            f"(UD={result.ud_count}, UZ={result.uz_count}), "
            f"PPK dopasowanych={result.ppk_matched}, "
            f"pominięto={result.skipped_rows}."
        )
        if result.date_wyplaty_min:
            self._log(
                f"Data wypłaty: {result.date_wyplaty_min} – {result.date_wyplaty_max}"
            )
        if result.date_zawarcia_min:
            self._log(
                f"Data zawarcia: {result.date_zawarcia_min} – {result.date_zawarcia_max}"
            )
        for w in result.warnings:
            self._log(f"  OSTRZEŻENIE: {w}")

        self._lbl_format_status.setText(
            self._t("auto.step1.ok").format(
                total=result.total_rows,
                ud=result.ud_count,
                uz=result.uz_count,
                wpl_min=result.date_wyplaty_min or "?",
                wpl_max=result.date_wyplaty_max or "?",
            )
        )
        self._lbl_format_status.setStyleSheet(
            f"color:{_COLOR_OK};font-size:9pt;font-weight:600;"
        )
        self._fill_preview(df)
        for btn in (self._btn_check, self._btn_verify, self._btn_dryrun):
            btn.setEnabled(True)

    def _fill_preview(self, df: pd.DataFrame, limit: int = 20) -> None:
        display = df_to_export(df).head(limit)
        cols = list(display.columns)
        self._preview_table.setColumnCount(len(cols))
        self._preview_table.setRowCount(len(display))
        self._preview_table.setHorizontalHeaderLabels(cols)
        for r_idx, (_, row) in enumerate(display.iterrows()):
            for c_idx, col in enumerate(cols):
                val = row[col]
                if hasattr(val, "strftime"):
                    text = val.strftime("%d/%m/%Y")
                elif pd.isna(val) if not isinstance(val, str) else False:
                    text = ""
                else:
                    text = str(val)
                it = QTableWidgetItem(text)
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self._preview_table.setItem(r_idx, c_idx, it)
        self._preview_table.resizeColumnsToContents()

    # ─── Step 2: PESEL check ──────────────────────────────────────────────────

    def _on_check_pesels(self, checked: bool = False, *, auto_onboard: bool = True) -> None:
        if self._df_formatted is None:
            return
        del checked
        self._log("Sprawdzanie pracowników w bazie payroll system…")
        try:
            svc = DatabaseService(self._db_config_provider())
            ok, msg = svc.test_connection()
            if not ok:
                raise RuntimeError(f"Brak połączenia: {msg}")
            result = check_pesels_in_db(self._df_formatted, svc)
        except Exception as exc:
            self._log(f"BŁĄD sprawdzania pracowników: {exc}")
            self._lbl_check_status.setText(f"Błąd: {exc}")
            self._lbl_check_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")
            return

        self._check_result = result
        self._log(
            f"PESEL: znaleziono={result.found}/{result.total}, brak={len(result.missing)}"
        )

        if result.missing:
            self._missing_table.setRowCount(len(result.missing_rows))
            for r_idx, mr in enumerate(result.missing_rows):
                self._missing_table.setItem(r_idx, 0, _item(mr.get("PESEL", ""), _COLOR_WARN))
                self._missing_table.setItem(r_idx, 1, _item(mr.get("Pracownik", ""), _COLOR_WARN))
                self._missing_table.setItem(r_idx, 2, _item(mr.get("Nr Rachunku", ""), _COLOR_WARN))
                self._log(
                    f"  BRAK: PESEL={mr['PESEL']} | {mr['Pracownik']} | Nr={mr['Nr Rachunku']}"
                )
            self._missing_table.resizeColumnsToContents()
            self._missing_table.setVisible(True)
            self._lbl_check_status.setText(
                self._t("auto.step2.warn").format(
                    found=result.found, total=result.total, missing=len(result.missing)
                )
            )
            self._lbl_check_status.setStyleSheet(
                f"color:{_COLOR_WARN};font-size:9pt;font-weight:600;"
            )
            self._btn_onboard.setVisible(True)
            self._btn_onboard.setEnabled(True)
            self._btn_onboard.setText(
                self._t("auto.step2.onboard_btn").format(count=len(result.missing))
            )
            self._lbl_onboard_status.setText(self._t("auto.step2.onboard_hint"))
            self._lbl_onboard_status.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")

            is_api_data = (
                "__audit_data_source" in self._df_formatted.columns
                and (self._df_formatted["__audit_data_source"].astype(str).str.lower() == "api").any()
            )
            if auto_onboard and is_api_data and not self._auto_onboard_in_progress:
                self._log(
                    "Auto-onboarding: wykryto brakujących pracowników w API — "
                    "próbuję założyć karty z danych CRM."
                )
                self._on_onboard_missing(auto=True)
        else:
            self._missing_table.setVisible(False)
            self._btn_onboard.setVisible(False)
            self._lbl_onboard_status.setText("")
            self._lbl_check_status.setText(
                self._t("auto.step2.ok").format(found=result.found, total=result.total)
            )
            self._lbl_check_status.setStyleSheet(
                f"color:{_COLOR_OK};font-size:9pt;font-weight:600;"
            )
            self._log("Wszyscy pracownicy znalezieni w bazie.")

        self._btn_import.setEnabled(True)

    # ─── Step 2b: Auto-onboard missing employees from CRM ───────────────────

    def _on_onboard_missing(self, auto: bool = False) -> None:
        if self._check_result is None or not self._check_result.missing_rows:
            return
        if self._auto_onboard_in_progress:
            return
        self._auto_onboard_in_progress = True
        self._btn_onboard.setEnabled(False)
        if auto:
            self._log(
                self._t("auto.step2.onboard.auto_start").format(
                    count=len(self._check_result.missing_rows)
                )
            )
        else:
            self._log(self._t("auto.step2.onboard.start").format(count=len(self._check_result.missing_rows)))

        try:
            from crm.onboarding import (
                collect_onboarding_candidates,
                build_employee_import_rows,
            )
            settings = load_crm_api_settings()
            tenant_id = int(self._api_tenant.currentData() or 0)
            plan = collect_onboarding_candidates(
                self._check_result.missing_rows,
                settings,
                tenant_id=tenant_id if tenant_id else None,
            )
        except Exception as exc:
            self._log(f"BŁĄD onboardingu (CRM): {exc}")
            self._lbl_onboard_status.setText(f"Błąd: {exc}")
            self._lbl_onboard_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")
            self._btn_onboard.setEnabled(True)
            self._auto_onboard_in_progress = False
            return

        self._log(
            self._t("auto.step2.onboard.plan").format(
                ready=len(plan.can_onboard),
                blocked=len(plan.blocked),
                missing=len(plan.not_found_pesels),
            )
        )
        for cand in plan.blocked:
            self._log(
                f"  BLOKADA [{cand.source or 'brak'}] {cand.pesel} "
                f"{cand.full_name_label or '-'}: {'; '.join(cand.blockers)}"
            )
        for pesel in plan.not_found_pesels:
            self._log(f"  BRAK W CRM: PESEL={pesel}")
        for cand in plan.can_onboard:
            warn = f" | uwagi: {'; '.join(cand.warnings)}" if cand.warnings else ""
            self._log(
                f"  OK do utworzenia [{cand.source}] {cand.pesel} "
                f"{cand.full_name_label}{warn}"
            )

        if not plan.can_onboard:
            self._lbl_onboard_status.setText(self._t("auto.step2.onboard.nothing"))
            self._lbl_onboard_status.setStyleSheet(f"color:{_COLOR_WARN};font-size:9pt;")
            self._btn_onboard.setEnabled(False)
            self._auto_onboard_in_progress = False
            return

        if not auto:
            confirm = QMessageBox.question(
                self,
                self._t("auto.step2.onboard.confirm_title"),
                self._t("auto.step2.onboard.confirm_body").format(
                    ready=len(plan.can_onboard),
                    blocked=len(plan.blocked),
                    missing=len(plan.not_found_pesels),
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                self._btn_onboard.setEnabled(True)
                self._auto_onboard_in_progress = False
                return
        else:
            self._log(
                self._t("auto.step2.onboard.auto_confirm").format(
                    ready=len(plan.can_onboard),
                    blocked=len(plan.blocked),
                    missing=len(plan.not_found_pesels),
                )
            )

        try:
            svc = DatabaseService(self._db_config_provider())
            ok, msg = svc.test_connection()
            if not ok:
                raise RuntimeError(f"Brak połączenia: {msg}")

            data_od_clarion = self._current_clarion_data_od()
            rows = build_employee_import_rows(plan.can_onboard, data_od=data_od_clarion)
            stats = svc.execute_employee_import(
                rows=rows,
                start_urzad_id=_ONBOARD_START_URZAD_ID,
                id_firmy=_ONBOARD_ID_FIRMY,
                data_od=data_od_clarion,
            )
        except Exception as exc:
            self._log(f"BŁĄD utworzenia kart pracowników: {exc}")
            self._lbl_onboard_status.setText(f"Błąd: {exc}")
            self._lbl_onboard_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")
            self._btn_onboard.setEnabled(True)
            self._auto_onboard_in_progress = False
            return

        self._log(
            self._t("auto.step2.onboard.done").format(
                created=stats.created_employees,
                addresses=stats.created_addresses,
                urzedy=stats.created_urzedy,
                links=stats.created_links,
                skipped=stats.skipped_duplicates,
            )
        )
        self._lbl_onboard_status.setText(
            self._t("auto.step2.onboard.status_done").format(created=stats.created_employees)
        )
        self._lbl_onboard_status.setStyleSheet(f"color:{_COLOR_OK};font-size:9pt;font-weight:600;")

        # Re-run PESEL check so the user immediately sees the updated state.
        self._auto_onboard_in_progress = False
        self._on_check_pesels(auto_onboard=False)

    def _current_clarion_data_od(self) -> int:
        """First day of the API report period in Clarion format (days since 1800-12-28)."""
        from importer.utils import _to_clarion_date

        year = int(self._api_year.value())
        month = int(self._api_month.value())
        try:
            value = _to_clarion_date(f"01/{month:02d}/{year:04d}")
            if value is not None:
                return int(value)
        except Exception:
            pass
        # Fallback: today (matches payroll system's GETDATE() convention)
        from datetime import date as _date
        delta = (_date.today() - _date(1800, 12, 28)).days
        return int(delta)

    # ─── Step 3: Financial verification ──────────────────────────────────────

    def _on_verify(self) -> None:
        if self._df_formatted is None:
            return
        self._log("Weryfikacja obliczeń netto/brutto…")
        try:
            result = verify_financials(self._df_formatted)
        except Exception as exc:
            self._log(f"BŁĄD weryfikacji: {exc}")
            self._lbl_verify_status.setText(f"Błąd: {exc}")
            self._lbl_verify_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")
            return

        self._verify_result = result
        self._log(
            f"Weryfikacja: {result.total} wierszy — "
            f"OK={result.ok}, marginalne={result.marginal}, niezgodności={result.discrepancy}"
        )
        breakdown_parts = []
        if result.api_rows or result.excel_rows:
            breakdown_parts.append(f"API={result.api_rows}/Excel={result.excel_rows}")
        if result.zus_exempt_rows:
            breakdown_parts.append(f"ZUS-exempt={result.zus_exempt_rows}")
        if result.brutto_from_netto_rows:
            breakdown_parts.append(f"brutto_from_netto={result.brutto_from_netto_rows}")
        if result.pit_zero_rows:
            breakdown_parts.append(f"PIT=0={result.pit_zero_rows}")
        if breakdown_parts:
            self._log("  Rozkład: " + ", ".join(breakdown_parts))
        for row in result.rows:
            if not row.is_ok:
                src_label = f"[{row.data_source}]" if row.data_source else ""
                self._log(
                    f"  {'MARGINAL' if row.is_marginal else 'NIEZGODNOŚĆ'}: "
                    f"Nr={row.nr_rachunku} PESEL={row.pesel} Typ={row.typ} {src_label} "
                    f"Brutto={row.brutto:.2f} PIT={row.pit_rate:.1f}% — {row.note}"
                )

        if result.discrepancy > 0:
            self._lbl_verify_status.setText(
                self._t("auto.step3.err").format(
                    ok=result.ok, disc=result.discrepancy, total=result.total
                )
            )
            self._lbl_verify_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;font-weight:600;")
        elif result.marginal > 0:
            self._lbl_verify_status.setText(
                self._t("auto.step3.marginal").format(
                    ok=result.ok, marg=result.marginal, total=result.total
                )
            )
            self._lbl_verify_status.setStyleSheet(f"color:{_COLOR_WARN};font-size:9pt;font-weight:600;")
        else:
            self._lbl_verify_status.setText(
                self._t("auto.step3.ok").format(ok=result.ok, total=result.total)
            )
            self._lbl_verify_status.setStyleSheet(f"color:{_COLOR_OK};font-size:9pt;font-weight:600;")
            self._log("Weryfikacja OK — wszystkie kwoty zgodne.")

    # ─── Step 4: Dry-run ──────────────────────────────────────────────────────

    def _on_dryrun(self) -> None:
        if self._df_formatted is None:
            return
        self._log("Dry-run: check-in bez zapisu w bazie…")
        try:
            svc = DatabaseService(self._db_config_provider())
            ok, msg = svc.test_connection()
            if not ok:
                raise RuntimeError(f"Brak połączenia: {msg}")
            is_api_data = (
                "__audit_data_source" in self._df_formatted.columns
                and (self._df_formatted["__audit_data_source"].astype(str).str.lower() == "api").any()
            )
            if is_api_data:
                tenant_id = int(self._api_tenant.currentData() or 0)
                rachunki_report = reconcile_rachunki(
                    settings=load_crm_api_settings(),
                    db_service=svc,
                    year=int(self._api_year.value()),
                    month=int(self._api_month.value()),
                    tenant_id=tenant_id,
                )
                self._log(
                    "Rachunki 1:1 CRM↔payroll system: "
                    f"CRM paid={rachunki_report.crm_paid_total}, "
                    f"CRM importable={rachunki_report.crm_importable_total}, "
                    f"payroll system month={rachunki_report.payroll_month_total}, "
                    f"found-any-date={rachunki_report.matched_any_date}, "
                    f"same-month={rachunki_report.matched_same_month}, "
                    f"date-mismatch={len(rachunki_report.date_mismatch)}, "
                    f"missing-in-payroll system={len(rachunki_report.crm_missing_in_payroll)} "
                    f"(importable={len(rachunki_report.crm_missing_importable)}, "
                    f"blocked={len(rachunki_report.crm_missing_blocked)})"
                )
                if rachunki_report.date_mismatch:
                    for crm_bill, payroll_bill in rachunki_report.date_mismatch[:10]:
                        self._log(
                            f"  DATA RÓŻNA: {crm_bill.nr_rachunku} "
                            f"CRM paid={crm_bill.payment_date}, "
                            f"payroll system DATA_WYPLATY={payroll_bill.data_wyplaty}"
                        )
                if rachunki_report.crm_missing_in_payroll:
                    for item in rachunki_report.crm_missing_in_payroll[:10]:
                        self._log(
                            f"  CRM NIE MA W PAYROLL_DB [{item.status}/{item.reason or 'ok'}]: "
                            f"{item.nr_rachunku} {item.worker_name} PESEL={item.pesel}"
                        )
            df_clean = df_to_export(self._df_formatted)
            mapped_df = map_columns(
                df_clean, AUTO_MAPPING, UMOWY_MIXED_IMPORT_PROFILE,
                employee_lookup_mode="pesel",
            )
            checkin_result = check_in(
                mapped_df, db_service=svc, dry_run=True, data_od=0,
                profile=UMOWY_MIXED_IMPORT_PROFILE, employee_lookup_mode="pesel",
            )
        except Exception as exc:
            tb = traceback.format_exc()
            self._log(f"BŁĄD dry-run: {exc}\n{tb}")
            self._lbl_dryrun_status.setText(f"Błąd: {exc}")
            self._lbl_dryrun_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")
            return

        warnings_count = sum(1 for r in checkin_result.rows if r.status == RowStatus.WARNING)
        importable = len(checkin_result.importable_rows)
        self._log(
            f"Dry-run: importowalnych={importable}, błędów={checkin_result.errors}, "
            f"ostrzeżeń={warnings_count}"
        )

        # Populate check-in table (only non-OK rows to keep it manageable)
        non_ok = [r for r in checkin_result.rows if r.status != RowStatus.OK]
        if not non_ok:
            non_ok = checkin_result.rows  # show all if everything is fine
        self._checkin_table.setRowCount(len(non_ok))
        for r_idx, vrow in enumerate(non_ok):
            color = _status_color(vrow.status)
            icon = (
                _STATUS_OK if vrow.status == RowStatus.OK
                else _STATUS_WARN if vrow.status == RowStatus.WARNING
                else _STATUS_ERR
            )
            cells = [
                str(r_idx + 1),
                f"{icon} {vrow.status.value.upper()}",
                str(vrow.index),
                str(vrow.field_name or ""),
                vrow.message,
            ]
            for c_idx, text in enumerate(cells):
                it = _item(text, color if c_idx == 1 else None)
                self._checkin_table.setItem(r_idx, c_idx, it)
        self._checkin_table.resizeColumnsToContents()
        self._checkin_table.setVisible(True)

        if checkin_result.errors > 0:
            color = _COLOR_ERR
            msg = self._t("auto.step_dryrun.errors").format(
                ok=importable, err=checkin_result.errors, warn=warnings_count
            )
        elif warnings_count > 0:
            color = _COLOR_WARN
            msg = self._t("auto.step_dryrun.warnings").format(
                ok=importable, warn=warnings_count
            )
        else:
            color = _COLOR_OK
            msg = self._t("auto.step_dryrun.ok").format(ok=importable)

        self._lbl_dryrun_status.setText(msg)
        self._lbl_dryrun_status.setStyleSheet(f"color:{color};font-size:9pt;font-weight:600;")
        if importable > 0:
            self._btn_import.setEnabled(True)

    # ─── Step 5: Import ───────────────────────────────────────────────────────

    def _on_import(self) -> None:
        if self._df_formatted is None:
            return

        # ── Pre-import duplicate guard ────────────────────────────────────────
        nr_rachunki = list(
            df_to_export(self._df_formatted)["Nr Rachunku"].dropna().astype(str).unique()
        ) if "Nr Rachunku" in df_to_export(self._df_formatted).columns else []

        try:
            svc_check = DatabaseService(self._db_config_provider())
            ok_conn, msg_conn = svc_check.test_connection()
            if ok_conn and nr_rachunki:
                dup_count, dup_list = svc_check.count_existing_nr_rachunki(nr_rachunki)
                if dup_count > 0:
                    self._log(
                        f"DUPLIKATY: {dup_count} numerów rachunku już jest w bazie: "
                        + ", ".join(dup_list[:10])
                        + ("…" if len(dup_list) > 10 else "")
                    )
                    reply = QMessageBox.question(
                        self,
                        self._t("auto.import_confirm_title"),
                        self._t("auto.dup_check.found").format(n=dup_count),
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        return
                else:
                    self._log("Duplikaty: brak — plik nie był wcześniej importowany.")
        except Exception as exc:
            self._log(f"Ostrzeżenie: nie można sprawdzić duplikatów: {exc}")

        # ── Confirmation dialog ───────────────────────────────────────────────
        rows_total = len(self._df_formatted)
        missing_cnt = len(self._check_result.missing) if self._check_result else "?"
        disc_cnt = self._verify_result.discrepancy if self._verify_result else 0

        if isinstance(disc_cnt, int) and disc_cnt > 0:
            reply = QMessageBox.question(
                self,
                self._t("auto.import_confirm_title"),
                self._t("auto.import_confirm_disc").format(
                    disc=disc_cnt, total=rows_total
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
        else:
            reply = QMessageBox.question(
                self,
                self._t("auto.import_confirm_title"),
                self._t("auto.import_confirm").format(
                    total=rows_total, missing=missing_cnt
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # ── Build mapping + check-in on main thread ───────────────────────────
        self._save_formatted_file()
        self._log("Rozpoczynanie importu umów do payroll system…")
        self._btn_import.setEnabled(False)
        self._btn_cancel.setEnabled(True)
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(True)

        try:
            svc = DatabaseService(self._db_config_provider())
            ok_conn, msg_conn = svc.test_connection()
            if not ok_conn:
                raise RuntimeError(f"Brak połączenia z bazą: {msg_conn}")

            df_clean = df_to_export(self._df_formatted)
            mapped_df = map_columns(
                df_clean, AUTO_MAPPING, UMOWY_MIXED_IMPORT_PROFILE,
                employee_lookup_mode="pesel",
            )
            checkin_result = check_in(
                mapped_df, db_service=svc, dry_run=False, data_od=0,
                profile=UMOWY_MIXED_IMPORT_PROFILE, employee_lookup_mode="pesel",
            )
        except Exception as exc:
            tb = traceback.format_exc()
            self._log(f"BŁĄD przygotowania importu: {exc}\n{tb}")
            self._lbl_import_status.setText(f"Błąd: {exc}")
            self._lbl_import_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")
            self._btn_import.setEnabled(True)
            self._btn_cancel.setEnabled(False)
            self._progress_bar.setVisible(False)
            return

        warnings_count = sum(1 for r in checkin_result.rows if r.status == RowStatus.WARNING)
        rows_z = [r for r in checkin_result.importable_rows if int(r.get("typ_umowy_no") or 0) == 1]
        rows_d = [r for r in checkin_result.importable_rows if int(r.get("typ_umowy_no") or 0) == 2]
        self._log(
            f"CheckIn: UZ={len(rows_z)}, UD={len(rows_d)}, "
            f"błędów={checkin_result.errors}, ostrzeżeń={warnings_count}"
        )

        total_rows = len(rows_z) + len(rows_d)
        if total_rows == 0:
            self._log("Brak wierszy do zaimportowania — import przerwany.")
            self._lbl_import_status.setText("Brak wierszy do importu.")
            self._lbl_import_status.setStyleSheet(f"color:{_COLOR_WARN};font-size:9pt;")
            self._btn_import.setEnabled(True)
            self._btn_cancel.setEnabled(False)
            self._progress_bar.setVisible(False)
            return

        self._progress_bar.setMaximum(total_rows)

        from database import UmowyImportStats

        def _job(progress_cb, cancel_token, _svc=svc, _rz=rows_z, _rd=rows_d, _tot=total_rows):
            combined = UmowyImportStats()
            combined.created_contract_ids = []
            offset = 0
            if _rz:
                def _wrap_z(done, _t):
                    if progress_cb:
                        progress_cb(offset + done, _tot)
                s1 = _svc.execute_umowy_import(
                    _rz, id_firmy=None, progress_callback=_wrap_z, cancel_token=cancel_token
                )
                combined.created_contracts += s1.created_contracts
                combined.skipped_duplicates += s1.skipped_duplicates
                combined.missing_employees += s1.missing_employees
                if s1.created_contract_ids:
                    combined.created_contract_ids.extend(s1.created_contract_ids)
            offset = len(_rz)
            if _rd:
                def _wrap_d(done, _t):
                    if progress_cb:
                        progress_cb(offset + done, _tot)
                s2 = _svc.execute_umowy_dzielo_import(
                    _rd, id_firmy=None, progress_callback=_wrap_d, cancel_token=cancel_token
                )
                combined.created_contracts += s2.created_contracts
                combined.skipped_duplicates += s2.skipped_duplicates
                combined.missing_employees += s2.missing_employees
                if s2.created_contract_ids:
                    combined.created_contract_ids.extend(s2.created_contract_ids)
            return combined

        self._thread, self._worker = run_in_thread(self, _job)
        self._worker.progress.connect(self._on_import_progress)
        self._worker.finished.connect(self._on_import_finished)
        self._worker.failed.connect(self._on_import_failed)
        self._worker.cancelled.connect(self._on_import_cancelled)
        self._thread.start()

    def _on_import_progress(self, done: int, total: int) -> None:
        self._progress_bar.setMaximum(max(total, 1))
        self._progress_bar.setValue(done)

    def _on_cancel_import(self) -> None:
        if self._worker and hasattr(self._worker, "request_cancel"):
            self._worker.request_cancel()
            self._log("Żądanie anulowania importu…")
        self._btn_cancel.setEnabled(False)

    def _on_import_finished(self, stats: object) -> None:
        from database import UmowyImportStats
        combined: UmowyImportStats = stats  # type: ignore[assignment]

        self._last_import_ids = list(combined.created_contract_ids or [])
        self._log(
            f"Import zakończony: utworzono={combined.created_contracts}, "
            f"pominięto duplikatów={combined.skipped_duplicates}, "
            f"brak pracownika={combined.missing_employees}"
        )
        if self._last_import_ids:
            self._log(
                f"Nowe IDs umów ({len(self._last_import_ids)}): "
                + ", ".join(str(i) for i in self._last_import_ids[:15])
                + ("…" if len(self._last_import_ids) > 15 else "")
            )

        self._lbl_import_status.setText(
            self._t("auto.step4.ok").format(
                created=combined.created_contracts,
                dup=combined.skipped_duplicates,
                miss=combined.missing_employees,
            )
        )
        color = _COLOR_OK if combined.missing_employees == 0 else _COLOR_WARN
        self._lbl_import_status.setStyleSheet(f"color:{color};font-size:9pt;font-weight:600;")
        self._btn_import.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._progress_bar.setValue(self._progress_bar.maximum())

        # Save log and persist history record
        log_path = self._save_log_to_file(auto=True)
        self._save_history_record(combined, log_path)
        self._load_history_table()

    def _on_import_failed(self, message: str) -> None:
        self._log(f"BŁĄD importu:\n{message}")
        self._lbl_import_status.setText("Błąd — patrz log")
        self._lbl_import_status.setStyleSheet(f"color:{_COLOR_ERR};font-size:9pt;")
        self._btn_import.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._progress_bar.setVisible(False)

    def _on_import_cancelled(self) -> None:
        self._log("Import anulowany przez użytkownika.")
        self._lbl_import_status.setText("Import anulowany.")
        self._lbl_import_status.setStyleSheet(f"color:{_COLOR_MUTED};font-size:9pt;")
        self._btn_import.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._progress_bar.setVisible(False)

    # ─── History ──────────────────────────────────────────────────────────────

    def _save_history_record(self, stats: object, log_path: str) -> None:
        from database import UmowyImportStats
        combined: UmowyImportStats = stats  # type: ignore[assignment]
        fmt = self._fmt_result
        ver = self._verify_result
        try:
            rec = ImportHistoryRecord.new(
                source_file=Path(self._source_path or "").name,
                period_wyplaty_min=fmt.date_wyplaty_min if fmt else "",
                period_wyplaty_max=fmt.date_wyplaty_max if fmt else "",
                total_rows=fmt.total_rows if fmt else 0,
                ud_count=fmt.ud_count if fmt else 0,
                uz_count=fmt.uz_count if fmt else 0,
                created_contracts=combined.created_contracts,
                skipped_duplicates=combined.skipped_duplicates,
                missing_employees=combined.missing_employees,
                verify_ok=ver.ok if ver else 0,
                verify_marginal=ver.marginal if ver else 0,
                verify_discrepancy=ver.discrepancy if ver else 0,
                contract_ids=list(combined.created_contract_ids or []),
                log_file_path=log_path,
            )
            _hist.save_record(rec)
            self._last_history_id = rec.record_id
            self._log(f"Historia: rekord zapisany (ID: {rec.record_id[:8]}…)")
        except Exception as exc:
            self._log(f"Ostrzeżenie: nie udało się zapisać historii: {exc}")

    def _load_history_table(self) -> None:
        records = _hist.load_records()
        self._history_table.setRowCount(len(records))
        for r_idx, rec in enumerate(records):
            status_text = (
                self._t("auto.history.rolledback_label") if rec.rolledback
                else self._t("auto.history.active_label")
            )
            status_color = _COLOR_MUTED if rec.rolledback else _COLOR_OK
            cells = [
                (rec.timestamp_display, None),
                (rec.source_file, None),
                (rec.period_display, None),
                (str(rec.created_contracts), _COLOR_OK if rec.created_contracts > 0 else None),
                (str(rec.skipped_duplicates), _COLOR_WARN if rec.skipped_duplicates > 0 else None),
                (str(rec.missing_employees), _COLOR_ERR if rec.missing_employees > 0 else None),
                (rec.verify_status, (
                    _COLOR_ERR if rec.verify_discrepancy > 0
                    else _COLOR_WARN if rec.verify_marginal > 0
                    else _COLOR_OK if rec.verify_ok > 0 else None
                )),
                (str(len(rec.contract_ids)), None),
                (status_text, status_color),
            ]
            for c_idx, (text, color) in enumerate(cells):
                it = _item(text, color)
                self._history_table.setItem(r_idx, c_idx, it)
            # Store record_id in first column's user data
            it0 = self._history_table.item(r_idx, 0)
            if it0:
                it0.setData(Qt.ItemDataRole.UserRole, rec.record_id)

        self._history_table.resizeColumnsToContents()

    def _selected_history_record(self) -> Optional[ImportHistoryRecord]:
        row = self._history_table.currentRow()
        if row < 0:
            return None
        it = self._history_table.item(row, 0)
        if it is None:
            return None
        record_id = it.data(Qt.ItemDataRole.UserRole)
        return _hist.get_record(record_id) if record_id else None

    def _on_history_rollback(self) -> None:
        rec = self._selected_history_record()
        if rec is None:
            QMessageBox.information(
                self, self._t("auto.history.rollback_btn"),
                self._t("auto.history.no_selection")
            )
            return
        if not rec.can_rollback:
            QMessageBox.warning(
                self, self._t("auto.history.rollback_btn"),
                self._t("auto.history.rollback_no_ids")
            )
            return

        n = len(rec.contract_ids)
        reply = QMessageBox.question(
            self,
            self._t("auto.history.rollback_confirm_title"),
            self._t("auto.history.rollback_confirm").format(
                n=n, date=rec.timestamp_display, file=rec.source_file
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._log(
            f"Rollback: usuwanie {n} umów z importu {rec.timestamp_display} "
            f"(plik: {rec.source_file})…"
        )
        try:
            svc = DatabaseService(self._db_config_provider())
            ok, msg = svc.test_connection()
            if not ok:
                raise RuntimeError(f"Brak połączenia: {msg}")
            undo_stats = svc.undo_import_record(
                {"created_contract_ids": rec.contract_ids}
            )
            deleted = getattr(undo_stats, "deleted_contracts", n)
            self._log(
                f"Rollback zakończony: usunięto {deleted} umów "
                f"(z {n} planowanych)."
            )
            _hist.mark_rolledback(rec.record_id)
            self._load_history_table()
            QMessageBox.information(
                self, self._t("auto.history.rollback_confirm_title"),
                self._t("auto.history.rollback_done").format(n=deleted)
            )
        except Exception as exc:
            tb = traceback.format_exc()
            self._log(f"BŁĄD rollback: {exc}\n{tb}")
            QMessageBox.critical(
                self, self._t("auto.history.rollback_confirm_title"),
                f"Błąd: {exc}"
            )

    def _on_history_open_log(self) -> None:
        rec = self._selected_history_record()
        if rec is None:
            QMessageBox.information(
                self, self._t("auto.history.open_log_btn"),
                self._t("auto.history.no_selection")
            )
            return
        if not rec.log_file_path or not Path(rec.log_file_path).exists():
            QMessageBox.warning(
                self, self._t("auto.history.open_log_btn"),
                self._t("auto.history.no_log")
            )
            return
        try:
            if os.name == "nt":
                os.startfile(rec.log_file_path)
            else:
                subprocess.Popen(["xdg-open", rec.log_file_path])
        except Exception as exc:
            self._log(f"Nie można otworzyć logu: {exc}")

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _save_formatted_file(self) -> None:
        try:
            _IMPORT_FILES_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = _IMPORT_FILES_DIR / f"auto_crm_{ts}.xlsx"
            df_to_export(self._df_formatted).to_excel(str(out_path), index=False)
            self._log(f"Plik sformatowany zapisany: {out_path.name}")
        except Exception as exc:
            self._log(f"Ostrzeżenie: nie udało się zapisać pliku: {exc}")

    # ─── Log persistence ──────────────────────────────────────────────────────

    def _on_save_log(self) -> None:
        path = self._save_log_to_file(auto=False)
        if path:
            QMessageBox.information(
                self, self._t("common.done"),
                self._t("auto.log_saved").format(path=path),
            )

    def _on_open_logs_folder(self) -> None:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", str(_LOGS_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(_LOGS_DIR)])
        except Exception as exc:
            self._log(f"Nie można otworzyć folderu: {exc}")

    def _save_log_to_file(self, auto: bool = False) -> str:
        try:
            _LOGS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fpath = _LOGS_DIR / f"automatyzacja_{ts}.log"
            header = [
                "=== Log Automatyzacji CRM Import ===",
                f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"Plik źródłowy: {self._source_path or 'brak'}",
            ]
            if self._fmt_result:
                r = self._fmt_result
                header += [
                    f"Umowy: {r.total_rows} (UD={r.ud_count}, UZ={r.uz_count}), "
                    f"PPK={r.ppk_matched}, ostrzeżeń={len(r.warnings)}",
                ]
            if self._check_result:
                c = self._check_result
                header.append(
                    f"Pracownicy: {c.found}/{c.total} znaleziono, brak={len(c.missing)}"
                )
            if self._verify_result:
                v = self._verify_result
                header.append(
                    f"Weryfikacja: OK={v.ok}, marginalne={v.marginal}, niezgodności={v.discrepancy}"
                )
            if self._last_import_ids:
                header.append(
                    f"Zaimportowane IDs ({len(self._last_import_ids)}): "
                    + ", ".join(str(i) for i in self._last_import_ids)
                )
            header.append("=" * 60)

            with open(fpath, "w", encoding="utf-8") as f:
                f.write("\n".join(header) + "\n\n")
                f.write("\n".join(self._log_lines) + "\n\n")
                if self._verify_result and self._verify_result.discrepancy > 0:
                    f.write("=== NIEZGODNOŚCI OBLICZEŃ ===\n")
                    for row in self._verify_result.rows:
                        if not row.is_ok:
                            f.write(
                                f"Nr={row.nr_rachunku} PESEL={row.pesel} Typ={row.typ} "
                                f"Brutto={row.brutto:.2f} Netto_src={row.netto_source:.2f} "
                                f"Netto_calc={row.netto_calc:.2f} | {row.note}\n"
                            )
                if self._check_result and self._check_result.missing:
                    f.write("\n=== BRAKUJĄCY PRACOWNICY ===\n")
                    for mr in self._check_result.missing_rows:
                        f.write(
                            f"PESEL={mr['PESEL']} | {mr['Pracownik']} | {mr['Nr Rachunku']}\n"
                        )

            if not auto:
                self._log(f"Log zapisany: {fpath}")
            return str(fpath)
        except Exception as exc:
            self._log(f"Błąd zapisu logu: {exc}")
            return ""
