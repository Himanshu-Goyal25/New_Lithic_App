import datetime
import os
import shutil

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
    QFrame, QProgressBar, QSizePolicy,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QLinearGradient, QColor, QBrush, QPen, QFont

import config

# ── Theme palette ───────────────────────────────────────────────────────────
# Re-exported from gui.theme so the rest of the GUI can keep
# `from gui.main_window import BG, PRIMARY, …` imports unchanged while the
# underlying values flip between LIGHT and DARK at startup.
from gui.theme import P as _P
BG              = _P['BG']
PANEL_BG        = _P['PANEL_BG']
SIDEBAR_BG      = _P['SIDEBAR_BG']
BORDER          = _P['BORDER']
TEXT            = _P['TEXT']
LABEL           = _P['LABEL']
SUBTLE          = _P['SUBTLE']
PRIMARY         = _P['PRIMARY']
PRIMARY_DARK    = _P['PRIMARY_DARK']
PRIMARY_PRESSED = _P['PRIMARY_PRESSED']
ACCENT          = _P['ACCENT']
ACCENT_DARK     = _P['ACCENT_DARK']
DANGER          = _P['DANGER']
DANGER_DARK     = _P['DANGER_DARK']
SUCCESS         = _P['SUCCESS']
SUCCESS_DARK    = _P['SUCCESS_DARK']
WARNING         = _P['WARNING']
DISABLED_BG     = _P['DISABLED_BG']
DISABLED_TEXT   = _P['DISABLED_TEXT']
INPUT_BG        = _P['INPUT_BG']
COMBO_HOVER     = _P['COMBO_HOVER']
BTN_HOVER_WASH  = _P['BTN_HOVER_WASH']
CHIP_BG         = _P['CHIP_BG']
VIDEO_BG        = _P['VIDEO_BG']
CONSOLE_BG      = _P['CONSOLE_BG']


class _GradientLabel(QLabel):
    """A QLabel that paints its text with a horizontal linear gradient."""
    def __init__(self, text, start, end, *args, **kwargs):
        super().__init__(text, *args, **kwargs)
        self._start = QColor(start)
        self._end   = QColor(end)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self.font())
        grad = QLinearGradient(0, 0, self.width(), 0)
        grad.setColorAt(0.0, self._start)
        grad.setColorAt(1.0, self._end)
        painter.setPen(QPen(QBrush(grad), 0))
        painter.drawText(self.rect(), int(self.alignment()), self.text())


class _NavButton(QPushButton):
    """Sidebar nav row — icon glyph + label, with selected state."""
    def __init__(self, glyph: str, label: str, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setMinimumHeight(56)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 14, 0)
        row.setSpacing(12)

        self._icon = QLabel(glyph)
        self._icon.setFixedWidth(24)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setStyleSheet('font-size: 18pt; background: transparent;')
        row.addWidget(self._icon)

        self._label = QLabel(label)
        self._label.setStyleSheet(
            'font-size: 13pt; font-weight: bold; background: transparent;')
        row.addWidget(self._label)
        row.addStretch()

        self._apply_style(False)
        self.toggled.connect(self._apply_style)

    def _apply_style(self, checked: bool):
        if checked:
            self.setStyleSheet(
                f'_NavButton {{ background-color: {PRIMARY};'
                f'  border: 0px; outline: 0; border-radius: 28px;'
                f'  text-align: left; }} '
                f'_NavButton:focus {{ border: 0px; outline: 0; }} '
                f'_NavButton QLabel {{ background: transparent; border: 0px; }}'
            )
            self._icon.setStyleSheet(
                'color: white; font-size: 18pt; background: transparent; border: 0;')
            self._label.setStyleSheet(
                'color: white; font-size: 13pt; font-weight: bold; '
                'background: transparent; border: 0;')
        else:
            self.setStyleSheet(
                f'_NavButton {{ background-color: transparent;'
                f'  border: 0px; outline: 0; border-radius: 28px;'
                f'  text-align: left; }} '
                f'_NavButton:hover {{ background-color: rgba(1, 89, 196, 0.15); }} '
                f'_NavButton:focus {{ border: 0px; outline: 0; }} '
                f'_NavButton QLabel {{ background: transparent; border: 0px; }}'
            )
            self._icon.setStyleSheet(
                f'color: {PRIMARY}; font-size: 18pt; background: transparent; border: 0;')
            self._label.setStyleSheet(
                f'color: {LABEL}; font-size: 13pt; font-weight: bold; '
                f'background: transparent; border: 0;')


