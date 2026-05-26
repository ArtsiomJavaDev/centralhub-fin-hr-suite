from __future__ import annotations

from datetime import datetime
from typing import Callable

from PyQt6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from db.config import DbConfig
from db.introspection import DatabaseIntrospectionService, WorkingRelationInfo, WorkingTableInfo


class DbOverviewTab(QWidget):
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
        self._translate = translate or (lambda key: key)
        self._tables_index: list[WorkingTableInfo] = []
        self._build_ui()

    def _tr(self, key: str, fallback: str) -> str:
        value = self._translate(key)
        return value if value != key else fallback

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel(self._tr("tab.db", "Обзор рабочих таблиц и связей БД"))
        title.setStyleSheet("font-size: 10pt; font-weight: 600; color: #e2e8f0;")
        self.refresh_button = QPushButton(self._tr("db.refresh", "Обновить"))
        self.refresh_button.clicked.connect(self.refresh_snapshot)
        self.updated_at_label = QLabel(self._tr("db.not_updated", "Еще не обновлялось"))
        self.updated_at_label.setStyleSheet("color: #94a3b8;")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.updated_at_label)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        self.tables_group = QGroupBox(
            self._tr("db.tables_group", "Таблицы, с которыми работает приложение")
        )
        tables_layout = QVBoxLayout(self.tables_group)
        self.tables_table = QTableWidget()
        self.tables_table.setColumnCount(3)
        self.tables_table.setHorizontalHeaderLabels(
            (
                self._tr("db.col.schema", "Схема"),
                self._tr("db.col.table", "Таблица"),
                self._tr("db.col.rows", "Строк"),
            )
        )
        self.tables_table.verticalHeader().setVisible(False)
        tables_layout.addWidget(self.tables_table)
        layout.addWidget(self.tables_group)

        self.relations_group = QGroupBox(
            self._tr("db.relations_group", "Связи между рабочими таблицами")
        )
        relations_layout = QVBoxLayout(self.relations_group)
        self.relations_table = QTableWidget()
        self.relations_table.setColumnCount(5)
        self.relations_table.setHorizontalHeaderLabels(
            (
                "FK",
                self._tr("db.col.parent_table", "Родительская таблица"),
                self._tr("db.col.child_table", "Дочерняя таблица"),
                self._tr("db.col.parent_col", "Колонка parent"),
                self._tr("db.col.child_col", "Колонка child"),
            )
        )
        self.relations_table.verticalHeader().setVisible(False)
        relations_layout.addWidget(self.relations_table)
        layout.addWidget(self.relations_group)

        self.preview_group = QGroupBox(
            self._tr("db.preview_group", "Предпросмотр содержимого таблицы")
        )
        preview_layout = QVBoxLayout(self.preview_group)
        controls = QHBoxLayout()
        self.table_selector = QComboBox()
        self.table_selector.setMinimumWidth(340)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 500)
        self.limit_spin.setValue(100)
        self.load_preview_button = QPushButton(self._tr("db.show_content", "Показать содержимое"))
        self.load_preview_button.clicked.connect(self.load_selected_table_preview)
        controls.addWidget(QLabel(self._tr("db.table", "Таблица")))
        controls.addWidget(self.table_selector)
        controls.addWidget(QLabel(self._tr("db.limit", "Лимит строк")))
        controls.addWidget(self.limit_spin)
        controls.addStretch()
        controls.addWidget(self.load_preview_button)
        preview_layout.addLayout(controls)
        self.preview_table = QTableWidget()
        self.preview_table.verticalHeader().setVisible(False)
        preview_layout.addWidget(self.preview_table)
        layout.addWidget(self.preview_group)

    def refresh_snapshot(self) -> None:
        try:
            service = DatabaseIntrospectionService(self._db_config_provider())
            snapshot = service.load_working_snapshot()
            self._tables_index = snapshot.tables
            self._fill_tables(snapshot.tables)
            self._fill_relations(snapshot.relations)
            self._fill_table_selector(snapshot.tables)
            self.updated_at_label.setText(
                f"Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
            )
            self._log(
                "Обзор БД обновлен: "
                f"таблиц={len(snapshot.tables)}, связей={len(snapshot.relations)}"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка обзора БД", str(exc))
            self._log(f"Ошибка обзора БД: {exc}")

    def _fill_tables(self, tables: list[WorkingTableInfo]) -> None:
        self.tables_table.setRowCount(len(tables))
        for row_idx, info in enumerate(tables):
            self.tables_table.setItem(row_idx, 0, QTableWidgetItem(info.schema_name))
            self.tables_table.setItem(row_idx, 1, QTableWidgetItem(info.table_name))
            self.tables_table.setItem(row_idx, 2, QTableWidgetItem(str(info.row_count)))
        self.tables_table.resizeColumnsToContents()

    def _fill_relations(self, relations: list[WorkingRelationInfo]) -> None:
        self.relations_table.setRowCount(len(relations))
        for row_idx, info in enumerate(relations):
            self.relations_table.setItem(row_idx, 0, QTableWidgetItem(info.fk_name))
            self.relations_table.setItem(row_idx, 1, QTableWidgetItem(info.parent_table))
            self.relations_table.setItem(row_idx, 2, QTableWidgetItem(info.child_table))
            self.relations_table.setItem(row_idx, 3, QTableWidgetItem(info.parent_column))
            self.relations_table.setItem(row_idx, 4, QTableWidgetItem(info.child_column))
        self.relations_table.resizeColumnsToContents()

    def _fill_table_selector(self, tables: list[WorkingTableInfo]) -> None:
        current_text = self.table_selector.currentText()
        self.table_selector.blockSignals(True)
        self.table_selector.clear()
        for table_info in tables:
            fq_name = f"{table_info.schema_name}.{table_info.table_name}"
            self.table_selector.addItem(fq_name)
        if current_text:
            idx = self.table_selector.findText(current_text)
            if idx >= 0:
                self.table_selector.setCurrentIndex(idx)
        self.table_selector.blockSignals(False)

    def load_selected_table_preview(self) -> None:
        selected = self.table_selector.currentText().strip()
        if not selected:
            QMessageBox.information(self, "Нет таблицы", "Сначала обновите обзор БД.")
            return
        if "." not in selected:
            QMessageBox.warning(self, "Ошибка таблицы", f"Не удалось распознать таблицу: {selected}")
            return

        schema_name, table_name = selected.split(".", 1)
        limit = self.limit_spin.value()
        try:
            service = DatabaseIntrospectionService(self._db_config_provider())
            columns, rows = service.load_table_preview(
                schema_name=schema_name,
                table_name=table_name,
                limit=limit,
            )
            self._fill_preview_table(columns, rows)
            self._log(
                f"Предпросмотр таблицы {selected}: показано {len(rows)} строк (лимит={limit})."
            )
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка предпросмотра", str(exc))
            self._log(f"Ошибка предпросмотра таблицы {selected}: {exc}")

    def _fill_preview_table(self, columns: list[str], rows: list[tuple]) -> None:
        self.preview_table.clear()
        self.preview_table.setColumnCount(len(columns))
        self.preview_table.setHorizontalHeaderLabels(columns)
        self.preview_table.setRowCount(len(rows))

        for row_idx, row in enumerate(rows):
            for col_idx, value in enumerate(row):
                self.preview_table.setItem(row_idx, col_idx, QTableWidgetItem(str(value)))
        self.preview_table.resizeColumnsToContents()
