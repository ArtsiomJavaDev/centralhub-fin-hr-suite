from __future__ import annotations

import configparser
import json
import os
import re
import sys
import traceback
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from PyQt6.QtCore import QEventLoop, Qt
from PyQt6.QtGui import QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QInputDialog,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QFrame,
    QGroupBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from database import DatabaseService, DbConfig, UmowyImportStats
from secrets_store import decrypt_secret, encrypt_secret, looks_encrypted
from ui.db_overview_tab import DbOverviewTab
from ui.i18n import t as i18n_t
from ui.import_worker import run_in_thread
from ui.theme import APP_STYLESHEET
from ui.umowy_export_tab import UmowyExportTab
from ui.automatyzacja_tab import AutomatyzacjaTab
from crm.formatter import infer_pit_rate_from_podatek
from importer import (
    AVAILABLE_PROFILES,
    EMPLOYEE_IMPORT_PROFILE,
    EMPLOYEE_ADDRESS_IMPORT_PROFILE,
    LEGACY_URZEDY_PROFILE,
    PRZEPROWADZKI_IMPORT_PROFILE,
    UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE,
    UMOWY_DZIELO_IMPORT_PROFILE,
    UMOWY_IMPORT_PROFILE,
    UMOWY_MIXED_IMPORT_PROFILE,
    ImportProfile,
    RowStatus,
    check_in,
    map_columns,
    preview_dataframe,
    read_excel,
    read_excel_umowy_format,
    summarize_result,
)
from importer.umowy_ppk_pairs import merge_ppk_companion_rows_format
from importer.utils import _normalize_typ_umowy
from importer.col_utils import (
    FORMAT_UMOWY_DROP_COLS,
    _SKLADKI_NETTO_MIRROR_COL,
    FORMAT_UMOWY_RENAME_COLS,
    _FORMAT_TYP_UMOWY_COL,
    FORMAT_UMOWY_SKLADKI_COLUMNS,
    FORMAT_MODE_UMOWY,
    FORMAT_MODE_UMOWY_DZIELO,
    FORMAT_MODE_UMOWY_MIXED,
    FORMAT_MODE_UMOWY_BATCH,
    FORMAT_MODE_UBEZPIECZENIA,
    UBEZPIECZENIA_OUTPUT_COLUMNS,
    UBEZPIECZENIA_TYP_CONST,
    UBEZPIECZENIA_SPECIAL_PESELS,
    UBEZPIECZENIA_SOURCE_ALIASES,
    UMOWY_SPECIAL_PESELS,
    UMOWY_SPECIAL_CHOROBOWE_COLUMN,
    UMOWY_SPECIAL_CHOROBOWE_PERCENT,
    FORMAT_UMOWY_TYPE_VALUE_MAP,
    _normalize_column_name,
    _normalize_text_value,
    _compact_umowy_typ_alnum,
    _nonempty_umowy_typ_cell,
    _coalesce_typ_umowy_column_values,
    _map_umowy_typ_text_to_12,
    _normalize_pesel,
    _pesel_to_display,
    _to_float,
    _numeric_equal,
    _is_under_26_on_date_from_pesel,
    _find_column,
    _is_excel_unnamed_column,
    typ_umowy_kind,
)

def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()
CONFIG_PATH = APP_DIR / "config.ini"
IMPORT_HISTORY_PATH = APP_DIR / "import_history.json"
LOGS_DIR = APP_DIR / "Logi"
IMPORT_FILES_DIR = APP_DIR / "ImportFiles"
LOGO_PATH = APP_DIR / "design" / "image.png"
CRASH_LOG_PATH = LOGS_DIR / "crash.log"


