"""Runs page — read-only browser of previous scans."""

import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
)
from PySide6.QtCore import Qt

import config
from gui.main_window import (
    PRIMARY, PRIMARY_DARK, SUBTLE, _GradientLabel,
)
from gui.scan_list import ScanListWidget, primary_btn_style
from gui.scan_stats import format_size


class RunsPage(QWidget):
    def __init__(self, shell):
        super().__init__()
        self._shell = shell
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 16)
        outer.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)

        title = _GradientLabel('PREVIOUS SCANS', PRIMARY, PRIMARY_DARK)
        title.setStyleSheet('font-size: 22pt; font-weight: bold;')
        head.addWidget(title)
        head.addStretch()

        refresh = QPushButton('↻  Refresh')
        refresh.setMinimumHeight(34)
        refresh.setMinimumWidth(110)
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.setToolTip('Rescan the dumps folder')
        refresh.setStyleSheet(primary_btn_style())
        refresh.clicked.connect(self._reload)
        head.addWidget(refresh)
        outer.addLayout(head)

        self._total_lbl = QLabel('—')
        self._total_lbl.setStyleSheet(f'color: {SUBTLE}; font-size: 11pt;')
        outer.addWidget(self._total_lbl)

        self._list = ScanListWidget(
            os.path.join(config.DUMP_PATH, 'dumps'), selectable=False)
        self._list.selection_changed.connect(self._on_selection_changed)
        outer.addWidget(self._list, stretch=1)

        self._reload()

    def showEvent(self, event):
        super().showEvent(event)
        self._reload()

    def _reload(self):
        self._list.reload()

    def on_show(self):
        self._reload()

    def _on_selection_changed(self, _selected: int, total: int):
        total_size = sum(r.info.get('size_bytes', 0) for r in self._list._rows)
        self._total_lbl.setText(
            f'{total} scan(s)  ·  total {format_size(total_size)}')
