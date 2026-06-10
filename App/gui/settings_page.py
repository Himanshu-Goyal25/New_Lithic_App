"""Settings / Menu page — Storage + Appearance + Supervisor cards."""

import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QPushButton, QLineEdit, QFileDialog, QScrollArea, QSizePolicy,
)
from PySide6.QtCore import Qt, QTimer

import config
from gui.main_window import (
    BG, PRIMARY, PRIMARY_DARK, ACCENT, ACCENT_DARK, DANGER, TEXT, LABEL,
    SUBTLE, BORDER, PANEL_BG, DISABLED_BG, DISABLED_TEXT, WARNING,
    _GradientLabel,
)


class SettingsPage(QWidget):
    def __init__(self, shell):
        super().__init__()
        self._shell = shell
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet('background: transparent; border: 0;')
        outer.addWidget(scroll)

        host = QWidget()
        host.setStyleSheet('background: transparent; border: 0;')
        col = QVBoxLayout(host)
        col.setContentsMargins(28, 24, 28, 24)
        col.setSpacing(14)

        title = _GradientLabel('MENU', PRIMARY, PRIMARY_DARK)
        title.setStyleSheet('font-size: 22pt; font-weight: bold;')
        col.addWidget(title)

        col.addWidget(self._storage_card())
        col.addWidget(self._appearance_card())
        col.addWidget(self._supervisor_card())
        col.addStretch()

        scroll.setWidget(host)
        from gui.scan_list import _enable_touch_scroll
        _enable_touch_scroll(scroll)

    # ── Card scaffolding ────────────────────────────────────────────────────

    def _wrap(self, title: str):
        card = QFrame()
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        card.setStyleSheet(
            f'QFrame {{ background-color: {PANEL_BG}; border: 1px solid {BORDER};'
            f'  border-radius: 12px; }}'
        )
        col = QVBoxLayout(card)
        col.setContentsMargins(18, 14, 18, 14)
        col.setSpacing(10)

        hdr = QLabel(title)
        hdr.setStyleSheet(
            f'color: {SUBTLE}; font-size: 10pt; font-weight: bold; border: none;')
        col.addWidget(hdr)
        return card, col

    # ── Storage ─────────────────────────────────────────────────────────────

    def _storage_card(self) -> QFrame:
        card, col = self._wrap('STORAGE')

        lbl = QLabel('Recording Location')
        lbl.setStyleSheet(
            f'color: {TEXT}; font-size: 12pt; font-weight: bold; border: none;')
        col.addWidget(lbl)

        path_row_widget = QWidget()
        path_row_widget.setStyleSheet('background: transparent; border: none;')
        path_row = QHBoxLayout(path_row_widget)
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(8)

        self._path_edit = QLineEdit(config.DUMP_PATH)
        self._path_edit.setMinimumHeight(38)
        self._path_edit.setStyleSheet(
            f'QLineEdit {{ background-color: {BG}; color: {TEXT};'
            f'  border: 1px solid {BORDER}; border-radius: 6px; padding-left: 8px;'
            f'  font-size: 11pt; }}'
        )
        path_row.addWidget(self._path_edit, stretch=1)

        browse = QPushButton('Browse')
        browse.setFixedSize(96, 38)
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.setStyleSheet(self._primary_btn_style())
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)

        col.addWidget(path_row_widget)

        set_default = QPushButton('Set as Default Storage')
        set_default.setFixedHeight(38)
        set_default.setCursor(Qt.CursorShape.PointingHandCursor)
        set_default.setStyleSheet(self._primary_btn_style(accent=True))
        set_default.clicked.connect(self._set_default)
        col.addWidget(set_default)

        return card

    # ── Appearance ──────────────────────────────────────────────────────────

    def _appearance_card(self) -> QFrame:
        from gui.theme import THEME_NAME
        card, col = self._wrap('APPEARANCE')

        row_widget = QWidget()
        row_widget.setStyleSheet('background: transparent; border: none;')
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        self._light_btn = QPushButton('Light')
        self._dark_btn  = QPushButton('Dark')
        for b in (self._light_btn, self._dark_btn):
            b.setFixedHeight(40)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(b, stretch=1)
        self._light_btn.setChecked(THEME_NAME == 'light')
        self._dark_btn.setChecked(THEME_NAME == 'dark')
        self._apply_theme_btn_styles()

        self._light_btn.clicked.connect(lambda: self._pick_theme('light'))
        self._dark_btn.clicked.connect(lambda: self._pick_theme('dark'))

        col.addWidget(row_widget)
        return card

    # ── Supervisor ──────────────────────────────────────────────────────────

    def _supervisor_card(self) -> QFrame:
        from gui import supervisor

        card, col = self._wrap('SUPERVISOR')
        self._sv_card_ref = card

        if supervisor.is_default_pin():
            warn = QLabel(
                '⚠   Default PIN (0000) is still in use. '
                'Change it via "Change Supervisor PIN".')
            warn.setWordWrap(True)
            warn.setStyleSheet(
                f'color: {WARNING}; font-size: 10pt; font-weight: bold;'
                f'background: transparent; border: 0;')
            col.addWidget(warn)

        grid = QGridLayout()
        grid.setSpacing(8)

        actions = [
            ('🔒  Change PIN',                self._sv_change_pin),
            (f'🏷  Device ID: {config.DEVICE}', self._sv_device_id),
            (f'🧪  DEV_MODE: {"ON" if config.DEV_MODE else "OFF"}',
                                              self._sv_dev_mode),
            ('📍  Manage Sites',               self._sv_manage_sites),
            ('👤  Manage Scan Incharges',      self._sv_manage_incharges),
            ('📜  View Action Log',            self._sv_action_log),
        ]
        for i, (label, handler) in enumerate(actions):
            btn = QPushButton(label)
            btn.setMinimumHeight(44)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f'QPushButton {{ background: {BG}; color: {TEXT};'
                f'  border: 1px solid {BORDER}; border-radius: 8px;'
                f'  padding: 0 12px; font-size: 11pt;'
                f'  font-weight: bold; text-align: left; }} '
                f'QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }} '
                f'QPushButton:pressed {{ background: {PANEL_BG}; }}')
            btn.clicked.connect(handler)
            grid.addWidget(btn, i // 2, i % 2)

        col.addLayout(grid)
        return card

    # ── Supervisor-gated handlers ──────────────────────────────────────────

    def _sv_change_pin(self):
        from gui.supervisor_tools import open_change_pin
        open_change_pin(self)
        self._rebuild_supervisor_card()

    def _sv_device_id(self):
        from gui import supervisor
        from gui.supervisor_tools import open_device_id
        if not supervisor.ensure_unlocked(self):
            return
        open_device_id(self)

    def _sv_dev_mode(self):
        from gui import supervisor
        from gui.supervisor_tools import open_dev_mode_toggle
        if not supervisor.ensure_unlocked(self):
            return
        open_dev_mode_toggle(self)

    def _sv_manage_sites(self):
        from gui import supervisor
        from gui.supervisor_tools import open_manage_sites
        if not supervisor.ensure_unlocked(self):
            return
        open_manage_sites(self)

    def _sv_manage_incharges(self):
        from gui import supervisor
        from gui.supervisor_tools import open_manage_incharges
        if not supervisor.ensure_unlocked(self):
            return
        open_manage_incharges(self)

    def _sv_action_log(self):
        from gui import supervisor
        from gui.supervisor_tools import open_action_log
        if not supervisor.ensure_unlocked(self):
            return
        open_action_log(self)

    def _rebuild_supervisor_card(self):
        host = self.findChild(QScrollArea).widget()
        host_col = host.layout()
        for i in range(host_col.count()):
            w = host_col.itemAt(i).widget()
            if w is getattr(self, '_sv_card_ref', None):
                host_col.removeWidget(w)
                w.deleteLater()
                new_card = self._supervisor_card()
                host_col.insertWidget(i, new_card)
                self._sv_card_ref = new_card
                return

    # ── Handlers ────────────────────────────────────────────────────────────

    def _browse(self):
        path = QFileDialog.getExistingDirectory(
            self, 'Select Recording Location', self._path_edit.text())
        if path:
            self._path_edit.setText(path)

    def _set_default(self):
        from gui.scan_list import _show_result
        path = self._path_edit.text().strip()
        if not path or not os.path.isdir(path):
            _show_result(self, 'Invalid Path',
                         'Please select a valid existing directory.',
                         kind='error')
            return
        config.DUMP_PATH = path
        _show_result(
            self, 'Storage Updated',
            f'Recording location set to:\n{path}\n\n'
            f'(Runtime only — update config.py to persist.)')

    def _pick_theme(self, theme: str):
        from gui.theme import THEME_NAME, save_theme_name
        from gui.scan_list import _ConfirmDialog, _show_result

        self._light_btn.setChecked(theme == 'light')
        self._dark_btn.setChecked(theme == 'dark')
        self._apply_theme_btn_styles()

        if theme == THEME_NAME:
            return

        if getattr(self._shell, '_scanning', False):
            _show_result(
                self, 'Theme',
                'A scan is currently running. Stop the scan before changing '
                'the theme.', kind='error')
            self._light_btn.setChecked(THEME_NAME == 'light')
            self._dark_btn.setChecked(THEME_NAME == 'dark')
            self._apply_theme_btn_styles()
            return

        if not save_theme_name(theme):
            _show_result(
                self, 'Theme',
                'Could not save the theme preference (disk error).',
                kind='error')
            self._light_btn.setChecked(THEME_NAME == 'light')
            self._dark_btn.setChecked(THEME_NAME == 'dark')
            self._apply_theme_btn_styles()
            return

        confirm = _ConfirmDialog(
            self, f'Switch to {theme.title()} Theme',
            f'The app will restart to apply the {theme} theme.\n\n'
            f'Continue?',
            yes_label='Apply', no_label='Cancel')
        if confirm.exec() == confirm.DialogCode.Accepted:
            QTimer.singleShot(0, self._restart_in_place)
        else:
            self._light_btn.setChecked(THEME_NAME == 'light')
            self._dark_btn.setChecked(THEME_NAME == 'dark')
            self._apply_theme_btn_styles()

    @staticmethod
    def _restart_in_place():
        import subprocess
        import sys
        from core.ros_controller import RosController

        try:
            RosController.shutdown_roscore()
        except Exception as e:
            print(f'[theme-restart] shutdown_roscore failed: {e}',
                  file=sys.stderr)

        # Close the QLocalServer so the new instance's guard can re-listen.
        from PySide6.QtWidgets import QApplication
        from PySide6.QtNetwork import QLocalServer
        app = QApplication.instance()
        guard = getattr(app, '_singleton_guard', None)
        if guard is not None and getattr(guard, '_server', None) is not None:
            try:
                guard._server.close()
            except Exception:
                pass
            try:
                QLocalServer.removeServer(guard.KEY)
            except Exception:
                pass

        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except OSError as e:
            print(f'[theme-restart] execv failed: {e}', file=sys.stderr)
            os._exit(1)

    def _apply_theme_btn_styles(self):
        for b in (self._light_btn, self._dark_btn):
            if b.isChecked():
                b.setStyleSheet(
                    f'QPushButton {{ background-color: {PRIMARY}; color: white;'
                    f'  border: none; border-radius: 20px;'
                    f'  font-size: 12pt; font-weight: bold; }}'
                )
            else:
                b.setStyleSheet(
                    f'QPushButton {{ background-color: {BG}; color: {LABEL};'
                    f'  border: 1px solid {BORDER}; border-radius: 20px;'
                    f'  font-size: 12pt; font-weight: bold; }} '
                    f'QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }}'
                )

    @staticmethod
    def _primary_btn_style(accent: bool = False) -> str:
        bg = ACCENT if accent else PRIMARY
        hover = ACCENT_DARK if accent else PRIMARY_DARK
        return (
            f'QPushButton {{ background-color: {bg}; color: white;'
            f'  border: none; border-radius: 8px; padding: 0 14px;'
            f'  font-size: 11pt; font-weight: bold; }} '
            f'QPushButton:hover {{ background-color: {hover}; }} '
            f'QPushButton:disabled {{ background-color: {DISABLED_BG}; color: {DISABLED_TEXT}; }}'
        )

    def on_show(self):
        pass