class MainWindow(QWidget):
    """Application shell — header, left sidebar nav, content stack, footer."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle('INKERS')
        self.setStyleSheet(f'background-color: {BG};')
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)

        self._scanning = False
        self._build_ui()
        self._switch_page(0)   # start on Home

        # Orphan-scan recovery runs once on startup AFTER the shell is
        # painted, otherwise the dialog pops up on a black frame.
        QTimer.singleShot(200, self._offer_orphan_recovery)

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addLayout(self._build_header())

        stripe = QFrame()
        stripe.setFixedHeight(2)
        stripe.setStyleSheet(f'background-color: {PRIMARY}; border: none;')
        outer.addWidget(stripe)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())
        body.addWidget(self._build_content_stack(), stretch=1)
        outer.addLayout(body, stretch=1)

        outer.addLayout(self._build_footer())

    def _build_header(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(18, 10, 14, 8)
        bar.setSpacing(12)

        title = _GradientLabel('INKERS', PRIMARY, PRIMARY_DARK)
        title.setStyleSheet('font-size: 22pt; font-weight: bold;')
        f = title.font()
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 4)
        title.setFont(f)
        bar.addWidget(title)

        bar.addStretch()

        self.clock_label = QLabel()
        self.clock_label.setStyleSheet(
            f'color: {SUBTLE}; font-size: 11pt; font-weight: bold;')
        self._tick_clock()
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)
        bar.addWidget(self.clock_label)

        for name, symbol, color, slot in [
            ('mw_osk',   '⌨', PRIMARY, self._toggle_osk),
            ('mw_min',   '−', PRIMARY, self.showMinimized),
            ('mw_close', '✕', DANGER,  self.close),
        ]:
            btn = QPushButton(symbol)
            btn.setObjectName(name)
            btn.setFixedSize(32, 32)
            btn.setStyleSheet(
                f'QPushButton#{name} {{ background-color: {BG};'
                f'  border: 2px solid {color}; border-radius: 16px;'
                f'  color: {color}; font-size: 14px; font-weight: bold; }} '
                f'QPushButton#{name}:hover {{ background-color: {color};'
                f'  color: {BG}; }}'
            )
            btn.clicked.connect(slot)
            bar.addWidget(btn)
        return bar

    def _toggle_osk(self):
        """Manually flip the on-screen keyboard. The focus-driven
        auto-show/hide in gui/osk.py still works; this is just a way
        for the operator to surface the keyboard without first
        tapping into a text field (handy for sanity-checking that
        squeekboard is alive, or to hide it without changing focus)."""
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        osk = getattr(app, '_osk', None)
        if osk is not None:
            osk.toggle()

    def _confirm_shutdown(self):
        """Operator-driven clean shutdown of the Pi.

        Pulling power without `shutdown` has corrupted the SSD before.
        The kiosk doesn't expose a terminal, so this button is the
        only safe way out for a non-technical operator.

        Refuses while a scan is active — operator must Stop first so
        rosbag flushes cleanly and `scan_info.json` is written. After
        confirmation, calls `systemctl poweroff` (no sudo needed: the
        active desktop user is authorised via systemd-logind's
        policykit rule `org.freedesktop.login1.power-off`).
        """
        import subprocess
        from gui.scan_list import _ConfirmDialog, _show_result
        from PySide6.QtWidgets import QDialog

        scan_page = getattr(self, 'scan_page', None)
        is_scanning = bool(
            scan_page and getattr(scan_page.player_page, 'is_scanning', False))

        if is_scanning:
            _show_result(
                self, 'Stop the scan first',
                'A scan is running. Stop it before shutting down the '
                'device — otherwise the last rosbag may be left '
                'unfinalized.',
                kind='error')
            return

        dlg = _ConfirmDialog(
            self,
            'Shut down the device?',
            'After tapping Shut Down, wait at least 15 seconds before '
            'unplugging the power.',
            kind='error', yes_label='Shut Down', no_label='Cancel')
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            subprocess.Popen(
                ['systemctl', 'poweroff'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            _show_result(
                self, 'Shutdown failed',
                f'Could not invoke systemctl poweroff:\n{e}\n\n'
                f'Try running `sudo systemctl poweroff` from a '
                f'terminal.',
                kind='error')

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setFixedWidth(220)
        sidebar.setStyleSheet(
            f'QFrame {{ background-color: {SIDEBAR_BG}; border: 0; }}'
        )

        col = QVBoxLayout(sidebar)
        col.setContentsMargins(10, 18, 10, 18)
        col.setSpacing(8)

        items = [
            ('⌂', 'Home'),
            ('▶', 'Scan'),
            ('⊟', 'Runs'),
            ('↑', 'Data Transfer'),
            ('⚙', 'Menu'),
        ]
        self.nav_buttons = []
        for idx, (glyph, label) in enumerate(items):
            btn = _NavButton(glyph, label)
            btn.clicked.connect(lambda _checked=False, i=idx: self._on_nav_clicked(i))
            col.addWidget(btn)
            self.nav_buttons.append(btn)

        col.addStretch()

        # Shutdown button — sits directly above the DEVICE chip so the
        # operator has a single obvious way to cleanly power off the
        # Pi. Skipping it (i.e. pulling the cord) has corrupted the
        # filesystem before. A confirm dialog gates the action.
        shutdown_btn = QPushButton('⏻  Shutdown')
        shutdown_btn.setObjectName('mw_shutdown')
        shutdown_btn.setMinimumHeight(40)
        shutdown_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        shutdown_btn.setStyleSheet(
            f'QPushButton#mw_shutdown {{'
            f'  background-color: {BG};'
            f'  border: 2px solid {DANGER};'
            f'  border-radius: 10px;'
            f'  color: {DANGER};'
            f'  font-size: 12pt; font-weight: bold;'
            f'  padding: 4px 8px;'
            f'}}'
            f'QPushButton#mw_shutdown:hover {{'
            f'  background-color: {DANGER};'
            f'  color: white;'
            f'}}'
        )
        shutdown_btn.clicked.connect(self._confirm_shutdown)
        col.addWidget(shutdown_btn)

        chip = QLabel(f'DEVICE  ·  {config.DEVICE}')
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip.setStyleSheet(
            f'color: {PRIMARY_DARK}; font-size: 10pt; font-weight: bold;'
            f'background-color: rgba(1, 89, 196, 0.08);'
            f'border-radius: 12px; padding: 8px 6px;'
        )
        col.addWidget(chip)
        return sidebar

    def _build_content_stack(self) -> QStackedWidget:
        from gui.home          import HomePage
        from gui.scan_page     import ScanPage
        from gui.runs          import RunsPage
        from gui.data_transfer import DataTransferPage
        from gui.settings_page import SettingsPage

        self.stack = QStackedWidget()

        self.home_page    = HomePage(self)
        self.scan_page    = ScanPage(self)
        self.runs_page    = RunsPage(self)
        self.xfer_page    = DataTransferPage(self)
        self.settings_pg  = SettingsPage(self)

        self.stack.addWidget(self.home_page)
        self.stack.addWidget(self.scan_page)
        self.stack.addWidget(self.runs_page)
        self.stack.addWidget(self.xfer_page)
        self.stack.addWidget(self.settings_pg)

        self.scan_page.scan_state_changed.connect(self._on_scan_state_changed)
        return self.stack

    def _build_footer(self) -> QHBoxLayout:
        footer = QHBoxLayout()
        footer.setContentsMargins(16, 4, 16, 8)
        footer.setSpacing(10)

        self.scan_indicator = QLabel('')
        self.scan_indicator.setStyleSheet(f'color: {SUBTLE}; font-size: 10pt;')
        footer.addWidget(self.scan_indicator)

        footer.addStretch()

        self.disk_label = QLabel()
        self.disk_label.setStyleSheet(f'color: {SUBTLE}; font-size: 10pt;')
        self.disk_label.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        footer.addWidget(self.disk_label)

        self.disk_bar = QProgressBar()
        self.disk_bar.setFixedWidth(180)
        self.disk_bar.setFixedHeight(6)
        self.disk_bar.setTextVisible(False)
        self.disk_bar.setRange(0, 100)
        footer.addWidget(self.disk_bar)

        self._update_disk()
        self._disk_timer = QTimer(self)
        self._disk_timer.timeout.connect(self._update_disk)
        self._disk_timer.start(5000)
        return footer

    # ── Navigation ──────────────────────────────────────────────────────────

    def _on_nav_clicked(self, idx: int):
        if self._scanning and idx != 1:
            for i, b in enumerate(self.nav_buttons):
                b.setChecked(i == 1)
            return
        self._switch_page(idx)

    def _switch_page(self, idx: int):
        for i, b in enumerate(self.nav_buttons):
            b.setChecked(i == idx)
        self.stack.setCurrentIndex(idx)
        # let the page refresh itself if it wants
        page = self.stack.currentWidget()
        if hasattr(page, 'on_show'):
            try:
                page.on_show()
            except Exception:
                pass

    def _on_scan_state_changed(self, scanning: bool):
        self._scanning = scanning
        for i, b in enumerate(self.nav_buttons):
            if i != 1:
                b.setEnabled(not scanning)
        if scanning:
            self.scan_indicator.setText('● SCANNING')
            self.scan_indicator.setStyleSheet(
                f'color: {SUCCESS}; font-size: 10pt; font-weight: bold;')
        else:
            self.scan_indicator.setText('')

    def jump_to(self, page: str):
        idx = {'home': 0, 'scan': 1, 'runs': 2, 'transfer': 3, 'menu': 4}.get(page, 0)
        self._switch_page(idx)

    # ── Orphan-scan recovery (once at startup) ──────────────────────────────

    def _offer_orphan_recovery(self):
        from gui.scan_list import offer_orphan_recovery
        dumps_root = os.path.join(config.DUMP_PATH, 'dumps')
        if offer_orphan_recovery(self, dumps_root):
            for page_attr in ('home_page', 'runs_page', 'xfer_page'):
                p = getattr(self, page_attr, None)
                if p is not None:
                    for refresh_attr in ('_refresh_all', '_reload', 'refresh'):
                        if hasattr(p, refresh_attr):
                            try:
                                getattr(p, refresh_attr)()
                            except Exception:
                                pass
                            break

    # ── Footer ticks ────────────────────────────────────────────────────────

    def _tick_clock(self):
        self.clock_label.setText(
            datetime.datetime.now().strftime('%A, %d %b %Y    %H:%M:%S'))

    _DISK_WARN_GB = 20.0

    def _update_disk(self):
        try:
            usage = shutil.disk_usage(config.DUMP_PATH)
        except OSError:
            self.disk_label.setText('Disk: —')
            self.disk_bar.setValue(0)
            return

        total_gb = usage.total / 1024 ** 3
        free_gb  = usage.free  / 1024 ** 3
        used_pct = int(usage.used / usage.total * 100) if usage.total else 0

        if free_gb < config.MIN_DISK_GB:
            chunk = DANGER
        elif free_gb < self._DISK_WARN_GB:
            chunk = WARNING
        else:
            chunk = SUCCESS

        self.disk_label.setText(
            f'<span style="color:{chunk};">Primary <b>{free_gb:.1f} GB</b> free</span>'
            f'<span style="color:{SUBTLE};">  ·  Total {total_gb:.1f} GB</span>'
        )
        self.disk_label.setTextFormat(Qt.TextFormat.RichText)

        self.disk_bar.setStyleSheet(
            'QProgressBar { border: none; border-radius: 3px; '
            f'background-color: {BORDER}; }} '
            f'QProgressBar::chunk {{ background-color: {chunk}; '
            f'border-radius: 3px; }}'
        )
        self.disk_bar.setValue(used_pct)

    # ── Close guard ─────────────────────────────────────────────────────────

    def closeEvent(self, event):
        if self._scanning:
            event.ignore()
            self.scan_page.request_close_with_confirm()
        else:
            self._clock_timer.stop()
            self._disk_timer.stop()
            from core.ros_controller import RosController
            RosController.shutdown_roscore()
            event.accept()
