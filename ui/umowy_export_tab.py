from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable

from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from database import DatabaseService, DbConfig


class UmowyExportTab(QWidget):
    """Eksport umów z bazy do Excel (UD / UZ)."""

    def __init__(
        self,
        db_config_provider: Callable[[], DbConfig],
        log_callback: Callable[[str], None],
        translate: Callable[[str], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db_config_provider = db_config_provider
        self._log = log_callback
        self._t = translate or (lambda key: key)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel(self._t("export.umowy.title"))
        title.setStyleSheet("font-size: 10pt; font-weight: 600; color: #e2e8f0;")
        layout.addWidget(title)

        hint = QLabel(self._t("export.umowy.hint"))
        hint.setStyleSheet("color: #94a3b8;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        box = QGroupBox(self._t("export.umowy.group"))
        form = QVBoxLayout(box)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel(self._t("export.umowy.mode")))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(self._t("export.umowy.ud"), 2)
        self.mode_combo.addItem(self._t("export.umowy.uz"), 1)
        mode_row.addWidget(self.mode_combo, stretch=1)
        form.addLayout(mode_row)

        year_row = QHBoxLayout()
        year_row.addWidget(QLabel(self._t("export.umowy.year_label")))
        self.year_spin = QSpinBox()
        self.year_spin.setRange(0, 2100)
        self.year_spin.setValue(date.today().year)
        year_row.addWidget(self.year_spin)
        year_row.addStretch()
        form.addLayout(year_row)

        year_help = QLabel(self._t("export.umowy.year_hint"))
        year_help.setStyleSheet("color: #94a3b8; font-size: 9pt;")
        year_help.setWordWrap(True)
        form.addWidget(year_help)

        month_row = QHBoxLayout()
        month_row.addWidget(QLabel(self._t("export.umowy.month_label")))
        self.month_spin = QSpinBox()
        self.month_spin.setRange(0, 12)
        self.month_spin.setValue(0)
        month_row.addWidget(self.month_spin)
        month_row.addStretch()
        form.addLayout(month_row)

        month_help = QLabel(self._t("export.umowy.month_hint"))
        month_help.setStyleSheet("color: #94a3b8; font-size: 9pt;")
        month_help.setWordWrap(True)
        form.addWidget(month_help)

        layout.addWidget(box)

        export_btn = QPushButton(self._t("export.umowy.button"))
        export_btn.clicked.connect(self._on_export)
        layout.addWidget(export_btn)

        sidelist_hint = QLabel(self._t("export.umowy.sidelist_hint"))
        sidelist_hint.setStyleSheet("color: #94a3b8;")
        sidelist_hint.setWordWrap(True)
        layout.addWidget(sidelist_hint)

        sidelist_btn = QPushButton(self._t("export.umowy.sidelist_button"))
        sidelist_btn.clicked.connect(self._on_export_sidelist)
        layout.addWidget(sidelist_btn)

        sidelist_uz_btn = QPushButton(self._t("export.umowy.sidelist_zlecenie_button"))
        sidelist_uz_btn.clicked.connect(self._on_export_sidelist_zlecenie)
        layout.addWidget(sidelist_uz_btn)

        sidelist_ud_btn = QPushButton(self._t("export.umowy.sidelist_dzielo_button"))
        sidelist_ud_btn.clicked.connect(self._on_export_sidelist_dzielo)
        layout.addWidget(sidelist_ud_btn)

        layout.addStretch()

    def _on_export(self) -> None:
        rodzaj = int(self.mode_combo.currentData())
        rok = int(self.year_spin.value())
        rok_arg = None if rok <= 0 else rok
        miesiac = int(self.month_spin.value())
        miesiac_arg = None if miesiac <= 0 else miesiac

        try:
            service = DatabaseService(self._db_config_provider())
            ok, msg = service.test_connection()
            if not ok:
                QMessageBox.critical(self, self._t("export.umowy.err_title"), msg)
                self._log(f"[export/umowy] brak połączenia: {msg}")
                return

            df = service.umowy_export_dataframe(
                rodzaj_umowy=rodzaj,
                rok_wyplaty=rok_arg,
                miesiac_wyplaty=miesiac_arg,
            )
        except Exception as exc:
            QMessageBox.critical(self, self._t("export.umowy.err_title"), str(exc))
            self._log(f"[export/umowy] błąd: {exc}")
            return

        if df.empty:
            QMessageBox.information(self, self._t("common.done"), self._t("export.umowy.empty"))
            self._log("[export/umowy] brak rekordów")
            return

        default_name = "eksport_UZ_" if rodzaj == 1 else "eksport_UD_"
        if rok_arg:
            default_name += f"{rok_arg}_"
        if miesiac_arg:
            default_name += f"m{miesiac_arg:02d}_"
        default_name += f"{date.today():%Y%m%d}.xlsx"

        path, _ = QFileDialog.getSaveFileName(
            self,
            self._t("export.umowy.save_title"),
            str(Path.home() / default_name),
            "Excel (*.xlsx)",
        )
        if not path:
            return
        save_path = path if path.lower().endswith(".xlsx") else path + ".xlsx"

        try:
            df.to_excel(save_path, index=False)
        except Exception as exc:
            QMessageBox.critical(self, self._t("export.umowy.err_title"), str(exc))
            self._log(f"[export/umowy] zapis Excel: {exc}")
            return

        self._log(self._t("export.umowy.success", path=save_path, rows=len(df)))
        QMessageBox.information(
            self,
            self._t("common.done"),
            self._t("export.umowy.success", path=save_path, rows=len(df)),
        )

    def _on_export_sidelist(self) -> None:
        rok = int(self.year_spin.value())
        rok_arg = None if rok <= 0 else rok
        miesiac = int(self.month_spin.value())
        miesiac_arg = None if miesiac <= 0 else miesiac

        try:
            service = DatabaseService(self._db_config_provider())
            ok, msg = service.test_connection()
            if not ok:
                QMessageBox.critical(self, self._t("export.umowy.err_title"), msg)
                self._log(f"[export/umowy/sidelist] brak połączenia: {msg}")
                return

            df = service.umowy_export_sidelist_both_types_dataframe(
                rok_wyplaty=rok_arg,
                miesiac_wyplaty=miesiac_arg,
            )
        except Exception as exc:
            QMessageBox.critical(self, self._t("export.umowy.err_title"), str(exc))
            self._log(f"[export/umowy/sidelist] błąd: {exc}")
            return

        if df.empty:
            QMessageBox.information(
                self, self._t("common.done"), self._t("export.umowy.sidelist_empty")
            )
            self._log("[export/umowy/sidelist] brak rekordów")
            return

        default_name = "eksport_lista_PESEL_UZ_UD_"
        if rok_arg:
            default_name += f"{rok_arg}_"
        if miesiac_arg:
            default_name += f"m{miesiac_arg:02d}_"
        default_name += f"{date.today():%Y%m%d}.xlsx"

        path, _ = QFileDialog.getSaveFileName(
            self,
            self._t("export.umowy.save_title"),
            str(Path.home() / default_name),
            "Excel (*.xlsx)",
        )
        if not path:
            return
        save_path = path if path.lower().endswith(".xlsx") else path + ".xlsx"

        try:
            df.to_excel(save_path, index=False)
        except Exception as exc:
            QMessageBox.critical(self, self._t("export.umowy.err_title"), str(exc))
            self._log(f"[export/umowy/sidelist] zapis Excel: {exc}")
            return

        self._log(self._t("export.umowy.success", path=save_path, rows=len(df)))
        QMessageBox.information(
            self,
            self._t("common.done"),
            self._t("export.umowy.success", path=save_path, rows=len(df)),
        )

    def _on_export_sidelist_zlecenie(self) -> None:
        rok = int(self.year_spin.value())
        rok_arg = None if rok <= 0 else rok
        miesiac = int(self.month_spin.value())
        miesiac_arg = None if miesiac <= 0 else miesiac

        try:
            service = DatabaseService(self._db_config_provider())
            ok, msg = service.test_connection()
            if not ok:
                QMessageBox.critical(self, self._t("export.umowy.err_title"), msg)
                self._log(f"[export/umowy/sidelist_uz] brak połączenia: {msg}")
                return

            df = service.umowy_export_sidelist_zlecenie_dataframe(
                rok_wyplaty=rok_arg,
                miesiac_wyplaty=miesiac_arg,
            )
        except Exception as exc:
            QMessageBox.critical(self, self._t("export.umowy.err_title"), str(exc))
            self._log(f"[export/umowy/sidelist_uz] błąd: {exc}")
            return

        if df.empty:
            QMessageBox.information(
                self, self._t("common.done"), self._t("export.umowy.sidelist_empty")
            )
            self._log("[export/umowy/sidelist_uz] brak rekordów")
            return

        default_name = "eksport_lista_PESEL_UZ_"
        if rok_arg:
            default_name += f"{rok_arg}_"
        if miesiac_arg:
            default_name += f"m{miesiac_arg:02d}_"
        default_name += f"{date.today():%Y%m%d}.xlsx"

        path, _ = QFileDialog.getSaveFileName(
            self,
            self._t("export.umowy.save_title"),
            str(Path.home() / default_name),
            "Excel (*.xlsx)",
        )
        if not path:
            return
        save_path = path if path.lower().endswith(".xlsx") else path + ".xlsx"

        try:
            df.to_excel(save_path, index=False)
        except Exception as exc:
            QMessageBox.critical(self, self._t("export.umowy.err_title"), str(exc))
            self._log(f"[export/umowy/sidelist_uz] zapis Excel: {exc}")
            return

        self._log(self._t("export.umowy.success", path=save_path, rows=len(df)))
        QMessageBox.information(
            self,
            self._t("common.done"),
            self._t("export.umowy.success", path=save_path, rows=len(df)),
        )

    def _on_export_sidelist_dzielo(self) -> None:
        rok = int(self.year_spin.value())
        rok_arg = None if rok <= 0 else rok
        miesiac = int(self.month_spin.value())
        miesiac_arg = None if miesiac <= 0 else miesiac

        try:
            service = DatabaseService(self._db_config_provider())
            ok, msg = service.test_connection()
            if not ok:
                QMessageBox.critical(self, self._t("export.umowy.err_title"), msg)
                self._log(f"[export/umowy/sidelist_ud] brak połączenia: {msg}")
                return

            df = service.umowy_export_sidelist_dzielo_dataframe(
                rok_wyplaty=rok_arg,
                miesiac_wyplaty=miesiac_arg,
            )
        except Exception as exc:
            QMessageBox.critical(self, self._t("export.umowy.err_title"), str(exc))
            self._log(f"[export/umowy/sidelist_ud] błąd: {exc}")
            return

        if df.empty:
            QMessageBox.information(
                self, self._t("common.done"), self._t("export.umowy.sidelist_empty")
            )
            self._log("[export/umowy/sidelist_ud] brak rekordów")
            return

        default_name = "eksport_lista_PESEL_UD_"
        if rok_arg:
            default_name += f"{rok_arg}_"
        if miesiac_arg:
            default_name += f"m{miesiac_arg:02d}_"
        default_name += f"{date.today():%Y%m%d}.xlsx"

        path, _ = QFileDialog.getSaveFileName(
            self,
            self._t("export.umowy.save_title"),
            str(Path.home() / default_name),
            "Excel (*.xlsx)",
        )
        if not path:
            return
        save_path = path if path.lower().endswith(".xlsx") else path + ".xlsx"

        try:
            df.to_excel(save_path, index=False)
        except Exception as exc:
            QMessageBox.critical(self, self._t("export.umowy.err_title"), str(exc))
            self._log(f"[export/umowy/sidelist_ud] zapis Excel: {exc}")
            return

        self._log(self._t("export.umowy.success", path=save_path, rows=len(df)))
        QMessageBox.information(
            self,
            self._t("common.done"),
            self._t("export.umowy.success", path=save_path, rows=len(df)),
        )
