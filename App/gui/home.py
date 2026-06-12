"""Home page — device + last-scan + storage summary + live device status."""

import os
import shutil
import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QGraphicsDropShadowEffect, QSizePolicy,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor

import config
from gui.main_window import (
    PRIMARY, PRIMARY_DARK, TEXT, LABEL, SUBTLE, BORDER, PANEL_BG,
    _GradientLabel,
)
from gui.device_status import DeviceStatusPanel
from gui.scan_stats import (
    list_scans, format_size, estimate_gb_per_hour, estimate_hours_remaining,
)


class HomePage(QWidget):
    def __init__(self, shell):
        super().__init__()
        self._shell = shell
        self._build_ui()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_all)
        self._refresh_timer.start(10_000)
        self._refresh_all()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 22, 28, 20)
        outer.setSpacing(16)

        title = _GradientLabel('HOME', PRIMARY, PRIMARY_DARK)
        title.setStyleSheet('font-size: 24pt; font-weight: bold;')
        outer.addWidget(title)

        sub = QLabel(f'Welcome — {config.DEVICE} is ready.')
        sub.setStyleSheet(f'color: {SUBTLE}; font-size: 11pt;')
        outer.addWidget(sub)

        # ── Top card row ────────────────────────────────────────────────────
        cards = QHBoxLayout()
        cards.setSpacing(14)

        self._device_card    = self._card('DEVICE', config.DEVICE, 'Multi-sensor scanner')
        self._last_scan_card = self._card('LAST SCAN', '—', '')
        self._storage_card   = self._card('STORAGE FREE', '—', '', big_hint=True)

        cards.addWidget(self._device_card)
        cards.addWidget(self._last_scan_card)
        cards.addWidget(self._storage_card)
        outer.addLayout(cards)

        # ── Devices status banner ───────────────────────────────────────────
        outer.addWidget(DeviceStatusPanel(horizontal=True))

        # ── CTA ─────────────────────────────────────────────────────────────
        cta = QPushButton('▶  Start a New Scan')
        cta.setMinimumHeight(52)
        cta.setCursor(Qt.CursorShape.PointingHandCursor)
        cta.setStyleSheet(
            f'QPushButton {{ background-color: {PRIMARY}; color: white;'
            f'  border-radius: 10px; font-size: 14pt; font-weight: bold; border: none; }} '
            f'QPushButton:hover {{ background-color: {PRIMARY_DARK}; }}'
        )
        cta.clicked.connect(lambda: self._shell.jump_to('scan'))
        outer.addWidget(cta)

        outer.addStretch()

    def _card(self, label: str, value: str, hint: str, big_hint: bool = False) -> QFrame:
        card = QFrame()
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        card.setStyleSheet(
            f'QFrame {{ background-color: {PANEL_BG}; border: 1px solid {BORDER};'
            f'  border-radius: 14px; }} '
            'QLabel { background: transparent; border: 0; }'
        )
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(16)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(1, 89, 196, 28))
        card.setGraphicsEffect(shadow)

        col = QVBoxLayout(card)
        col.setContentsMargins(20, 16, 20, 16)
        col.setSpacing(4)

        lbl = QLabel(label)
        lbl.setStyleSheet(f'color: {SUBTLE}; font-size: 10pt; font-weight: bold;')
        col.addWidget(lbl)

        val = QLabel(value)
        val.setObjectName('value')
        val.setStyleSheet(f'color: {TEXT}; font-size: 22pt; font-weight: bold;')
        col.addWidget(val)

        hint_lbl = QLabel(hint)
        hint_lbl.setObjectName('hint')
        hint_lbl.setWordWrap(True)
        if big_hint:
            hint_lbl.setStyleSheet(
                f'color: {PRIMARY}; font-size: 13pt; font-weight: bold;')
        else:
            hint_lbl.setStyleSheet(f'color: {LABEL}; font-size: 11pt;')
        col.addWidget(hint_lbl)
        col.addStretch()
        return card

    # ── Refresh ─────────────────────────────────────────────────────────────

    def set_scan_active(self, scanning: bool):
        """Pause the periodic refresh while a scan is recording.

        `_refresh_all` → `list_scans` walks the ENTIRE dumps tree and
        stats every file — on the same disk rosbag is writing to. The
        timer fires even when Home isn't the visible page, so without
        this it competes with the recorder every 10 s (same class of
        contention as the 1-Hz listdir loop that cost ~50 IMU msgs/bag).
        """
        if scanning:
            self._refresh_timer.stop()
        else:
            self._refresh_timer.start(10_000)
            self._refresh_all()

    def _refresh_all(self):
        scans = list_scans(os.path.join(config.DUMP_PATH, 'dumps'))
        self._refresh_last_scan(scans)
        self._refresh_storage(scans)

    def _refresh_last_scan(self, scans: list):
        val  = self._last_scan_card.findChild(QLabel, 'value')
        hint = self._last_scan_card.findChild(QLabel, 'hint')
        if not scans:
            val.setText('No scans yet')
            hint.setText('Start your first scan from the Scan tab.')
            return
        info = scans[0]
        when = info.get('stopped_at') or info.get('mtime')
        try:
            dt = datetime.datetime.fromisoformat(when)
            when_txt = dt.strftime('%d %b %Y  ·  %H:%M')
        except Exception:
            when_txt = str(when)
        val.setText(info.get('site', 'Scan'))
        hint.setText(when_txt)

    def _refresh_storage(self, scans: list):
        val  = self._storage_card.findChild(QLabel, 'value')
        hint = self._storage_card.findChild(QLabel, 'hint')
        try:
            usage = shutil.disk_usage(config.DUMP_PATH)
        except Exception:
            val.setText('—')
            hint.setText('Disk info unavailable')
            return

        val.setText(format_size(usage.free))

        gb_per_h = estimate_gb_per_hour(scans)
        hours    = estimate_hours_remaining(usage.free, gb_per_h)
        if hours is None:
            hint.setText('—')
            return
        if hours >= 1:
            hint.setText(f'≈ {hours:.0f} hours of scan time')
        else:
            hint.setText(f'≈ {int(hours * 60)} minutes of scan time')

    def on_show(self):
        self._refresh_all()