DEFAULT_CONFIG: dict[str, dict[str, str]] = {
    "database": {
        "driver": "ODBC Driver 17 for SQL Server",
        "server": "localhost",
        "database": "PAYROLL_DB",
        "username": "",
        "password": "",
        "trusted_connection": "yes",
    },
    "app": {
        "start_urzad_id": "12",
        "log_file": "importer.log",
        "data_od": "",
        "strict_od_dnia": "no",
        "language": "ru",
    },
}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.resize(1100, 750)

        from _secrets import get_merged_config as _get_merged_config
        self.config = _get_merged_config(CONFIG_PATH)
        self._ensure_config_defaults()
        self.language = self.config["app"].get("language", "ru").strip().lower()
        if self.language not in {"ru", "pl"}:
            self.language = "ru"
        self.setWindowTitle(self._t("app.title"))

        self.source_df: Optional[pd.DataFrame] = None
        self.preview_df: Optional[pd.DataFrame] = None
        self.current_file: Optional[str] = None
        self.last_checkin_result = None
        self.active_profile: ImportProfile = EMPLOYEE_IMPORT_PROFILE
        self.current_log_path: Optional[Path] = None
        self.current_mapping: dict[str, str] = {}
        self._operation_in_progress = False

        self._apply_branding()
        self._apply_style()
        self._build_ui()
        self._load_settings_to_form()

    def _t(self, key: str, **kwargs: object) -> str:
        return i18n_t(self.language, key, **kwargs)

    def _profile_display_label(self, profile_key: str) -> str:
        mapping = {
            EMPLOYEE_IMPORT_PROFILE.key: self._t("import.type.employees"),
            EMPLOYEE_ADDRESS_IMPORT_PROFILE.key: self._t("import.type.employee_addresses"),
            UMOWY_IMPORT_PROFILE.key: self._t("import.type.umowy"),
            UMOWY_DZIELO_IMPORT_PROFILE.key: self._t("import.type.umowy_dzielo"),
            UMOWY_MIXED_IMPORT_PROFILE.key: self._t("import.type.umowy_mixed"),
            UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE.key: self._t("import.type.insurance"),
            PRZEPROWADZKI_IMPORT_PROFILE.key: self._t("import.type.przeprowadzki"),
            LEGACY_URZEDY_PROFILE.key: self._t("import.type.urzedy_link"),
        }
        return mapping.get(profile_key, AVAILABLE_PROFILES.get(profile_key, self.active_profile).label)

    def _ensure_config_defaults(self) -> None:
        changed = False
        for section, values in DEFAULT_CONFIG.items():
            if section not in self.config:
                self.config[section] = {}
                changed = True
            for key, default_value in values.items():
                if key not in self.config[section]:
                    self.config[section][key] = default_value
                    changed = True

        # Fill DATA_OD only at runtime; keep config readable.
        if not self.config["app"].get("data_od", "").strip():
            self.config["app"]["data_od"] = str(self._today_clarion())
            changed = True

        if changed or not CONFIG_PATH.exists():
            with CONFIG_PATH.open("w", encoding="utf-8") as cfg:
                self.config.write(cfg)

    def _apply_branding(self) -> None:
        if LOGO_PATH.exists():
            icon = QIcon(str(LOGO_PATH))
            if not icon.isNull():
                self.setWindowIcon(icon)

    def _apply_style(self) -> None:
        self.setStyleSheet(APP_STYLESHEET)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        header = QFrame()
        header.setObjectName("AppHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 12, 8)
        header_layout.setSpacing(10)
        logo_label = QLabel()
        logo_label.setFixedSize(44, 44)
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if LOGO_PATH.exists():
            pixmap = QPixmap(str(LOGO_PATH))
            if not pixmap.isNull():
                logo_label.setPixmap(
                    pixmap.scaled(
                        40,
                        40,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
        header_title = QLabel(self._t("app.title"))
        header_title.setStyleSheet("font-size: 12pt; font-weight: 700; color: #f8fafc;")
        header_subtitle = QLabel(self._t("app.subtitle"))
        header_subtitle.setStyleSheet("font-size: 9pt; color: #94a3b8;")
        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(2)
        title_box.addWidget(header_title)
        title_box.addWidget(header_subtitle)
        header_layout.addWidget(logo_label)
        header_layout.addLayout(title_box)
        header_layout.addStretch()
        root_layout.addWidget(header)

        tabs = QTabWidget()
        tabs.setMovable(False)
        tabs.addTab(self._build_import_tab(), self._t("tab.import"))
        tabs.addTab(self._build_employee_tools_tab(), self._t("tab.employees"))
        tabs.addTab(self._build_format_tab(), self._t("tab.format"))
        tabs.addTab(self._build_umowy_export_tab(), self._t("tab.umowy_export"))
        tabs.addTab(self._build_automatyzacja_tab(), self._t("tab.auto"))
        tabs.addTab(self._build_db_overview_tab(), self._t("tab.db"))
        tabs.addTab(self._build_logs_tab(), self._t("tab.logs"))
        tabs.addTab(self._build_settings_tab(), self._t("tab.settings"))
        root_layout.addWidget(tabs)
        self.setCentralWidget(root)

    def _build_import_tab(self) -> QWidget:
        tab = QWidget()
        outer_layout = QVBoxLayout(tab)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(10)
        self.kpi_file_rows = self._create_kpi_card(self._t("import.kpi.rows"), "0")
        self.kpi_ready_rows = self._create_kpi_card(self._t("import.kpi.ready"), "0")
        self.kpi_errors = self._create_kpi_card(self._t("import.kpi.errors"), "0")
        kpi_row.addWidget(self.kpi_file_rows["frame"])
        kpi_row.addWidget(self.kpi_ready_rows["frame"])
        kpi_row.addWidget(self.kpi_errors["frame"])
        layout.addLayout(kpi_row)

        source_group = QGroupBox(self._t("import.source"))
        source_layout = QVBoxLayout(source_group)
        import_type_row = QHBoxLayout()
        self.import_type_box = QComboBox()
        self.import_type_box.addItem(
            self._profile_display_label(EMPLOYEE_IMPORT_PROFILE.key),
            EMPLOYEE_IMPORT_PROFILE.key,
        )
        self.import_type_box.addItem(
            self._profile_display_label(EMPLOYEE_ADDRESS_IMPORT_PROFILE.key),
            EMPLOYEE_ADDRESS_IMPORT_PROFILE.key,
        )
        self.import_type_box.addItem(
            self._profile_display_label(UMOWY_IMPORT_PROFILE.key),
            UMOWY_IMPORT_PROFILE.key,
        )
        self.import_type_box.addItem(
            self._profile_display_label(UMOWY_DZIELO_IMPORT_PROFILE.key),
            UMOWY_DZIELO_IMPORT_PROFILE.key,
        )
        self.import_type_box.addItem(
            self._profile_display_label(UMOWY_MIXED_IMPORT_PROFILE.key),
            UMOWY_MIXED_IMPORT_PROFILE.key,
        )
        self.import_type_box.addItem(
            self._profile_display_label(UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE.key),
            UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE.key,
        )
        self.import_type_box.addItem(
            self._profile_display_label(PRZEPROWADZKI_IMPORT_PROFILE.key),
            PRZEPROWADZKI_IMPORT_PROFILE.key,
        )
        self.import_type_box.addItem(
            self._profile_display_label(LEGACY_URZEDY_PROFILE.key),
            LEGACY_URZEDY_PROFILE.key,
        )
        self.import_type_box.currentIndexChanged.connect(self._on_profile_changed)
        import_type_row.addWidget(QLabel(self._t("import.type")))
        import_type_row.addWidget(self.import_type_box)
        import_type_row.addStretch()
        source_layout.addLayout(import_type_row)

        firma_row = QHBoxLayout()
        self.firma_label = QLabel("Фирма")
        self.firma_combo = QComboBox()
        self.firma_combo.addItem("Fundacja Freedom Business Area (FBA)", 1)
        self.firma_combo.addItem("FBA Payroll Solutions Sp. z o.o.", 2)
        firma_row.addWidget(self.firma_label)
        firma_row.addWidget(self.firma_combo)
        firma_row.addStretch()
        source_layout.addLayout(firma_row)

        employee_lookup_row = QHBoxLayout()
        self.employee_lookup_label = QLabel("Привязка сотрудника")
        self.employee_lookup_mode_box = QComboBox()
        self.employee_lookup_mode_box.addItem("NR Ewidencyjny", "nr")
        self.employee_lookup_mode_box.addItem("PESEL", "pesel")
        self.employee_lookup_mode_box.currentIndexChanged.connect(
            self._on_employee_lookup_mode_changed
        )
        employee_lookup_row.addWidget(self.employee_lookup_label)
        employee_lookup_row.addWidget(self.employee_lookup_mode_box)
        employee_lookup_row.addStretch()
        source_layout.addLayout(employee_lookup_row)
        top_row = QHBoxLayout()
        self.choose_file_button = QPushButton(self._t("import.choose_file"))
        self.choose_file_button.setObjectName("SecondaryAction")
        self.choose_file_button.clicked.connect(self._choose_excel_file)
        self.file_label = QLabel(self._t("import.file_not_selected"))
        self.file_label.setWordWrap(True)
        self.file_label.setStyleSheet("color: #93a4c4;")
        top_row.addWidget(self.choose_file_button)
        top_row.addWidget(self.file_label)
        source_layout.addLayout(top_row)
        self._update_employee_lookup_visibility()
        self._update_firma_selector_visibility()
        layout.addWidget(source_group)

        mapping_group = QGroupBox(self._t("import.mapping"))
        mapping_group_layout = QVBoxLayout(mapping_group)
        mapping_group_layout.setContentsMargins(10, 14, 10, 10)
        mapping_group_layout.setSpacing(8)
        mapping_hint = QLabel(self._t("import.mapping_hint"))
        mapping_hint.setStyleSheet("color: #94a3b8;")
        mapping_group_layout.addWidget(mapping_hint)
        self.mapping_scroll = QScrollArea()
        self.mapping_scroll.setWidgetResizable(True)
        self.mapping_scroll.setMinimumHeight(260)
        self.mapping_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.mapping_container = QWidget()
        self.mapping_container.setObjectName("MappingContainer")
        self.mapping_container.setStyleSheet("background: #0b1220;")
        self.mapping_layout = QGridLayout(self.mapping_container)
        self.mapping_layout.setContentsMargins(2, 2, 2, 2)
        self.mapping_layout.setHorizontalSpacing(10)
        self.mapping_layout.setVerticalSpacing(8)
        self.mapping_scroll.setWidget(self.mapping_container)
        mapping_group_layout.addWidget(self.mapping_scroll)
        self.mapping_boxes: dict[str, QComboBox] = {}
        layout.addWidget(mapping_group)
        self._rebuild_mapping_ui()

        actions_group = QGroupBox(self._t("import.actions"))
        actions_row = QHBoxLayout(actions_group)
        actions_row.setContentsMargins(12, 18, 12, 12)
        actions_row.setSpacing(10)
        self.dry_run_checkbox = QCheckBox(self._t("import.dry_run"))
        self.dry_run_checkbox.setChecked(True)
        self.strict_od_dnia_checkbox = QCheckBox(self._t("import.strict_mode"))
        self.strict_od_dnia_checkbox.setChecked(False)
        self.strict_od_dnia_checkbox.toggled.connect(self._on_strict_od_dnia_toggled)
        self.strict_od_hint = QLabel(
            self._t("import.strict_hint")
        )
        self.strict_od_hint.setStyleSheet("color: #f59e0b; font-size: 9pt; font-weight: 600;")
        self.strict_od_hint.setVisible(False)
        self.checkin_button = QPushButton(self._t("import.check"))
        self.checkin_button.setObjectName("SecondaryAction")
        self.checkin_button.clicked.connect(self._run_checkin)
        self.execute_button = QPushButton(self._t("import.execute"))
        self.execute_button.setObjectName("PrimaryAction")
        self.execute_button.clicked.connect(self._run_execute_import_stub)
        self.verify_umowy_button = QPushButton(self._t("import.verify_umowy"))
        self.verify_umowy_button.setObjectName("SecondaryAction")
        self.verify_umowy_button.clicked.connect(self._run_verify_umowy_all)
        self.rollback_button = QPushButton(self._t("import.rollback"))
        self.rollback_button.setObjectName("DangerAction")
        self.rollback_button.clicked.connect(self._rollback_last_import)
        actions_row.addWidget(self.dry_run_checkbox)
        actions_row.addWidget(self.strict_od_dnia_checkbox)
        actions_row.addWidget(self.strict_od_hint)
        actions_row.addStretch()
        actions_row.addWidget(self.verify_umowy_button)
        actions_row.addWidget(self.rollback_button)
        actions_row.addWidget(self.checkin_button)
        actions_row.addWidget(self.execute_button)
        layout.addWidget(actions_group)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet("color: #334155;")
        layout.addWidget(divider)

        preview_label = QLabel(self._t("import.preview"))
        preview_label.setStyleSheet("font-size: 10pt; font-weight: 600; color: #e2e8f0;")
        layout.addWidget(preview_label)

        self.table = QTableWidget()
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(320)
        layout.addWidget(self.table)
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        return tab

    def _build_employee_tools_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        status_group = QGroupBox(self._t("employees.status"))
        status_layout = QGridLayout(status_group)
        status_layout.setContentsMargins(12, 18, 12, 12)
        status_layout.setHorizontalSpacing(10)
        status_layout.setVerticalSpacing(8)

        self.status_mode_box = QComboBox()
        self.status_mode_box.addItem(self._t("employees.mode_all"), "all")
        self.status_mode_box.addItem(self._t("employees.mode_selected"), "selected")
        self.status_mode_box.currentIndexChanged.connect(self._toggle_status_numbers_input)

        self.status_from_input = QLineEdit("2")
        self.status_to_input = QLineEdit("1")
        self.status_numbers_input = QTextEdit()
        self.status_numbers_input.setMinimumHeight(100)
        self.status_numbers_input.setPlaceholderText(self._t("employees.numbers_placeholder"))
        self.status_apply_button = QPushButton(self._t("employees.apply"))
        self.status_apply_button.setObjectName("PrimaryAction")
        self.status_apply_button.clicked.connect(self._run_change_employee_status)

        status_layout.addWidget(QLabel(self._t("employees.mode")), 0, 0)
        status_layout.addWidget(self.status_mode_box, 0, 1)
        status_layout.addWidget(QLabel(self._t("employees.from_status")), 1, 0)
        status_layout.addWidget(self.status_from_input, 1, 1)
        status_layout.addWidget(QLabel(self._t("employees.to_status")), 2, 0)
        status_layout.addWidget(self.status_to_input, 2, 1)
        status_layout.addWidget(QLabel(self._t("employees.numbers")), 3, 0)
        status_layout.addWidget(self.status_numbers_input, 3, 1)
        status_layout.addWidget(self.status_apply_button, 4, 1)
        layout.addWidget(status_group)
        self._toggle_status_numbers_input()
        layout.addStretch()

        return tab

    def _build_format_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel(self._t("format.title"))
        title.setStyleSheet("font-size: 11pt; font-weight: 700; color: #e2e8f0;")
        layout.addWidget(title)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel(self._t("format.mode")))
        self.format_mode_box = QComboBox()
        self.format_mode_box.addItem(self._t("format.mode.umowy"), FORMAT_MODE_UMOWY)
        self.format_mode_box.addItem(
            self._t("format.mode.umowy_dzielo"), FORMAT_MODE_UMOWY_DZIELO
        )
        self.format_mode_box.addItem(
            self._t("format.mode.umowy_mixed"), FORMAT_MODE_UMOWY_MIXED
        )
        self.format_mode_box.addItem(
            self._t("format.mode.umowy_batch"), FORMAT_MODE_UMOWY_BATCH
        )
        self.format_mode_box.addItem(
            self._t("format.mode.ubezpieczenia"), FORMAT_MODE_UBEZPIECZENIA
        )
        self.format_mode_box.currentIndexChanged.connect(self._on_format_mode_changed)
        mode_row.addWidget(self.format_mode_box)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        self.format_hint_label = QLabel(self._t("format.hint.umowy"))
        self.format_hint_label.setWordWrap(True)
        self.format_hint_label.setStyleSheet("color: #94a3b8;")
        layout.addWidget(self.format_hint_label)

        source_group = QGroupBox(self._t("format.source"))
        source_layout = QHBoxLayout(source_group)
        source_layout.setContentsMargins(12, 18, 12, 12)
        source_layout.setSpacing(10)
        self.format_choose_button = QPushButton(self._t("format.choose_file"))
        self.format_choose_button.setObjectName("SecondaryAction")
        self.format_choose_button.clicked.connect(self._choose_format_source_file)
        self.format_file_label = QLabel(self._t("format.file_not_selected"))
        self.format_file_label.setWordWrap(True)
        self.format_file_label.setStyleSheet("color: #93a4c4;")
        source_layout.addWidget(self.format_choose_button)
        source_layout.addWidget(self.format_file_label, 1)
        layout.addWidget(source_group)

        output_group = QGroupBox(self._t("format.output_group"))
        output_layout = QHBoxLayout(output_group)
        output_layout.setContentsMargins(12, 18, 12, 12)
        output_layout.setSpacing(10)
        output_layout.addWidget(QLabel(self._t("format.output_name")))
        self.format_output_name_input = QLineEdit()
        self.format_output_name_input.setPlaceholderText(self._t("format.output_name_placeholder"))
        output_layout.addWidget(self.format_output_name_input, 1)
        self.format_save_button = QPushButton(self._t("format.save_button"))
        self.format_save_button.setObjectName("PrimaryAction")
        self.format_save_button.clicked.connect(self._run_format_save)
        output_layout.addWidget(self.format_save_button)
        self.format_open_folder_button = QPushButton(self._t("format.open_folder"))
        self.format_open_folder_button.setObjectName("SecondaryAction")
        self.format_open_folder_button.clicked.connect(self._open_import_files_folder)
        output_layout.addWidget(self.format_open_folder_button)
        layout.addWidget(output_group)

        preview_label = QLabel(self._t("format.preview"))
        preview_label.setStyleSheet("font-size: 10pt; font-weight: 600; color: #e2e8f0;")
        layout.addWidget(preview_label)

        self.format_table = QTableWidget()
        self.format_table.setAlternatingRowColors(False)
        self.format_table.verticalHeader().setVisible(False)
        self.format_table.setMinimumHeight(280)
        layout.addWidget(self.format_table)

        self.format_source_df: Optional[pd.DataFrame] = None
        self.format_source_path: Optional[str] = None
        self.format_result_df: Optional[pd.DataFrame] = None
        self.format_batch_paths: list[str] = []

        return tab

    def _build_umowy_export_tab(self) -> QWidget:
        return UmowyExportTab(
            db_config_provider=self._read_db_config_from_form,
            log_callback=self._log,
            translate=self._t,
        )

    def _build_automatyzacja_tab(self) -> QWidget:
        return AutomatyzacjaTab(
            db_config_provider=self._read_db_config_from_form,
            log_callback=self._log,
            translate=self._t,
        )

    def _choose_format_source_file(self) -> None:
        if self._current_format_mode() == FORMAT_MODE_UMOWY_BATCH:
            paths, _ = QFileDialog.getOpenFileNames(
                self,
                self._t("format.choose_files"),
                "",
                "Excel Files (*.xlsx *.xls)",
            )
            if not paths:
                return
            self.format_batch_paths = [str(p) for p in paths]
            first = self.format_batch_paths[0]
            try:
                df, sheet_names = read_excel_umowy_format(first)
                if len(sheet_names) > 1:
                    self._log(
                        "[format] pierwszy plik (preview): scalono arkusze "
                        f"{sheet_names} → {len(df)} surowych wierszy"
                    )
            except Exception as exc:
                QMessageBox.critical(self, "Ошибка чтения", str(exc))
                self._log(f"[format] Ошибка чтения файла: {exc}")
                self.format_batch_paths = []
                return
            self.format_source_df = df
            self.format_source_path = first
            n = len(self.format_batch_paths)
            self.format_file_label.setText(self._t("format.batch_selected", count=n))
            self.format_file_label.setToolTip("\n".join(self.format_batch_paths))
            self._log(self._t("format.batch_log", count=n, first=first, rows=len(df)))
            if not self.format_output_name_input.text().strip():
                self.format_output_name_input.setText("batch_merged")
            self._refresh_format_preview()
            return

        self.format_batch_paths = []
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self._t("format.choose_file"),
            "",
            "Excel Files (*.xlsx *.xls)",
        )
        if not file_path:
            return
        try:
            if self._current_format_mode() == FORMAT_MODE_UBEZPIECZENIA:
                df = read_excel(file_path)
            else:
                df, sheet_names = read_excel_umowy_format(file_path)
                if len(sheet_names) > 1:
                    self._log(
                        "[format] scalono arkusze "
                        f"{sheet_names} → {len(df)} surowych wierszy ({file_path})"
                    )
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка чтения", str(exc))
            self._log(f"[format] Ошибка чтения файла: {exc}")
            return

        self.format_source_df = df
        self.format_source_path = file_path
        self.format_file_label.setText(self._format_path_for_label(file_path))
        self.format_file_label.setToolTip(file_path)
        self._log(self._t("format.loaded", path=file_path, rows=len(df)))

        if not self.format_output_name_input.text().strip():
            self.format_output_name_input.setText(Path(file_path).stem)

        self._refresh_format_preview()

    def _current_format_mode(self) -> str:
        mode = self.format_mode_box.currentData()
        if mode in (
            FORMAT_MODE_UMOWY,
            FORMAT_MODE_UMOWY_DZIELO,
            FORMAT_MODE_UMOWY_MIXED,
            FORMAT_MODE_UMOWY_BATCH,
            FORMAT_MODE_UBEZPIECZENIA,
        ):
            return str(mode)
        return FORMAT_MODE_UMOWY

    def _on_format_mode_changed(self) -> None:
        mode = self._current_format_mode()
        if mode == FORMAT_MODE_UMOWY:
            hint_key = "format.hint.umowy"
        elif mode == FORMAT_MODE_UMOWY_DZIELO:
            hint_key = "format.hint.umowy_dzielo"
        elif mode == FORMAT_MODE_UMOWY_MIXED:
            hint_key = "format.hint.umowy_mixed"
        elif mode == FORMAT_MODE_UMOWY_BATCH:
            hint_key = "format.hint.umowy_batch"
        else:
            hint_key = "format.hint.ubezpieczenia"
        self.format_hint_label.setText(self._t(hint_key))
        if mode != FORMAT_MODE_UMOWY_BATCH:
            self.format_batch_paths = []
            if self.format_source_path:
                self.format_file_label.setText(
                    self._format_path_for_label(self.format_source_path)
                )
                self.format_file_label.setToolTip(self.format_source_path)
            else:
                self.format_file_label.setText(self._t("format.file_not_selected"))
                self.format_file_label.setToolTip("")
        self.format_choose_button.setText(
            self._t("format.choose_files")
            if mode == FORMAT_MODE_UMOWY_BATCH
            else self._t("format.choose_file")
        )
        self.format_output_name_input.setPlaceholderText(
            self._t("format.output_name_placeholder")
        )
        if self.format_source_df is not None:
            self._refresh_format_preview()

    def _refresh_format_preview(self) -> None:
        if self.format_source_df is None:
            return
        try:
            result_df, dropped, renamed = self._transform_current(self.format_source_df)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка форматирования", str(exc))
            self._log(f"[format] Ошибка трансформации: {exc}")
            self.format_result_df = None
            return

        self.format_result_df = result_df
        self._fill_format_table(result_df)
        if dropped:
            self._log(self._t("format.dropped", cols=", ".join(dropped)))
        if renamed:
            renamed_str = ", ".join(f"{src} → {dst}" for src, dst in renamed)
            self._log(self._t("format.renamed", cols=renamed_str))

    def _transform_current(
        self, df: pd.DataFrame
    ) -> tuple[pd.DataFrame, list[str], list[tuple[str, str]]]:
        mode = self._current_format_mode()
        if mode == FORMAT_MODE_UBEZPIECZENIA:
            return self._transform_ubezpieczenia_dataframe(df)
        dzielo = mode == FORMAT_MODE_UMOWY_DZIELO
        mixed = mode == FORMAT_MODE_UMOWY_MIXED or mode == FORMAT_MODE_UMOWY_BATCH
        return self._transform_umowy_dataframe(
            df,
            dzielo_format=dzielo,
            mixed_format=mixed,
        )

    def _transform_umowy_dataframe(
        self,
        df: pd.DataFrame,
        dzielo_format: bool = False,
        mixed_format: bool = False,
    ) -> tuple[pd.DataFrame, list[str], list[tuple[str, str]]]:
        new_columns: list[str] = []
        new_data: dict[str, object] = {}
        dropped: list[str] = []
        renamed: list[tuple[str, str]] = []
        normalized_drop_cols = {_normalize_column_name(col) for col in FORMAT_UMOWY_DROP_COLS}
        normalized_rename_cols = {
            _normalize_column_name(source): target
            for source, target in FORMAT_UMOWY_RENAME_COLS.items()
        }

        # Detect name column regardless of exact header — matches "Pracownik",
        # "Zleceniobiorca", "Imię i Nazwisko", etc. (same rules as _find_name_in_row).
        from importer.umowy_ppk_pairs import _NAME_COLS_NORM as _ppk_name_norms
        _name_col_norm_extra = frozenset({"pracow", "zleceniob", "osoba"})
        _ppk_name_col: Optional[str] = None
        for _c in df.columns:
            _ck = _normalize_column_name(str(_c))
            if _ck in _ppk_name_norms:
                _ppk_name_col = str(_c)
                break
        if _ppk_name_col is None:
            for _c in df.columns:
                _cl = str(_c).lower()
                if any(kw in _cl for kw in ("pracow", "zleceniob", "osoba")):
                    _ppk_name_col = str(_c)
                    break

        for col in df.columns:
            key = _normalize_column_name(str(col))
            if key in normalized_drop_cols or _is_excel_unnamed_column(col):
                # Zachowaj kolumnę z imieniem/nazwiskiem pod wewnętrzną nazwą dla PPK.
                if key == "pracownik" or (
                    _ppk_name_col is not None and str(col) == _ppk_name_col
                ):
                    new_name = "__ppk_match_osoba"
                    if new_name not in new_columns:
                        new_columns.append(new_name)
                        new_data[new_name] = df[col].values
                        renamed.append((str(col), new_name))
                    continue
                if key == "kwotanetto":
                    if _SKLADKI_NETTO_MIRROR_COL not in new_columns:
                        new_columns.append(_SKLADKI_NETTO_MIRROR_COL)
                        new_data[_SKLADKI_NETTO_MIRROR_COL] = df[col].values
                dropped.append(str(col))
                continue
            rename_pair: tuple[str, str] | None = None
            if key in normalized_rename_cols:
                new_name = normalized_rename_cols[key]
                rename_pair = (str(col), new_name)
            elif key == "kwotabrutto":
                new_name = "Kwota brutto"
                if str(col) != new_name:
                    rename_pair = (str(col), new_name)
            else:
                new_name = str(col)

            # payroll system often has both "Typ" (PPK + umowy) and "Typ umowy" / "Rodzaj umowy"
            # (sometimes empty on PPK lines). Second column used to overwrite the dict and
            # erased "PPK" before merge — coalesce per row instead.
            if new_name == _FORMAT_TYP_UMOWY_COL:
                vals = df[col].values
                if _FORMAT_TYP_UMOWY_COL not in new_data:
                    new_columns.append(_FORMAT_TYP_UMOWY_COL)
                    new_data[_FORMAT_TYP_UMOWY_COL] = vals
                    if rename_pair is not None:
                        renamed.append(rename_pair)
                else:
                    new_data[_FORMAT_TYP_UMOWY_COL] = _coalesce_typ_umowy_column_values(
                        new_data[_FORMAT_TYP_UMOWY_COL], vals
                    )
                    renamed.append((str(col), f"{_FORMAT_TYP_UMOWY_COL} (scala z {str(col)!r})"))
                continue

            new_columns.append(new_name)
            new_data[new_name] = df[col].values
            if rename_pair is not None:
                renamed.append(rename_pair)

        if not new_columns:
            raise ValueError(self._t("format.error_no_columns"))

        # If name column wasn't in DROP_COLS, copy it as __ppk_match_osoba now.
        if "__ppk_match_osoba" not in new_columns and _ppk_name_col is not None:
            if _ppk_name_col in new_data:
                new_columns.append("__ppk_match_osoba")
                new_data["__ppk_match_osoba"] = new_data[_ppk_name_col]

        result_df = pd.DataFrame(new_data, columns=new_columns)

        # ── PPK merge with full diagnostic log ──
        _ppk_debug: list[str] = []
        result_df = merge_ppk_companion_rows_format(result_df, debug_log=_ppk_debug)
        for _msg in _ppk_debug:
            self._log(_msg)

        # payroll system / niektóre eksporty zapisują kwotę PPK w kolumnie "Podatek" (wiersz Typ=PPK);
        # kolumna musi przetrwać merge, potem jak wcześniej — usuń z końcowej tabeli.
        podatek_cols = [
            c
            for c in result_df.columns
            if _normalize_column_name(str(c)) == "podatek"
        ]
        podatek_source = result_df[podatek_cols[0]].copy() if podatek_cols else None
        if podatek_cols:
            result_df = result_df.drop(columns=podatek_cols)

        # Summary after merge.
        _ppk_out_col = "PPK pracownika PLN"
        if _ppk_out_col in result_df.columns:
            _nonzero = int((result_df[_ppk_out_col].fillna(0) != 0).sum())
            _total = float(result_df[_ppk_out_col].fillna(0).sum())
            self._log(f"[format/ppk] wynik: {_nonzero} wierszy z PPK≠0, suma={_total:.2f}")
        else:
            self._log("[format/ppk] ⚠ kolumna 'PPK pracownika PLN' nie powstała")

        typ_col = "Typ umowy"
        if dzielo_format:
            if typ_col not in result_df.columns:
                result_df[typ_col] = "2"
            else:
                result_df[typ_col] = result_df[typ_col].apply(self._map_umowy_type_value)
            bad_rows: list[int] = []
            for pos, idx in enumerate(result_df.index, start=1):
                if typ_umowy_kind(result_df.at[idx, typ_col]) == 1:
                    bad_rows.append(pos)
            if bad_rows:
                raise ValueError(
                    self._t("format.error.umowy_dzielo_typ", rows=", ".join(map(str, bad_rows)))
                )
            result_df[typ_col] = "2"
        elif mixed_format:
            if typ_col not in result_df.columns:
                raise ValueError(self._t("format.error.umowy_mixed_no_typ"))
            result_df[typ_col] = result_df[typ_col].apply(self._map_umowy_type_value)
            bad_mixed: list[int] = []
            for pos, idx in enumerate(result_df.index, start=1):
                if typ_umowy_kind(result_df.at[idx, typ_col]) not in (1, 2):
                    bad_mixed.append(pos)
            if bad_mixed:
                raise ValueError(
                    self._t(
                        "format.error.umowy_mixed_typ",
                        rows=", ".join(map(str, bad_mixed)),
                    )
                )
        else:
            if typ_col in result_df.columns:
                result_df[typ_col] = result_df[typ_col].apply(self._map_umowy_type_value)
                bad_rows_z: list[int] = []
                for pos, idx in enumerate(result_df.index, start=1):
                    if typ_umowy_kind(result_df.at[idx, typ_col]) == 2:
                        bad_rows_z.append(pos)
                if bad_rows_z:
                    raise ValueError(
                        self._t(
                            "format.error.umowy_zlecenie_typ",
                            rows=", ".join(map(str, bad_rows_z)),
                        )
                    )
        result_df["Forma Opodtkowania"] = 1

        # Auto-fill SKŁADKI: zlecenie (per row); dzieło — wszystkie 0%; tryb mieszany — wg Typ umowy.
        # Must use result_df (after PPK merge): row counts no longer match the raw DataFrame df.
        pes_col_r = _find_column(result_df, UBEZPIECZENIA_SOURCE_ALIASES["pesel"])
        brutto_col_r = _find_column(result_df, UBEZPIECZENIA_SOURCE_ALIASES["kwota_brutto"])
        if _SKLADKI_NETTO_MIRROR_COL in result_df.columns:
            netto_col_r: Optional[str] = _SKLADKI_NETTO_MIRROR_COL
        else:
            netto_col_r = _find_column(result_df, UBEZPIECZENIA_SOURCE_ALIASES["kwota_netto"])
        kup_col_r = _find_column(result_df, UBEZPIECZENIA_SOURCE_ALIASES["kup"])
        dz_col_r = _find_column(result_df, UBEZPIECZENIA_SOURCE_ALIASES["data_zawarcia"])
        has_brutto_netto = brutto_col_r is not None and netto_col_r is not None

        for col_name, _ in FORMAT_UMOWY_SKLADKI_COLUMNS:
            result_df[col_name] = 0.0

        if dzielo_format:
            pass
        elif mixed_format:

            def _fill_skladki_row_zlecenie(i: int) -> None:
                row = result_df.iloc[i]
                pesel_norm = (
                    _normalize_pesel(row[pes_col_r]) if pes_col_r is not None else ""
                )
                force_special_chorob = pesel_norm in UMOWY_SPECIAL_PESELS
                all_zero = (
                    not force_special_chorob
                    and has_brutto_netto
                    and kup_col_r is not None
                    and dz_col_r is not None
                    and brutto_col_r is not None
                    and netto_col_r is not None
                    and _numeric_equal(row[kup_col_r], 0)
                    and _is_under_26_on_date_from_pesel(
                        row[pes_col_r] if pes_col_r is not None else "",
                        row[dz_col_r],
                    )
                    and _numeric_equal(row[brutto_col_r], row[netto_col_r])
                )
                if all_zero:
                    return
                row_idx = result_df.index[i]
                for col_name, default_value in FORMAT_UMOWY_SKLADKI_COLUMNS:
                    if force_special_chorob and col_name == UMOWY_SPECIAL_CHOROBOWE_COLUMN:
                        result_df.at[row_idx, col_name] = UMOWY_SPECIAL_CHOROBOWE_PERCENT
                    else:
                        result_df.at[row_idx, col_name] = default_value

            for i in range(len(result_df.index)):
                idx = result_df.index[i]
                if typ_umowy_kind(result_df.at[idx, typ_col]) == 2:
                    continue
                _fill_skladki_row_zlecenie(i)
        else:
            for i in range(len(result_df.index)):
                row = result_df.iloc[i]
                pesel_norm = (
                    _normalize_pesel(row[pes_col_r]) if pes_col_r is not None else ""
                )
                force_special_chorob = pesel_norm in UMOWY_SPECIAL_PESELS
                all_zero = (
                    not force_special_chorob
                    and has_brutto_netto
                    and kup_col_r is not None
                    and dz_col_r is not None
                    and brutto_col_r is not None
                    and netto_col_r is not None
                    and _numeric_equal(row[kup_col_r], 0)
                    and _is_under_26_on_date_from_pesel(
                        row[pes_col_r] if pes_col_r is not None else "",
                        row[dz_col_r],
                    )
                    and _numeric_equal(row[brutto_col_r], row[netto_col_r])
                )
                if all_zero:
                    continue
                rid = result_df.index[i]
                for col_name, default_value in FORMAT_UMOWY_SKLADKI_COLUMNS:
                    if force_special_chorob and col_name == UMOWY_SPECIAL_CHOROBOWE_COLUMN:
                        result_df.at[rid, col_name] = UMOWY_SPECIAL_CHOROBOWE_PERCENT
                    else:
                        result_df.at[rid, col_name] = default_value

        if _SKLADKI_NETTO_MIRROR_COL in result_df.columns:
            result_df = result_df.drop(columns=[_SKLADKI_NETTO_MIRROR_COL])

        if podatek_source is not None and brutto_col_r is not None:
            tax_rates: list[float] = []
            for idx in result_df.index:
                row = result_df.loc[idx]
                rates = {col_name: float(row.get(col_name, 0) or 0) for col_name, _ in FORMAT_UMOWY_SKLADKI_COLUMNS}
                tax_rates.append(
                    infer_pit_rate_from_podatek(
                        podatek_source.loc[idx],
                        row.get(brutto_col_r),
                        row.get(kup_col_r) if kup_col_r is not None else "0%",
                        rates,
                    )
                )
            result_df["Stawka podatku [%]"] = tax_rates

        pesel_col_name = None
        for column in result_df.columns:
            if _normalize_column_name(str(column)) == "pesel":
                pesel_col_name = column
                break
        if pesel_col_name is not None:
            result_df[pesel_col_name] = result_df[pesel_col_name].apply(_pesel_to_display)

        return result_df, dropped, renamed

    def _map_umowy_type_value(self, value: object) -> object:
        return _map_umowy_typ_text_to_12(value)

    def _transform_ubezpieczenia_dataframe(
        self, df: pd.DataFrame
    ) -> tuple[pd.DataFrame, list[str], list[tuple[str, str]]]:
        pesel_col = _find_column(df, UBEZPIECZENIA_SOURCE_ALIASES["pesel"])
        nr_umowy_col = _find_column(df, UBEZPIECZENIA_SOURCE_ALIASES["nr_umowy"])
        data_zawarcia_col = _find_column(df, UBEZPIECZENIA_SOURCE_ALIASES["data_zawarcia"])
        kwota_netto_col = _find_column(df, UBEZPIECZENIA_SOURCE_ALIASES["kwota_netto"])
        kwota_brutto_col = _find_column(df, UBEZPIECZENIA_SOURCE_ALIASES["kwota_brutto"])
        kup_col = _find_column(df, UBEZPIECZENIA_SOURCE_ALIASES["kup"])
        podatek_col = _find_column(df, UBEZPIECZENIA_SOURCE_ALIASES["podatek"])

        missing: list[str] = []
        if pesel_col is None:
            missing.append("PESEL")
        if nr_umowy_col is None:
            missing.append("Numer umowy")
        if data_zawarcia_col is None:
            missing.append("Data zawarcia umowy")
        if missing:
            raise ValueError("Отсутствуют обязательные колонки: " + ", ".join(missing))

        has_zero_check_cols = all(
            col is not None
            for col in (kwota_brutto_col, kwota_netto_col, kup_col, podatek_col)
        )

        out_rows: list[dict[str, object]] = []
        for _, row in df.iterrows():
            pesel_raw = row[pesel_col]
            pesel_norm = _normalize_pesel(pesel_raw)
            pesel_out = pesel_norm if pesel_norm else pesel_raw
            nr_umowy_val = row[nr_umowy_col]
            data_zawarcia_val = row[data_zawarcia_col]

            if pesel_norm in UBEZPIECZENIA_SPECIAL_PESELS:
                em, rnt, wyp, chor = 1, 1, 1, 1
            elif (
                has_zero_check_cols
                and _numeric_equal(row[kwota_brutto_col], row[kwota_netto_col])
                and _numeric_equal(row[kup_col], row[podatek_col])
                and _numeric_equal(row[kup_col], 0)
                and _numeric_equal(row[podatek_col], 0)
                and _is_under_26_on_date_from_pesel(pesel_raw, data_zawarcia_val)
            ):
                em, rnt, wyp, chor = 0, 0, 0, 0
            else:
                em, rnt, wyp, chor = 1, 1, 1, 0

            out_rows.append(
                {
                    "PESEL": pesel_out,
                    "Номер умовы": nr_umowy_val,
                    "Typ ubezpieczenia": UBEZPIECZENIA_TYP_CONST,
                    "Data powstania obowiazku ubezpieczenia": data_zawarcia_val,
                    "Osoba podlega ubezpieczeniu Emerytalnemu": em,
                    "Osoba podlega ubezpieczeniu Rentowemu": rnt,
                    "Osoba podlega ubezpieczeniu Wypadkowemu": wyp,
                    "Osoba podlega ubezpieczeniu Chorobowemu": chor,
                }
            )

        result_df = pd.DataFrame(out_rows, columns=UBEZPIECZENIA_OUTPUT_COLUMNS)
        return result_df, [], []

    def _fill_format_table(self, df: pd.DataFrame) -> None:
        preview = df.head(200)
        self.format_table.clear()
        self.format_table.setRowCount(len(preview.index))
        self.format_table.setColumnCount(len(preview.columns))
        self.format_table.setHorizontalHeaderLabels([str(c) for c in preview.columns])
        for row_index in range(len(preview.index)):
            for col_index, col_name in enumerate(preview.columns):
                value = str(preview.iloc[row_index][col_name])
                self.format_table.setItem(row_index, col_index, QTableWidgetItem(value))

    def _run_format_save(self) -> None:
        if self._current_format_mode() == FORMAT_MODE_UMOWY_BATCH:
            if not self.format_batch_paths:
                QMessageBox.warning(self, "", self._t("format.error_no_batch_files"))
                return
            name = self.format_output_name_input.text().strip()
            if not name:
                QMessageBox.warning(self, "", self._t("format.error_no_name"))
                return
            safe_name = self._sanitize_output_filename(name)
            if not safe_name.lower().endswith((".xlsx", ".xls")):
                safe_name += ".xlsx"
            IMPORT_FILES_DIR.mkdir(parents=True, exist_ok=True)
            parts: list[pd.DataFrame] = []
            err_lines: list[str] = []
            for src in self.format_batch_paths:
                try:
                    raw, sheet_names = read_excel_umowy_format(src)
                    if len(sheet_names) > 1:
                        self._log(
                            "[format] "
                            f"{src}: scalono arkuszy {sheet_names} ({len(raw)} surow.)"
                        )
                    result_df, _, _ = self._transform_umowy_dataframe(
                        raw, dzielo_format=False, mixed_format=True
                    )
                    parts.append(result_df)
                    self._log(
                        f"[format] batch fragment OK: {src} ({len(result_df)} rows)"
                    )
                except Exception as exc:
                    err_lines.append(f"{src}: {exc}")
                    self._log(f"[format] batch ERR: {src}: {exc}")
            if not parts:
                QMessageBox.critical(
                    self,
                    self._t("format.title"),
                    self._t("format.batch_merge_none"),
                )
                return
            try:
                merged = pd.concat(parts, axis=0, ignore_index=True, sort=False)
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    self._t("format.title"),
                    self._t("format.batch_merge_concat_err", err=str(exc)),
                )
                self._log(f"[format] batch concat: {exc}")
                return
            output_path = IMPORT_FILES_DIR / safe_name
            try:
                merged.to_excel(output_path, index=False)
            except Exception as exc:
                QMessageBox.critical(self, "Ошибка сохранения", str(exc))
                self._log(f"[format] batch save: {exc}")
                return
            ok_files = len(parts)
            self.format_result_df = merged
            self._fill_format_table(merged)
            msg = self._t(
                "format.batch_merged_done",
                path=str(output_path),
                rows=len(merged.index),
                files=ok_files,
                err=len(err_lines),
            )
            if err_lines:
                msg += "\n\n" + "\n".join(err_lines[:25])
                if len(err_lines) > 25:
                    msg += f"\n… (+{len(err_lines) - 25})"
            self._log(msg.replace("\n\n", " | "))
            self.statusBar().showMessage(msg.split("\n")[0], 10000)
            if err_lines:
                QMessageBox.warning(self, self._t("common.done"), msg)
            else:
                QMessageBox.information(self, self._t("common.done"), msg)
            return

        if self.format_source_df is None:
            QMessageBox.warning(self, "", self._t("format.error_no_file"))
            return

        name = self.format_output_name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "", self._t("format.error_no_name"))
            return

        try:
            result_df, _, _ = self._transform_current(self.format_source_df)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка форматирования", str(exc))
            self._log(f"[format] Ошибка трансформации: {exc}")
            return

        safe_name = self._sanitize_output_filename(name)
        if not safe_name.lower().endswith((".xlsx", ".xls")):
            safe_name += ".xlsx"

        try:
            IMPORT_FILES_DIR.mkdir(parents=True, exist_ok=True)
            output_path = IMPORT_FILES_DIR / safe_name
            result_df.to_excel(output_path, index=False)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка сохранения", str(exc))
            self._log(f"[format] Ошибка сохранения: {exc}")
            return

        self.format_result_df = result_df
        self._fill_format_table(result_df)
        self._log(self._t("format.success", path=str(output_path)))
        self.statusBar().showMessage(self._t("format.success", path=str(output_path)), 5000)
        QMessageBox.information(self, self._t("common.done"), self._t("format.success", path=str(output_path)))

    def _sanitize_output_filename(self, name: str) -> str:
        invalid = '<>:"/\\|?*'
        cleaned = "".join(ch for ch in name if ch not in invalid).strip().rstrip(".")
        return cleaned or "formatted"

    def _open_import_files_folder(self) -> None:
        try:
            IMPORT_FILES_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(IMPORT_FILES_DIR))
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось открыть папку", str(exc))

    def _build_db_overview_tab(self) -> QWidget:
        return DbOverviewTab(
            db_config_provider=self._read_db_config_from_form,
            log_callback=self._log,
            translate=self._t,
        )

    def _build_logs_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        logs_title = QLabel(self._t("logs.title"))
        logs_title.setStyleSheet("font-size: 10pt; font-weight: 600; color: #e2e8f0;")
        logs_help = QLabel(self._t("logs.hint"))
        logs_help.setStyleSheet("color: #94a3b8;")
        self.logs_text = QTextEdit()
        self.logs_text.setReadOnly(True)
        self.logs_text.setPlaceholderText(self._t("logs.placeholder"))
        logs_actions = QHBoxLayout()
        open_logs_button = QPushButton(self._t("logs.open_folder"))
        open_logs_button.setObjectName("SecondaryAction")
        open_logs_button.clicked.connect(self._open_logs_folder)
        clear_button = QPushButton(self._t("logs.clear"))
        clear_button.setObjectName("SecondaryAction")
        clear_button.clicked.connect(self.logs_text.clear)
        logs_actions.addWidget(open_logs_button)
        logs_actions.addStretch()
        logs_actions.addWidget(clear_button)
        layout.addWidget(logs_title)
        layout.addWidget(logs_help)
        layout.addWidget(self.logs_text)
        layout.addLayout(logs_actions)
        return tab

    def _build_settings_tab(self) -> QWidget:
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        db_group = QGroupBox(self._t("settings.db"))
        layout = QFormLayout(db_group)
        layout.setContentsMargins(12, 18, 12, 12)
        layout.setSpacing(10)

        self.driver_input = QLineEdit()
        self.server_input = QLineEdit()
        self.database_input = QLineEdit()
        self.username_input = QLineEdit()
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.trusted_checkbox = QCheckBox(self._t("settings.trusted"))
        self.language_box = QComboBox()
        self.language_box.addItem("Русский (RU)", "ru")
        self.language_box.addItem("Polski (PL)", "pl")

        self.start_id_input = QLineEdit()
        self.log_file_input = QLineEdit()
        self.data_od_input = QLineEdit()

        layout.addRow(self._t("settings.driver"), self.driver_input)
        layout.addRow(self._t("settings.server"), self.server_input)
        layout.addRow(self._t("settings.database"), self.database_input)
        layout.addRow(self._t("settings.user"), self.username_input)
        layout.addRow(self._t("settings.password"), self.password_input)
        layout.addRow(self._t("settings.lang"), self.language_box)
        layout.addRow("", self.trusted_checkbox)
        app_group = QGroupBox(self._t("settings.app"))
        app_layout = QFormLayout(app_group)
        app_layout.setContentsMargins(12, 18, 12, 12)
        app_layout.setSpacing(10)
        app_layout.addRow(self._t("settings.start_id"), self.start_id_input)
        app_layout.addRow(self._t("settings.data_od"), self.data_od_input)
        app_layout.addRow(self._t("settings.log_name"), self.log_file_input)

        buttons = QHBoxLayout()
        save_button = QPushButton(self._t("settings.save"))
        save_button.setObjectName("PrimaryAction")
        save_button.clicked.connect(self._save_settings)
        test_button = QPushButton(self._t("settings.test"))
        test_button.setObjectName("SecondaryAction")
        test_button.clicked.connect(self._test_connection)
        buttons.addWidget(save_button)
        buttons.addWidget(test_button)
        root.addWidget(db_group)
        root.addWidget(app_group)
        root.addLayout(buttons)
        root.addStretch()

        return tab

    def _create_kpi_card(self, title: str, value: str) -> dict[str, QWidget]:
        frame = QFrame()
        frame.setObjectName("KpiCard")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(10, 8, 10, 8)
        frame_layout.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("KpiTitle")
        value_label = QLabel(value)
        value_label.setObjectName("KpiValue")
        frame_layout.addWidget(title_label)
        frame_layout.addWidget(value_label)
        return {"frame": frame, "value": value_label}

    def _set_kpi_value(self, card: dict[str, QWidget], value: int | str) -> None:
        value_label = card.get("value")
        if isinstance(value_label, QLabel):
            value_label.setText(str(value))

    def _open_logs_folder(self) -> None:
        try:
            LOGS_DIR.mkdir(exist_ok=True)
            os.startfile(str(LOGS_DIR))
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось открыть папку", str(exc))

    def _choose_excel_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите Excel-файл",
            "",
            "Excel Files (*.xlsx *.xls)",
        )
        if not file_path:
            return

        try:
            self.source_df = read_excel(file_path)
            self.preview_df = preview_dataframe(self.source_df)
            self.current_file = file_path
            self.file_label.setText(self._format_path_for_label(file_path))
            self.file_label.setToolTip(file_path)
            self._populate_mapping_boxes(self.source_df.columns.tolist())
            self._fill_table(self.preview_df)
            self._set_kpi_value(self.kpi_file_rows, len(self.source_df))
            self._set_kpi_value(self.kpi_ready_rows, 0)
            self._set_kpi_value(self.kpi_errors, 0)
            self._log(f"Файл загружен: {file_path} ({len(self.source_df)} строк)")
            self.statusBar().showMessage("Excel-файл успешно загружен.", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка чтения", str(exc))
            self._log(f"Ошибка чтения файла: {exc}")
            self.statusBar().showMessage("Не удалось прочитать Excel-файл.", 5000)

    def _format_path_for_label(self, path: str, max_len: int = 90) -> str:
        normalized = path.replace("\\", "/")
        if len(normalized) <= max_len:
            return normalized
        return "..." + normalized[-(max_len - 3) :]

    def _populate_mapping_boxes(self, columns: list[str]) -> None:
        for field in self._effective_required_fields():
            box = self.mapping_boxes[field]
            box.blockSignals(True)
            box.clear()
            box.addItems(columns)
            match = self._guess_column_for_field(field, columns)
            if match:
                box.setCurrentText(match)
            box.blockSignals(False)

    def _guess_column_for_field(self, field: str, columns: list[str]) -> str:
        normalized = [col.lower().strip() for col in columns]
        guesses = {
            "Kod US": ("kod", "urzad", "us"),
            "Nazwa": ("nazwa", "name"),
            "Nr Ewidencyjny": ("nr", "ewid", "prac"),
            "NR Ewidencyjny": ("nr", "ewid", "prac"),
            "Nazwisko": ("nazw", "surname", "last"),
            "Imie": ("imie", "name", "first"),
            "Data urodzenia": ("urod", "data"),
            "PESEL": ("pesel",),
            "Nr dowodu": ("dowod",),
            "Nr paszportu": ("paszport",),
            "telefon": ("telefon", "phone"),
            "Kraj": ("kraj", "country"),
            "Wojewodztwo": ("woj",),
            "Powiat": ("powiat",),
            "Gmina": ("gmina",),
            "Ulica": ("ulica", "street"),
            "Numer Domu": ("domu", "dom"),
            "Numer lokalu": ("lok",),
            "Miejscowosc": ("miejsc", "city"),
            "Kod pocztowy": ("kod", "poczt"),
            "Poczta": ("poczta",),
            "nazwa Urząd Skarbowy": ("urz", "skarb"),
            "номер умовы": ("numer umowy", "nr umowy", "umowa"),
            "номер рахунка": ("numer rachunku", "rachunek", "konto"),
            "Тип умовы": ("typ umowy", "rodzaj umowy"),
            "Дата выплаты": ("data wyplaty", "wyplata"),
            "Дата умовы": ("data umowy", "umowy"),
            "Форма податка": ("forma podatka", "opodatkowania", "forma op"),
            "Kwota brutto": ("kwota brutto", "brutto", "wynagrodzenie brutto"),
            "KOSZTY UZYSKANIA PRZYCHODU %": ("koszty uzyskania", "kup", "%"),
            "Skł.na ub.emerytal.[%]": ("emerytal",),
            "Składka ub.rent. U [%]": ("rent u", "rentoweu"),
            "Składka ub.rent. P [%]": ("rent p", "rentowep"),
            "Składka ub.chorob.[%]": ("chorob",),
            "Składka ub.wypadk.[%]": ("wypadk",),
            "Składka ub.zdrowotne[%]": ("zdrowot",),
            "FP [%]": ("fp",),
            "FGŚP [%]": ("fgsp", "fgsp"),
            "Номер умовы": ("numer umowy", "nr umowy", "umowa"),
            "Typ ubezpieczenia": ("typ ubezpieczenia", "kod tytulu", "tytul"),
            "Data powstania obowiazku ubezpieczenia": (
                "data powstania obowiazku",
                "data obowiazku",
                "ubezpieczenia",
            ),
            "Osoba podlega ubezpieczeniu Emerytalnemu": (
                "emerytalnemu",
                "emerytalne",
            ),
            "Osoba podlega ubezpieczeniu Rentowemu": ("rentowemu", "rentowe"),
            "Osoba podlega ubezpieczeniu Wypadkowemu": ("wypadkowemu", "wypadkowe"),
            "Osoba podlega ubezpieczeniu Chorobowemu": ("chorobowemu", "chorobowe"),
            "Od dnia": ("od dnia", "data od", "od_dnia"),
        }
        tokens = guesses.get(field, (field.lower(),))
        for i, col in enumerate(normalized):
            if any(token in col for token in tokens):
                return columns[i]
        return columns[0] if columns else ""

    def _fill_table(self, df: pd.DataFrame) -> None:
        self.table.clear()
        self.table.setRowCount(len(df.index))
        self.table.setColumnCount(len(df.columns))
        self.table.setHorizontalHeaderLabels([str(c) for c in df.columns])

        for row_index in range(len(df.index)):
            for col_index, col_name in enumerate(df.columns):
                value = str(df.iloc[row_index][col_name])
                self.table.setItem(row_index, col_index, QTableWidgetItem(value))

    def _run_checkin(self) -> None:
        if not self._begin_operation("checkin"):
            return
        if self.source_df is None:
            QMessageBox.warning(self, "Нет данных", "Сначала выберите Excel-файл.")
            self._end_operation()
            return

        try:
            self._start_operation_log("checkin")
            mapping = {
                field: self.mapping_boxes[field].currentText()
                for field in self._effective_required_fields()
            }
            self.current_mapping = mapping.copy()
            mapped_df = map_columns(
                self.source_df,
                mapping,
                self.active_profile,
                employee_lookup_mode=self._active_employee_lookup_mode(),
            )

            db_service = DatabaseService(self._read_db_config_from_form())
            if not self._run_db_preflight(db_service, "проверка"):
                return
            data_od = self._read_data_od()

            result = check_in(
                mapped_df=mapped_df,
                db_service=db_service,
                dry_run=self.dry_run_checkbox.isChecked(),
                data_od=data_od,
                profile=self.active_profile,
                strict_od_dnia=self.strict_od_dnia_checkbox.isChecked(),
                employee_lookup_mode=self._active_employee_lookup_mode(),
            )
            self.last_checkin_result = result
            self._paint_rows(result.rows)

            stats = summarize_result(result)
            self._set_kpi_value(self.kpi_ready_rows, len(result.importable_rows))
            self._set_kpi_value(self.kpi_errors, stats["errors"])
            self._log(
                "Проверка завершена: "
                f"новых urzedow={stats['to_create_urzedy']}, "
                f"новых связей={stats['to_create_links']}, "
                f"пропущено связей={stats['skipped_links']}, "
                f"ошибок={stats['errors']}, "
                f"строк={stats['total_rows']}"
            )

            ok_count = sum(1 for row in result.rows if row.status == RowStatus.OK)
            warning_rows = [row for row in result.rows if row.status == RowStatus.WARNING]
            error_rows = [row for row in result.rows if row.status == RowStatus.ERROR]
            if ok_count:
                self._log(f"[OK] строк успешно: {ok_count}")
            for row in warning_rows[:50]:
                self._log(f"[WARNING] wiersz {row.index + 1}: {row.message}")
            if len(warning_rows) > 50:
                self._log(f"... и ещё {len(warning_rows) - 50} предупреждений")
            for row in error_rows[:50]:
                self._log(f"[ERROR] wiersz {row.index + 1}: {row.message}")
            if len(error_rows) > 50:
                self._log(f"... и ещё {len(error_rows) - 50} ошибок")
            if stats["errors"] > 0:
                self._show_error_details(result)
            self._show_missing_urzedy(result)
            self.statusBar().showMessage("Проверка завершена.", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка проверки", str(exc))
            self._log(f"Ошибка проверки: {exc}")
            self.statusBar().showMessage("Проверка завершилась с ошибкой.", 5000)
        finally:
            self._end_operation()

    def _paint_rows(self, rows) -> None:
        color_by_status = {
            RowStatus.OK: QColor(196, 255, 196),  # green
            RowStatus.WARNING: QColor(255, 245, 170),  # yellow
            RowStatus.ERROR: QColor(255, 196, 196),  # red
        }
        for row_result in rows:
            row_index = row_result.index
            if row_index >= self.table.rowCount():
                continue
            color = color_by_status[row_result.status]
            target_columns = self._resolve_target_columns(getattr(row_result, "field_name", None))
            if not target_columns:
                target_columns = list(range(self.table.columnCount()))
            for col_index in target_columns:
                item = self.table.item(row_index, col_index)
                if item is None:
                    item = QTableWidgetItem("")
                    self.table.setItem(row_index, col_index, item)
                item.setBackground(color)

    def _resolve_target_columns(self, field_name: str | None) -> list[int]:
        if not field_name:
            return []
        source_column_name = self.current_mapping.get(field_name)
        if not source_column_name:
            return []
        for col_index in range(self.table.columnCount()):
            header_item = self.table.horizontalHeaderItem(col_index)
            if header_item and header_item.text() == source_column_name:
                return [col_index]
        return []

    def _show_error_details(self, result) -> None:
        errors = [row for row in result.rows if row.status == RowStatus.ERROR]
        if not errors:
            return
        lines = []
        for row in errors[:10]:
            field_text = f" (поле: {row.field_name})" if getattr(row, "field_name", None) else ""
            lines.append(f"{row.index + 1}: {row.message}{field_text}")
        suffix = "\n..." if len(errors) > 10 else ""
        QMessageBox.warning(
            self,
            "Где ошибки",
            "Ошибки найдены в строках:\n\n" + "\n".join(lines) + suffix,
        )

    def _show_missing_urzedy(self, result) -> None:
        """Show a single consolidated hint about urzędy that are missing both
        from the database and from urzedy_reference.json.

        Saves the user from scrolling per-row errors when the same few
        urzedy are missing across many rows.
        """
        missing = getattr(result, "missing_urzedy", None) or []
        if not missing:
            return
        preview = missing[:20]
        body_lines = [f"  - {name}" for name in preview]
        if len(missing) > 20:
            body_lines.append(f"  ... и ещё {len(missing) - 20}")
        message = (
            f"Найдены {len(missing)} урзадов, которых нет ни в базе, ни в словаре "
            "(urzedy_reference.json).\nДобавьте их в словарь и повторите проверку:\n\n"
            + "\n".join(body_lines)
        )
        self._log(f"Пропущенные urzędy: {', '.join(missing)}")
        QMessageBox.warning(self, "Неизвестные urzędy", message)

    def _run_execute_import_stub(self) -> None:
        if not self._begin_operation("import"):
            return
        if self.source_df is None:
            QMessageBox.warning(self, "Нет данных", "Сначала выберите Excel-файл.")
            self._end_operation()
            return

        try:
            self._start_operation_log("import")
            mapping = {
                field: self.mapping_boxes[field].currentText()
                for field in self._effective_required_fields()
            }
            self.current_mapping = mapping.copy()
            mapped_df = map_columns(
                self.source_df,
                mapping,
                self.active_profile,
                employee_lookup_mode=self._active_employee_lookup_mode(),
            )
            db_service = DatabaseService(self._read_db_config_from_form())
            if not self._run_db_preflight(db_service, "импорт"):
                return
            data_od = self._read_data_od()

            result = check_in(
                mapped_df=mapped_df,
                db_service=db_service,
                dry_run=False,
                data_od=data_od,
                profile=self.active_profile,
                strict_od_dnia=self.strict_od_dnia_checkbox.isChecked(),
                employee_lookup_mode=self._active_employee_lookup_mode(),
            )
            self.last_checkin_result = result
            self._paint_rows(result.rows)

            if result.errors > 0:
                QMessageBox.warning(
                    self,
                    "Импорт остановлен",
                    f"Проверка нашла {result.errors} ошибок. Исправьте данные и попробуйте снова.",
                )
                self._show_error_details(result)
                self._log("Импорт остановлен: есть ошибки валидации.")
                self.statusBar().showMessage("Импорт остановлен: обнаружены ошибки.", 5000)
                return

            if not result.importable_rows:
                QMessageBox.information(self, "Нет изменений", "Нет новых записей для сохранения.")
                self._log("Импорт завершен без изменений.")
                self.statusBar().showMessage("Нет новых данных для записи.", 4000)
                return

            if len(result.importable_rows) >= 200 and not self._confirm_mass_operation(
                title="Подтверждение массового импорта",
                message=(
                    f"Будет обработано {len(result.importable_rows)} строк.\n"
                    "Продолжить запись в базу?"
                ),
            ):
                self._log("Импорт отменен пользователем: не подтвержден массовый режим.")
                return

            if self.dry_run_checkbox.isChecked():
                stats = summarize_result(result)
                QMessageBox.information(
                    self,
                    "Пробный запуск",
                    (
                        "Симуляция импорта завершена.\n"
                        f"Новых urzedow: {stats['to_create_urzedy']}\n"
                        f"Новых связей: {stats['to_create_links']}\n"
                        f"Пропущено связей: {stats['skipped_links']}\n"
                        f"Ошибок: {stats['errors']}"
                    ),
                )
                self._log("Пробный запуск: без записи в базу.")
                self.statusBar().showMessage("Пробный запуск выполнен.", 4000)
                return

            if self.active_profile.key == EMPLOYEE_IMPORT_PROFILE.key:
                start_id = int(self.start_id_input.text().strip() or "12")
                _id_firmy = self._selected_id_firmy()

                def _run(progress_cb, cancel_token, _rows=result.importable_rows, _start=start_id, _firm=_id_firmy):
                    return db_service.execute_employee_import(
                        rows=_rows,
                        start_urzad_id=_start,
                        id_firmy=_firm,
                        data_od=data_od,
                        progress_callback=progress_cb,
                        cancel_token=cancel_token,
                    )

                employee_stats = self._run_job_with_progress(
                    "Импорт сотрудников", len(result.importable_rows), _run
                )
                if employee_stats is None:
                    self._log("Импорт сотрудников отменён пользователем.")
                    self.statusBar().showMessage("Импорт отменён.", 4000)
                    return
                self._save_last_import_record(employee_stats)
                self._log(
                    "Импорт сотрудников завершен: "
                    f"создано сотрудников={employee_stats.created_employees}, "
                    f"создано адресов={employee_stats.created_addresses}, "
                    f"создано urzedow={employee_stats.created_urzedy}, "
                    f"создано связей={employee_stats.created_links}, "
                    f"пропущено дублей PESEL={getattr(employee_stats, 'skipped_duplicates', 0)}"
                )
                QMessageBox.information(
                    self,
                    "Импорт сотрудников завершен",
                    (
                        f"Создано сотрудников: {employee_stats.created_employees}\n"
                        f"Создано адресов: {employee_stats.created_addresses}\n"
                        f"Создано urzedow: {employee_stats.created_urzedy}\n"
                        f"Создано связей: {employee_stats.created_links}\n"
                        f"Пропущено дублей PESEL: {getattr(employee_stats, 'skipped_duplicates', 0)}"
                    ),
                )
                self.statusBar().showMessage("Импорт сотрудников выполнен.", 5000)
                return

            if self.active_profile.key == EMPLOYEE_ADDRESS_IMPORT_PROFILE.key:
                _id_firmy = self._selected_id_firmy()

                def _run(progress_cb, cancel_token, _rows=result.importable_rows, _firm=_id_firmy):
                    return db_service.execute_employee_address_import(
                        rows=_rows,
                        data_od=data_od,
                        id_firmy=_firm,
                        progress_callback=progress_cb,
                        cancel_token=cancel_token,
                    )

                address_stats = self._run_job_with_progress(
                    "Импорт адресов", len(result.importable_rows), _run
                )
                if address_stats is None:
                    self._log("Импорт адресов отменён пользователем.")
                    self.statusBar().showMessage("Импорт отменён.", 4000)
                    return
                self._save_last_import_record(address_stats)
                self._log(
                    "Импорт адресов завершен: "
                    f"создано адресов={address_stats.created_addresses}, "
                    f"обновлено адресов={address_stats.updated_addresses}, "
                    f"смещено дат={address_stats.shifted_address_dates}, "
                    f"не найдено сотрудников={address_stats.missing_employees}"
                )
                QMessageBox.information(
                    self,
                    "Импорт адресов завершен",
                    (
                        f"Создано адресов: {address_stats.created_addresses}\n"
                        f"Обновлено адресов: {address_stats.updated_addresses}\n"
                        f"Смещено дат: {address_stats.shifted_address_dates}\n"
                        f"Не найдено сотрудников: {address_stats.missing_employees}"
                    ),
                )
                self.statusBar().showMessage("Импорт адресов выполнен.", 5000)
                return

            if self.active_profile.key == UMOWY_IMPORT_PROFILE.key:
                _id_firmy = self._selected_id_firmy()

                def _run(progress_cb, cancel_token, _rows=result.importable_rows, _firm=_id_firmy):
                    return db_service.execute_umowy_import(
                        _rows,
                        id_firmy=_firm,
                        progress_callback=progress_cb,
                        cancel_token=cancel_token,
                    )

                umowy_stats = self._run_job_with_progress(
                    "Импорт UMOWY", len(result.importable_rows), _run
                )
                if umowy_stats is None:
                    self._log("Импорт UMOWY отменён пользователем.")
                    self.statusBar().showMessage("Импорт отменён.", 4000)
                    return
                self._save_last_import_record(umowy_stats)
                self._log(
                    "Импорт UMOWY завершен: "
                    f"создано договоров={umowy_stats.created_contracts}, "
                    f"пропущено дублей={umowy_stats.skipped_duplicates}, "
                    f"не найдено сотрудников={umowy_stats.missing_employees}"
                )
                QMessageBox.information(
                    self,
                    "Импорт UMOWY завершен",
                    (
                        f"Создано договоров: {umowy_stats.created_contracts}\n"
                        f"Пропущено дублей: {umowy_stats.skipped_duplicates}\n"
                        f"Не найдено сотрудников: {umowy_stats.missing_employees}"
                    ),
                )
                self.statusBar().showMessage("Импорт UMOWY выполнен.", 5000)
                return

            if self.active_profile.key == UMOWY_DZIELO_IMPORT_PROFILE.key:
                _id_firmy = self._selected_id_firmy()

                def _run_d(progress_cb, cancel_token, _rows=result.importable_rows, _firm=_id_firmy):
                    return db_service.execute_umowy_dzielo_import(
                        _rows,
                        id_firmy=_firm,
                        progress_callback=progress_cb,
                        cancel_token=cancel_token,
                    )

                dz_stats = self._run_job_with_progress(
                    "Импорт UMOWY (o dzieło)",
                    len(result.importable_rows),
                    _run_d,
                )
                if dz_stats is None:
                    self._log("Импорт UMOWY o dzieło отменён пользователем.")
                    self.statusBar().showMessage("Импорт отменён.", 4000)
                    return
                self._save_last_import_record(dz_stats)
                self._log(
                    "Импорт UMOWY (o dzieło) завершен: "
                    f"создано договоров={dz_stats.created_contracts}, "
                    f"пропущено дублей={dz_stats.skipped_duplicates}, "
                    f"не найдено сотрудников={dz_stats.missing_employees}"
                )
                QMessageBox.information(
                    self,
                    "Импорт UMOWY (o dzieło) завершен",
                    (
                        f"Создано договоров: {dz_stats.created_contracts}\n"
                        f"Пропущено дублей: {dz_stats.skipped_duplicates}\n"
                        f"Не найдено сотрудников: {dz_stats.missing_employees}"
                    ),
                )
                self.statusBar().showMessage("Импорт UMOWY (o dzieło) выполнен.", 5000)
                return

            if self.active_profile.key == UMOWY_MIXED_IMPORT_PROFILE.key:
                rows_z = [r for r in result.importable_rows if int(r.get("typ_umowy_no") or 0) == 1]
                rows_d = [r for r in result.importable_rows if int(r.get("typ_umowy_no") or 0) == 2]
                _id_firmy = self._selected_id_firmy()

                def _run_mixed(progress_cb, cancel_token, rz=rows_z, rd=rows_d, _firm=_id_firmy):
                    combined = UmowyImportStats()
                    total = len(rz) + len(rd)
                    if total == 0:
                        return combined
                    offset = 0

                    if rz:

                        def wrap_z(done: int, _tot: int) -> None:
                            if progress_cb:
                                progress_cb(offset + done, total)

                        s1 = db_service.execute_umowy_import(
                            rz,
                            id_firmy=_firm,
                            progress_callback=wrap_z,
                            cancel_token=cancel_token,
                        )
                        combined.created_contracts += s1.created_contracts
                        combined.skipped_duplicates += s1.skipped_duplicates
                        combined.missing_employees += s1.missing_employees
                        combined.created_contract_ids.extend(s1.created_contract_ids or [])
                        offset += len(rz)

                    if rd:

                        def wrap_d(done: int, _tot: int) -> None:
                            if progress_cb:
                                progress_cb(offset + done, total)

                        s2 = db_service.execute_umowy_dzielo_import(
                            rd,
                            id_firmy=_firm,
                            progress_callback=wrap_d,
                            cancel_token=cancel_token,
                        )
                        combined.created_contracts += s2.created_contracts
                        combined.skipped_duplicates += s2.skipped_duplicates
                        combined.missing_employees += s2.missing_employees
                        combined.created_contract_ids.extend(s2.created_contract_ids or [])
                    return combined

                mixed_stats = self._run_job_with_progress(
                    "Импорт UMOWY (zlecenie + o dzieło)",
                    max(1, len(result.importable_rows)),
                    _run_mixed,
                )
                if mixed_stats is None:
                    self._log("Импорт UMOWY (zlecenie + o dzieło) отменён пользователем.")
                    self.statusBar().showMessage("Импорт отменён.", 4000)
                    return
                self._save_last_import_record(mixed_stats)
                self._log(
                    "Импорт UMOWY (zlecenie + o dzieło) завершен: "
                    f"создано договоров={mixed_stats.created_contracts}, "
                    f"пропущено дублей={mixed_stats.skipped_duplicates}, "
                    f"не найдено сотрудников={mixed_stats.missing_employees}"
                )
                QMessageBox.information(
                    self,
                    "Импорт UMOWY (zlecenie + o dzieło) завершен",
                    (
                        f"Создано договоров: {mixed_stats.created_contracts}\n"
                        f"Пропущено дублей: {mixed_stats.skipped_duplicates}\n"
                        f"Не найдено сотрудников: {mixed_stats.missing_employees}"
                    ),
                )
                self.statusBar().showMessage("Импорт UMOWY (zlecenie + o dzieło) выполнен.", 5000)
                return

            if self.active_profile.key == UBEZPIECZENIA_OBOWIAZKOWE_IMPORT_PROFILE.key:
                _id_firmy = self._selected_id_firmy()

                def _run(progress_cb, cancel_token, _rows=result.importable_rows, _firm=_id_firmy):
                    return db_service.execute_ubezpieczenia_obowiazkowe_import(
                        _rows,
                        id_firmy=_firm,
                        progress_callback=progress_cb,
                        cancel_token=cancel_token,
                    )

                ins_stats = self._run_job_with_progress(
                    "Импорт Ubezpieczenie obowiązkowe",
                    len(result.importable_rows),
                    _run,
                )
                if ins_stats is None:
                    self._log("Импорт Ubezpieczenie obowiązkowe отменён пользователем.")
                    self.statusBar().showMessage("Импорт отменён.", 4000)
                    return
                self._save_last_import_record(ins_stats)
                self._log(
                    "Импорт Ubezpieczenie obowiązkowe завершен: "
                    f"создано={ins_stats.created_insurance_rows}, "
                    f"пропущено дублей={ins_stats.skipped_duplicates}, "
                    f"уже есть за этот год={ins_stats.skipped_existing_year}, "
                    f"не найдено сотрудников={ins_stats.missing_employees}, "
                    f"без umowy тип 1={ins_stats.missing_type1_contract}"
                )
                QMessageBox.information(
                    self,
                    "Импорт Ubezpieczenie obowiązkowe завершен",
                    (
                        f"Создано записей: {ins_stats.created_insurance_rows}\n"
                        f"Пропущено дублей: {ins_stats.skipped_duplicates}\n"
                        f"Уже есть за этот год: {ins_stats.skipped_existing_year}\n"
                        f"Не найдено сотрудников: {ins_stats.missing_employees}\n"
                        f"Нет umowy тип 1: {ins_stats.missing_type1_contract}"
                    ),
                )
                self.statusBar().showMessage(
                    "Импорт Ubezpieczenie obowiązkowe выполнен.", 5000
                )
                return

            if self.active_profile.key == PRZEPROWADZKI_IMPORT_PROFILE.key:
                start_id = int(self.start_id_input.text().strip() or "12")
                _id_firmy = self._selected_id_firmy()

                def _run(progress_cb, cancel_token, _rows=result.importable_rows, _start=start_id, _firm=_id_firmy):
                    return db_service.execute_przeprowadzki_import(
                        rows=_rows,
                        start_urzad_id=_start,
                        data_od=data_od,
                        id_firmy=_firm,
                        progress_callback=progress_cb,
                        cancel_token=cancel_token,
                    )

                move_stats = self._run_job_with_progress(
                    "Импорт для переехавших", len(result.importable_rows), _run
                )
                if move_stats is None:
                    self._log("Импорт для переехавших отменён пользователем.")
                    self.statusBar().showMessage("Импорт отменён.", 4000)
                    return
                self._save_last_import_record(move_stats)
                self._log(
                    "Импорт для переехавших завершен: "
                    f"создано адресов={move_stats.created_addresses}, "
                    f"смещено дат адресов={move_stats.shifted_address_dates}, "
                    f"создано urzedow={move_stats.created_urzedy}, "
                    f"создано связей={move_stats.created_links}, "
                    f"смещено дат связей={move_stats.shifted_link_dates}, "
                    f"не найдено сотрудников={move_stats.missing_employees}"
                )
                QMessageBox.information(
                    self,
                    "Импорт для переехавших завершен",
                    (
                        f"Создано адресов: {move_stats.created_addresses}\n"
                        f"Смещено дат адресов: {move_stats.shifted_address_dates}\n"
                        f"Создано urzedow: {move_stats.created_urzedy}\n"
                        f"Создано связей: {move_stats.created_links}\n"
                        f"Смещено дат связей: {move_stats.shifted_link_dates}\n"
                        f"Не найдено сотрудников: {move_stats.missing_employees}"
                    ),
                )
                self.statusBar().showMessage("Импорт для переехавших выполнен.", 5000)
                return

            start_id = int(self.start_id_input.text().strip() or "12")

            def _run(progress_cb, cancel_token, _rows=result.importable_rows, _start=start_id):
                return db_service.execute_import(
                    rows=_rows,
                    start_urzad_id=_start,
                    data_od=data_od,
                    progress_callback=progress_cb,
                    cancel_token=cancel_token,
                )

            import_stats = self._run_job_with_progress(
                "Импорт", len(result.importable_rows), _run
            )
            if import_stats is None:
                self._log("Импорт отменён пользователем.")
                self.statusBar().showMessage("Импорт отменён.", 4000)
                return
            self._save_last_import_record(import_stats)
            self._log(
                "Импорт завершен: "
                f"создано urzedow={import_stats.created_urzedy}, "
                f"создано связей={import_stats.created_links}, "
                f"смещено дат связей={import_stats.shifted_link_dates}, "
                f"пропущено связей={import_stats.skipped_links}"
            )
            QMessageBox.information(
                self,
                "Импорт завершен",
                (
                    "Данные сохранены в транзакции.\n"
                    f"Новых urzedow: {import_stats.created_urzedy}\n"
                    f"Новых связей: {import_stats.created_links}\n"
                    f"Смещено дат связей: {import_stats.shifted_link_dates}\n"
                    f"Пропущено связей: {import_stats.skipped_links}"
                ),
            )
            self.statusBar().showMessage("Импорт успешно завершен.", 5000)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка импорта", str(exc))
            self._log(f"Ошибка импорта: {exc}")
            self._log(traceback.format_exc())
            self.statusBar().showMessage("Импорт завершился с ошибкой.", 5000)
        finally:
            self._end_operation()

    def _run_verify_umowy_all(self) -> None:
        if not self._begin_operation("verify_umowy"):
            return
        try:
            self._start_operation_log("verify_umowy")
            db_service = DatabaseService(self._read_db_config_from_form())
            if not self._run_db_preflight(db_service, "проверка umowy в БД"):
                return

            def _run(progress_cb, cancel_token):
                return db_service.verify_umowy_financials(
                    tolerance=0.005,
                    progress_callback=progress_cb,
                    cancel_token=cancel_token,
                )

            report = self._run_job_with_progress("Проверка UMOWY w bazie", 1, _run)
            if report is None:
                self._log("Проверка UMOWY отменена пользователем.")
                self.statusBar().showMessage("Проверка отменена.", 4000)
                return

            self._log(
                "Проверка UMOWY завершена: "
                f"проверено={report.checked}, "
                f"ok={report.ok}, "
                f"с проблемами={report.with_issues}, "
                f"pass_rate={report.pass_rate_pct}%"
            )

            if report.with_issues:
                self._log("Первые 20 проблемных umowy:")
                for issue in report.issues[:20]:
                    self._log(
                        f"  ID={issue.identyfikator}, employee={issue.employee_id}, "
                        f"umowa={issue.numer_umowy}, deltas={len(issue.deltas)}, "
                        f"rate_warnings={len(issue.rate_warnings)}"
                    )
                    for delta in issue.deltas[:6]:
                        self._log(
                            f"    {delta.field}: bd={delta.stored:.2f}, "
                            f"expected={delta.expected:.2f}, delta={delta.delta:+.2f}"
                        )
                    if len(issue.deltas) > 6:
                        self._log(f"    ... i jeszcze {len(issue.deltas) - 6} rozbieżności")
                    for warning in issue.rate_warnings[:3]:
                        self._log(f"    [RATE] {warning}")
                    if len(issue.rate_warnings) > 3:
                        self._log(
                            f"    ... i jeszcze {len(issue.rate_warnings) - 3} ostrzeżeń stawek"
                        )

            QMessageBox.information(
                self,
                "Проверка UMOWY завершена",
                (
                    f"Проверено umowy: {report.checked}\n"
                    f"OK: {report.ok}\n"
                    f"С проблемами: {report.with_issues}\n"
                    f"Pass rate: {report.pass_rate_pct}%\n\n"
                    "Подробности (включая топ проблемных договоров) записаны в лог."
                ),
            )
            self.statusBar().showMessage(
                f"Проверка UMOWY завершена: {report.ok}/{report.checked} OK",
                6000,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка проверки UMOWY", str(exc))
            self._log(f"Ошибка проверки UMOWY: {exc}")
            self._log(traceback.format_exc())
            self.statusBar().showMessage("Проверка UMOWY завершилась с ошибкой.", 5000)
        finally:
            self._end_operation()

    def _toggle_status_numbers_input(self) -> None:
        selected_mode = self.status_mode_box.currentData()
        enabled = selected_mode == "selected"
        self.status_numbers_input.setEnabled(enabled)

    def _on_strict_od_dnia_toggled(self, enabled: bool) -> None:
        self.strict_od_hint.setVisible(enabled)

    def _run_change_employee_status(self) -> None:
        if not self._begin_operation("change-status"):
            return
        try:
            self._start_operation_log("change-status")
            from_status_text = self.status_from_input.text().strip()
            to_status_text = self.status_to_input.text().strip()
            if not from_status_text.isdigit() or not to_status_text.isdigit():
                raise ValueError("Поля статусов должны быть целыми числами.")

            from_status = int(from_status_text)
            to_status = int(to_status_text)
            if from_status == to_status:
                raise ValueError("Статусы 'из' и 'в' не должны совпадать.")

            db_service = DatabaseService(self._read_db_config_from_form())
            if not self._run_db_preflight(db_service, "смена статуса"):
                return
            mode = self.status_mode_box.currentData()
            if mode == "all":
                candidates = db_service.count_employee_status_all(from_status)
                if candidates >= 100 and not self._confirm_mass_operation(
                    title="Подтверждение массовой смены статуса",
                    message=(
                        f"Будет изменен статус у {candidates} сотрудников.\n"
                        "Продолжить?"
                    ),
                ):
                    self._log("Смена статуса отменена пользователем: массовая операция.")
                    return
                stats = db_service.update_employee_status_all(
                    from_status=from_status,
                    to_status=to_status,
                )
                self._log(
                    f"Смена статуса для всех: изменено={stats.updated}, "
                    f"из={from_status}, в={to_status}"
                )
                QMessageBox.information(
                    self,
                    "Готово",
                    f"Статус изменен у {stats.updated} сотрудников.",
                )
            else:
                numbers = self._parse_nr_ewidencyjne(self.status_numbers_input.toPlainText())
                if not numbers:
                    raise ValueError("Укажите хотя бы один NR_EWIDENCYJNY.")
                preview = db_service.preview_employee_status_by_numbers(numbers, from_status)
                if preview.updated >= 30 and not self._confirm_mass_operation(
                    title="Подтверждение смены статуса по списку",
                    message=(
                        f"К изменению подготовлено {preview.updated} сотрудников.\n"
                        f"Не найдено: {preview.not_found}, другой статус: {preview.unchanged}.\n"
                        "Продолжить?"
                    ),
                ):
                    self._log("Смена статуса по списку отменена пользователем.")
                    return
                stats = db_service.update_employee_status_by_numbers(
                    nr_ewidencyjne=numbers,
                    from_status=from_status,
                    to_status=to_status,
                )
                self._log(
                    "Смена статуса по списку NR_EWIDENCYJNY: "
                    f"изменено={stats.updated}, "
                    f"не найдено={stats.not_found}, "
                    f"пропущено (другой статус)={stats.unchanged}, "
                    f"из={from_status}, в={to_status}"
                )
                QMessageBox.information(
                    self,
                    "Готово",
                    (
                        f"Изменено: {stats.updated}\n"
                        f"Не найдено: {stats.not_found}\n"
                        f"Пропущено (другой статус): {stats.unchanged}"
                    ),
                )

            self.statusBar().showMessage("Смена статуса выполнена.", 5000)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка смены статуса", str(exc))
            self._log(f"Ошибка смены статуса: {exc}")
            self.statusBar().showMessage("Смена статуса завершилась с ошибкой.", 5000)
        finally:
            self._end_operation()

    def _parse_nr_ewidencyjne(self, raw: str) -> list[str]:
        cleaned = raw.replace("\n", ",").replace(";", ",").replace(" ", ",")
        values = [part.strip() for part in cleaned.split(",") if part.strip()]
        unique: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value not in seen:
                seen.add(value)
                unique.append(value)
        return unique

    def _rollback_last_import(self) -> None:
        if not self._begin_operation("rollback"):
            return
        try:
            self._start_operation_log("rollback")
            history = self._load_import_history()
            if not history:
                QMessageBox.information(self, "Нет истории", "Не найден импорт для отмены.")
                return

            record = self._pick_history_record_to_rollback(history)
            if record is None:
                return

            summary = self._describe_history_record(record)
            confirm = QMessageBox.question(
                self,
                "Подтверждение отмены",
                f"Отменить импорт?\n\n{summary}\n\nБудут удалены добавленные записи.",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

            db_service = DatabaseService(self._read_db_config_from_form())
            if not self._run_db_preflight(db_service, "откат"):
                return
            undo_stats = db_service.undo_import_record(record)
            self._remove_history_record(record)
            self._log(
                "Откат завершен: "
                f"удалено страховых={undo_stats.deleted_insurance_rows}, "
                f"удалено договоров={undo_stats.deleted_contracts}, "
                f"удалено связей={undo_stats.deleted_links}, "
                f"удалено адресов={undo_stats.deleted_addresses}, "
                f"удалено сотрудников={undo_stats.deleted_employees}, "
                f"удалено urzedow={undo_stats.deleted_urzedy}, "
                f"пропущено urzedow={undo_stats.skipped_urzedy}"
            )
            QMessageBox.information(
                self,
                "Откат завершен",
                (
                    f"Удалено страховых записей: {undo_stats.deleted_insurance_rows}\n"
                    f"Удалено договоров: {undo_stats.deleted_contracts}\n"
                    f"Удалено связей: {undo_stats.deleted_links}\n"
                    f"Удалено адресов: {undo_stats.deleted_addresses}\n"
                    f"Удалено сотрудников: {undo_stats.deleted_employees}\n"
                    f"Удалено urzedow: {undo_stats.deleted_urzedy}\n"
                    f"Пропущено urzedow (все еще связаны): {undo_stats.skipped_urzedy}"
                ),
            )
            self.statusBar().showMessage("Последний импорт отменен.", 5000)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка отката", str(exc))
            self._log(f"Ошибка отката: {exc}")
            self.statusBar().showMessage("Откат завершился с ошибкой.", 5000)
        finally:
            self._end_operation()

    def _begin_operation(self, operation_key: str) -> bool:
        if self._operation_in_progress:
            QMessageBox.warning(
                self,
                "Операция уже выполняется",
                "Дождитесь завершения текущей операции.",
            )
            return False
        self._operation_in_progress = True
        self._set_action_buttons_enabled(False)
        self.statusBar().showMessage(f"Выполняется операция: {operation_key}...", 2000)
        return True

    def _end_operation(self) -> None:
        self._operation_in_progress = False
        self._set_action_buttons_enabled(True)

    def _run_job_with_progress(self, title: str, total: int, job) -> object:
        """Run `job(progress_cb, cancel_token)` on a background QThread with
        a modal progress dialog that has a working Cancel button.

        Returns the stats produced by the job, or `None` if the user cancelled.
        Raises the original exception if the job failed.

        Semantics:
        - During the run the UI stays responsive (event loop is live).
        - Only Cancel button is exposed; closing the dialog via X is blocked
          to avoid leaving the worker orphaned.
        - Cancel flips the CancelToken; the worker raises ImportCancelled on
          the next row and the SQL transaction rolls back.
        """
        dialog = QProgressDialog(title, "Отменить", 0, max(1, int(total)), self)
        dialog.setWindowTitle(self._t("app.title"))
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)

        result: dict[str, object] = {"stats": None, "error": None, "cancelled": False}
        loop = QEventLoop(self)

        thread, worker = run_in_thread(self, job)

        def on_progress(done: int, full: int) -> None:
            if full != dialog.maximum():
                dialog.setMaximum(max(1, int(full)))
            dialog.setValue(int(done))
            dialog.setLabelText(f"{title}\n{done} / {full}")
            self.statusBar().showMessage(f"{title}: {done}/{full}", 2000)

        def on_finished(stats: object) -> None:
            result["stats"] = stats
            loop.quit()

        def on_failed(message: str) -> None:
            result["error"] = message
            loop.quit()

        def on_cancelled() -> None:
            result["cancelled"] = True
            loop.quit()

        def on_cancel_clicked() -> None:
            worker.request_cancel()
            dialog.setLabelText(f"{title}\nОтмена... дождитесь отката транзакции.")

        worker.progress.connect(on_progress)
        worker.finished.connect(on_finished)
        worker.failed.connect(on_failed)
        worker.cancelled.connect(on_cancelled)
        dialog.canceled.connect(on_cancel_clicked)

        dialog.show()
        thread.start()
        loop.exec()
        dialog.close()
        # Ensure worker thread is fully stopped before leaving this method.
        if thread.isRunning():
            thread.quit()
            thread.wait(10000)

        if result["error"] is not None:
            raise RuntimeError(str(result["error"]))
        if result["cancelled"]:
            return None
        return result["stats"]

    def _set_action_buttons_enabled(self, enabled: bool) -> None:
        buttons = (
            getattr(self, "checkin_button", None),
            getattr(self, "execute_button", None),
            getattr(self, "verify_umowy_button", None),
            getattr(self, "rollback_button", None),
            getattr(self, "status_apply_button", None),
        )
        for button in buttons:
            if button is not None:
                button.setEnabled(enabled)

    def _run_db_preflight(self, db_service: DatabaseService, operation_name: str) -> bool:
        report = db_service.preflight_check()
        if report.missing_tables:
            missing = "\n".join(f"- {name}" for name in report.missing_tables)
            QMessageBox.critical(
                self,
                "Preflight: отсутствуют таблицы",
                (
                    f"Операция '{operation_name}' остановлена.\n"
                    "В базе не найдены обязательные таблицы:\n"
                    f"{missing}"
                ),
            )
            self._log(
                f"Preflight остановил операцию '{operation_name}': "
                f"нет таблиц {', '.join(report.missing_tables)}"
            )
            return False

        if report.permission_warnings:
            warning_text = "\n".join(f"- {line}" for line in report.permission_warnings[:8])
            if len(report.permission_warnings) > 8:
                warning_text += "\n- ..."
            answer = QMessageBox.question(
                self,
                "Preflight: предупреждения прав доступа",
                (
                    f"Для операции '{operation_name}' есть предупреждения:\n\n"
                    f"{warning_text}\n\n"
                    "Продолжить выполнение?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._log(
                    f"Операция '{operation_name}' отменена по предупреждениям preflight."
                )
                return False
        return True

    def _confirm_mass_operation(self, title: str, message: str) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _log(self, message: str) -> None:
        text = str(message)
        try:
            self.logs_text.append(text)
        except Exception:
            pass
        if self.current_log_path is not None:
            try:
                with self.current_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {text}\n")
            except Exception:
                pass

    def _load_settings_to_form(self) -> None:
        db = self.config["database"]
        app = self.config["app"]

        self.driver_input.setText(db.get("driver", "ODBC Driver 17 for SQL Server"))
        self.server_input.setText(db.get("server", "localhost"))
        self.database_input.setText(db.get("database", "PAYROLL_DB"))
        self.username_input.setText(db.get("username", ""))
        stored_password = db.get("password", "")
        self.password_input.setText(decrypt_secret(stored_password))
        # Auto-migrate legacy plaintext passwords: re-save them encrypted
        # so they never leak on disk again.
        if stored_password and not looks_encrypted(stored_password):
            self.config["database"]["password"] = encrypt_secret(stored_password)
            try:
                with CONFIG_PATH.open("w", encoding="utf-8") as cfg:
                    self.config.write(cfg)
            except Exception:
                pass
        self.trusted_checkbox.setChecked(db.getboolean("trusted_connection", True))
        lang = self.config["app"].get("language", self.language)
        lang_idx = self.language_box.findData(lang)
        if lang_idx < 0:
            lang_idx = self.language_box.findData("ru")
        if lang_idx >= 0:
            self.language_box.setCurrentIndex(lang_idx)

        self.start_id_input.setText(app.get("start_urzad_id", "12"))
        self.data_od_input.setText(app.get("data_od", str(self._today_clarion())))
        self.log_file_input.setText(app.get("log_file", "importer.log"))
        self.strict_od_dnia_checkbox.setChecked(app.getboolean("strict_od_dnia", False))
        self._on_strict_od_dnia_toggled(self.strict_od_dnia_checkbox.isChecked())

    def _save_settings(self) -> None:
        if "database" not in self.config:
            self.config["database"] = {}
        if "app" not in self.config:
            self.config["app"] = {}

        self.config["database"]["driver"] = self.driver_input.text().strip()
        self.config["database"]["server"] = self.server_input.text().strip()
        self.config["database"]["database"] = self.database_input.text().strip()
        trusted = self.trusted_checkbox.isChecked()
        if trusted:
            # For Windows-auth we do not need (and should not store) credentials.
            self.config["database"]["username"] = ""
            self.config["database"]["password"] = ""
        else:
            self.config["database"]["username"] = self.username_input.text().strip()
            self.config["database"]["password"] = encrypt_secret(
                self.password_input.text()
            )
        self.config["database"]["trusted_connection"] = "yes" if trusted else "no"
        selected_lang = str(self.language_box.currentData() or "ru")
        old_lang = self.language

        self.config["app"]["start_urzad_id"] = self.start_id_input.text().strip() or "12"
        self.config["app"]["data_od"] = self.data_od_input.text().strip() or str(self._today_clarion())
        self.config["app"]["log_file"] = self.log_file_input.text().strip() or "importer.log"
        self.config["app"]["strict_od_dnia"] = (
            "yes" if self.strict_od_dnia_checkbox.isChecked() else "no"
        )
        self.config["app"]["language"] = selected_lang

        with CONFIG_PATH.open("w", encoding="utf-8") as cfg:
            self.config.write(cfg)

        self._log("Настройки сохранены в config.ini")
        if selected_lang != old_lang:
            QMessageBox.information(
                self,
                self._t("common.done"),
                (
                    "Настройки сохранены. Перезапустите приложение, чтобы применить новый язык."
                    if self.language == "ru"
                    else "Ustawienia zapisano. Uruchom ponownie aplikację, aby zastosować nowy język."
                ),
            )
        else:
            QMessageBox.information(
                self,
                self._t("common.done"),
                "Настройки сохранены." if self.language == "ru" else "Ustawienia zapisano.",
            )

    def _read_db_config_from_form(self) -> DbConfig:
        return DbConfig(
            driver=self.driver_input.text().strip(),
            server=self.server_input.text().strip(),
            database=self.database_input.text().strip(),
            username=self.username_input.text().strip(),
            password=self.password_input.text().strip(),
            trusted_connection=self.trusted_checkbox.isChecked(),
        )

    def _test_connection(self) -> None:
        service = DatabaseService(self._read_db_config_from_form())
        ok, message = service.test_connection()
        if ok:
            QMessageBox.information(self, "Подключение", message)
            self._log(message)
        else:
            QMessageBox.critical(self, "Подключение", message)
            self._log(message)

    def _today_clarion(self) -> int:
        base = datetime(1800, 12, 28).date()
        return (datetime.now().date() - base).days

    def _read_data_od(self) -> int:
        value = self.data_od_input.text().strip()
        if not value:
            return self._today_clarion()
        if not value.isdigit():
            raise ValueError("DATA_OD musi byc dodatnia liczba (format Clarion int).")
        return int(value)

    _MAX_HISTORY_ENTRIES = 20

    def _describe_history_record(self, record: dict) -> str:
        timestamp = record.get("timestamp", "?")
        label = record.get("profile_label") or record.get("profile", "?")
        counts: list[str] = []
        id_fields = (
            ("created_employee_ids", "сотрудников"),
            ("created_urzedy_ids", "urzedow"),
            ("created_link_ids", "связей"),
            ("created_address_ids", "адресов"),
            ("created_contract_ids", "договоров"),
            ("created_insurance_ids", "страховок"),
        )
        for field, noun in id_fields:
            value = record.get(field) or []
            if value:
                counts.append(f"{noun}={len(value)}")
        counts_text = ", ".join(counts) if counts else "пусто"
        return f"[{timestamp}] {label} — {counts_text}"

    def _pick_history_record_to_rollback(self, history: list[dict]) -> Optional[dict]:
        if len(history) == 1:
            return history[0]
        items = [self._describe_history_record(rec) for rec in reversed(history)]
        selected, ok = QInputDialog.getItem(
            self,
            "Выбор импорта для отката",
            "Какой импорт откатить?",
            items,
            0,
            False,
        )
        if not ok or not selected:
            return None
        index = items.index(selected)
        # We reversed for display, so map back to original order.
        return history[len(history) - 1 - index]

    def _save_last_import_record(self, import_stats) -> None:
        def _ids(attr_name: str) -> list[int]:
            value = getattr(import_stats, attr_name, None)
            if not value:
                return []
            return [int(item) for item in value]

        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "profile": self.active_profile.key,
            "profile_label": self._profile_display_label(self.active_profile.key),
            "created_urzedy_ids": _ids("created_urzedy_ids"),
            "created_link_ids": _ids("created_link_ids"),
            "created_address_ids": _ids("created_address_ids"),
            "created_employee_ids": _ids("created_employee_ids"),
            "created_contract_ids": _ids("created_contract_ids"),
            "created_insurance_ids": _ids("created_insurance_ids"),
        }
        history = self._load_import_history()
        history.append(record)
        # Cap to avoid unlimited growth of the history file.
        if len(history) > self._MAX_HISTORY_ENTRIES:
            history = history[-self._MAX_HISTORY_ENTRIES :]
        IMPORT_HISTORY_PATH.write_text(
            json.dumps(history, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _load_import_history(self) -> list[dict]:
        """Load history as a list. Keeps backward compat with legacy single-record file."""
        if not IMPORT_HISTORY_PATH.exists():
            return []
        content = IMPORT_HISTORY_PATH.read_text(encoding="utf-8").strip()
        if not content:
            return []
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return []
        if isinstance(data, list):
            return [entry for entry in data if isinstance(entry, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    def _load_last_import_record(self) -> Optional[dict]:
        history = self._load_import_history()
        return history[-1] if history else None

    def _remove_history_record(self, record: dict) -> None:
        history = self._load_import_history()
        timestamp = record.get("timestamp")
        history = [
            entry for entry in history
            if entry.get("timestamp") != timestamp or entry.get("profile") != record.get("profile")
        ]
        IMPORT_HISTORY_PATH.write_text(
            json.dumps(history, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _clear_last_import_record(self) -> None:
        """Backwards-compat wrapper. Removes only the last record, not the whole history."""
        history = self._load_import_history()
        if not history:
            return
        history.pop()
        IMPORT_HISTORY_PATH.write_text(
            json.dumps(history, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _start_operation_log(self, operation: str) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        profile_name = self._slugify(self.active_profile.label)
        file_name = f"{timestamp}_{profile_name}_{operation}.log"
        self.current_log_path = LOGS_DIR / file_name
        with self.current_log_path.open("w", encoding="utf-8") as handle:
            handle.write(
                f"Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Import: {self.active_profile.label}\n"
                f"Operation: {operation}\n"
                "----------------------------------------\n"
            )

    def _slugify(self, value: str) -> str:
        allowed = []
        for char in value.lower():
            if char.isalnum():
                allowed.append(char)
            elif char in (" ", "-", "_"):
                allowed.append("-")
        result = "".join(allowed).strip("-")
        while "--" in result:
            result = result.replace("--", "-")
        return result or "import"

    def _on_profile_changed(self) -> None:
        profile_key = self.import_type_box.currentData()
        if not profile_key:
            return
        self.active_profile = AVAILABLE_PROFILES[profile_key]
        self._update_employee_lookup_visibility()
        self._update_firma_selector_visibility()
        self._rebuild_mapping_ui()
        self._log(f"Выбран профиль импорта: {self.active_profile.label}")
        self._set_kpi_value(self.kpi_ready_rows, 0)
        self._set_kpi_value(self.kpi_errors, 0)
        if self.source_df is not None:
            self._populate_mapping_boxes(self.source_df.columns.tolist())

    def _on_employee_lookup_mode_changed(self) -> None:
        if not self._supports_lookup_selection():
            return
        self._rebuild_mapping_ui()
        if self.source_df is not None:
            self._populate_mapping_boxes(self.source_df.columns.tolist())

    def _supports_lookup_selection(self) -> bool:
        return self.active_profile.key != EMPLOYEE_IMPORT_PROFILE.key

    def _active_employee_lookup_mode(self) -> str:
        if not self._supports_lookup_selection():
            return "nr"
        return str(self.employee_lookup_mode_box.currentData() or "nr")

    def _effective_required_fields(self) -> tuple[str, ...]:
        fields = list(self.active_profile.required_fields)
        if not self._supports_lookup_selection():
            return tuple(fields)
        mode = self._active_employee_lookup_mode()
        remove_field = "PESEL" if mode == "nr" else "NR Ewidencyjny"
        if self.active_profile.key == LEGACY_URZEDY_PROFILE.key and mode == "pesel":
            fields.append("PESEL")
            remove_field = "Nr Ewidencyjny"
        fields = [field for field in fields if field != remove_field]
        return tuple(dict.fromkeys(fields))

    def _update_employee_lookup_visibility(self) -> None:
        visible = self._supports_lookup_selection()
        self.employee_lookup_label.setVisible(visible)
        self.employee_lookup_mode_box.setVisible(visible)

    def _update_firma_selector_visibility(self) -> None:
        # Firm selector is shown for all profiles that touch employees or contracts.
        # Only the legacy urzedy-link profile has employee_id already pre-resolved —
        # firm filter is irrelevant there.
        from importer import LEGACY_URZEDY_PROFILE as _LP
        visible = self.active_profile.key != _LP.key
        self.firma_label.setVisible(visible)
        self.firma_combo.setVisible(visible)

    def _selected_id_firmy(self) -> int:
        """Return the currently selected ID_FIRMY (1 or 2)."""
        data = self.firma_combo.currentData()
        return int(data) if data is not None else 1

    def _rebuild_mapping_ui(self) -> None:
        while self.mapping_layout.count():
            item = self.mapping_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self.mapping_boxes.clear()
        for idx, field in enumerate(self._effective_required_fields()):
            label = QLabel(field)
            box = QComboBox()
            box.setMinimumWidth(300)
            self.mapping_layout.addWidget(label, idx, 0)
            self.mapping_layout.addWidget(box, idx, 1)
            self.mapping_boxes[field] = box


def main() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    def _global_excepthook(exc_type, exc_value, exc_tb):
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            with CRASH_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Unhandled exception\n"
                )
                handle.write(tb_text)
                handle.write("\n")
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _global_excepthook
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
