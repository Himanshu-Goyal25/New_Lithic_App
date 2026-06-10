"""Themed PIN-entry dialog with a 10-key touch pad."""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGridLayout,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from gui.main_window import (
    BG, PRIMARY, PRIMARY_DARK, DANGER, TEXT, LABEL,
    BORDER, PANEL_BG,
)
from gui.scan_list import _make_themed_card, _center_on_parent
from gui import supervisor


class _PinDialog(QDialog):
    _MAX_LEN = 8

    def __init__(self, parent=None, *, prompt: str = 'Enter Supervisor PIN'):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self._digits = ''
        self._prompt = prompt
        self.setFixedSize(360, 460)

        layout = _make_themed_card(self, PRIMARY)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel(prompt)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f'color: {PRIMARY}; font-size: 13pt; font-weight: bold; border: none;')
        layout.addWidget(title)

        self._display = QLabel('')
        self._display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._display.setFixedHeight(44)
        self._display.setStyleSheet(
            f'color: {TEXT}; background: {PANEL_BG};'
            f'border: 1px solid {BORDER}; border-radius: 6px;'
            f'font-size: 22pt;')
        f = self._display.font()
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 8)
        self._display.setFont(f)
        layout.addWidget(self._display)

        grid = QGridLayout()
        grid.setSpacing(6)
        keys = [
            ('1', 0, 0), ('2', 0, 1), ('3', 0, 2),
            ('4', 1, 0), ('5', 1, 1), ('6', 1, 2),
            ('7', 2, 0), ('8', 2, 1), ('9', 2, 2),
            ('⌫', 3, 0), ('0', 3, 1), ('OK', 3, 2),
        ]
        for label, r, c in keys:
            btn = QPushButton(label)
            btn.setMinimumHeight(54)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            if label == 'OK':
                btn.setStyleSheet(self._primary_btn())
                btn.clicked.connect(self._on_ok)
            elif label == '⌫':
                btn.setStyleSheet(self._neutral_btn())
                btn.clicked.connect(self._on_backspace)
            else:
                btn.setStyleSheet(self._neutral_btn())
                btn.clicked.connect(lambda _checked=False, d=label: self._on_digit(d))
            grid.addWidget(btn, r, c)
        layout.addLayout(grid)

        self._status = QLabel('')
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet(
            f'color: {DANGER}; font-size: 10pt; border: none;')
        layout.addWidget(self._status)

        cancel = QPushButton('Cancel')
        cancel.setMinimumHeight(34)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {LABEL};'
            f'  border: 1px solid {BORDER}; border-radius: 6px;'
            f'  padding: 0 14px; font-size: 10pt; }} '
            f'QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }}'
        )
        cancel.clicked.connect(self.reject)
        layout.addWidget(cancel)

    def showEvent(self, event):
        super().showEvent(event)
        _center_on_parent(self)

    def _on_digit(self, d: str):
        if len(self._digits) < self._MAX_LEN:
            self._digits += d
            self._refresh_display()

    def _on_backspace(self):
        self._digits = self._digits[:-1]
        self._refresh_display()

    def _on_ok(self):
        if not self._digits:
            return
        if supervisor.verify(self._digits):
            self.accept()
        else:
            self._status.setText('Incorrect PIN')
            self._digits = ''
            self._refresh_display()

    def _refresh_display(self):
        self._display.setText('●' * len(self._digits))
        self._status.setText('')

    @staticmethod
    def _neutral_btn() -> str:
        return (
            f'QPushButton {{ background: {BG}; color: {TEXT};'
            f'  border: 1px solid {BORDER}; border-radius: 8px;'
            f'  font-size: 16pt; font-weight: bold; }} '
            f'QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }} '
            f'QPushButton:pressed {{ background: {PANEL_BG}; }}'
        )

    @staticmethod
    def _primary_btn() -> str:
        return (
            f'QPushButton {{ background: {PRIMARY}; color: white;'
            f'  border: none; border-radius: 8px;'
            f'  font-size: 14pt; font-weight: bold; }} '
            f'QPushButton:hover {{ background: {PRIMARY_DARK}; }}'
        )
