APP_STYLESHEET = """
QWidget {
    font-family: Segoe UI, Arial, sans-serif;
    font-size: 10pt;
    color: #e5e7eb;
}
QMainWindow {
    background: #0b1020;
}
QTabWidget::pane {
    border: 1px solid #2e3b57;
    border-radius: 12px;
    background: #0f172a;
    top: -1px;
}
QTabBar {
    background: #0f172a;
}
QTabBar::tab {
    background: #1a2438;
    color: #a7b3cb;
    border: 1px solid #2e3b57;
    border-bottom: none;
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
    padding: 8px 14px;
    margin-right: 4px;
    min-width: 120px;
}
QTabBar::tab:selected {
    background: #0f172a;
    color: #f8fafc;
    font-weight: 600;
}
QPushButton {
    background: #2f6fed;
    color: #ffffff;
    border: 1px solid #2f6fed;
    border-radius: 9px;
    padding: 7px 14px;
    min-height: 30px;
    font-weight: 600;
}
QPushButton:hover {
    background: #215ed6;
    border: 1px solid #215ed6;
}
QPushButton:pressed {
    background: #1b4fb7;
    border: 1px solid #1b4fb7;
}
QPushButton:disabled {
    background: #2e3b57;
    border-color: #2e3b57;
    color: #8fa0be;
}
QPushButton#PrimaryAction {
    background: #179f4d;
    border-color: #179f4d;
}
QPushButton#PrimaryAction:hover {
    background: #11813d;
    border-color: #11813d;
}
QPushButton#DangerAction {
    background: #d43b4e;
    border-color: #d43b4e;
}
QPushButton#DangerAction:hover {
    background: #b02d3f;
    border-color: #b02d3f;
}
QPushButton#SecondaryAction {
    background: #334155;
    border-color: #334155;
}
QPushButton#SecondaryAction:hover {
    background: #283446;
    border-color: #283446;
}
QLineEdit, QComboBox, QTextEdit, QTableWidget {
    background: #0a1222;
    border: 1px solid #2e3b57;
    border-radius: 9px;
    padding: 6px 8px;
    color: #e5e7eb;
}
QComboBox QAbstractItemView {
    background: #0a1222;
    color: #e5e7eb;
    border: 1px solid #2e3b57;
    selection-background-color: #215ed6;
    selection-color: #ffffff;
}
QLineEdit:focus, QComboBox:focus, QTextEdit:focus {
    border: 1px solid #60a5fa;
}
QGroupBox {
    border: 1px solid #2e3b57;
    border-radius: 12px;
    margin-top: 12px;
    padding: 10px;
    background: #0f172a;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: #cbd5e1;
    font-weight: 600;
}
QHeaderView::section {
    background: #182238;
    color: #e2e8f0;
    border: none;
    border-bottom: 1px solid #2e3b57;
    padding: 8px;
    font-weight: 600;
}
QTableWidget {
    gridline-color: #1f2937;
    selection-background-color: #215ed6;
}
QScrollBar:vertical {
    background: #0f172a;
    width: 10px;
}
QScrollBar::handle:vertical {
    background: #334155;
    border-radius: 5px;
}
QStatusBar {
    background: #0a1222;
    color: #cbd5e1;
}
QDialog, QMessageBox {
    background: #0f172a;
    color: #e5e7eb;
}
QDialog QLabel, QMessageBox QLabel {
    color: #e5e7eb;
    background: transparent;
}
QMessageBox QPushButton {
    min-width: 72px;
    min-height: 30px;
}
QScrollArea {
    background: #0a1222;
    border: 1px solid #2e3b57;
    border-radius: 10px;
}
QScrollArea > QWidget > QWidget {
    background: #0a1222;
}
QFrame#AppHeader {
    background: #0f172a;
    border: 1px solid #2e3b57;
    border-radius: 12px;
}
QFrame#KpiCard {
    background: #111c31;
    border: 1px solid #2e3b57;
    border-radius: 10px;
}
QLabel#KpiTitle {
    color: #93a4c4;
    font-size: 9pt;
}
QLabel#KpiValue {
    color: #f8fafc;
    font-size: 14pt;
    font-weight: 700;
}
"""
