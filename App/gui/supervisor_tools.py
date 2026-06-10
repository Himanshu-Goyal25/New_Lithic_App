"""Admin dialogs launched from Settings → Supervisor.

USB-reset action from the reference is omitted — this device has no UVC
camera. Everything else (change-PIN / device-id / dev-mode / manage CSVs /
view action log) is preserved.
"""

import csv
import os

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QListWidget, QScrollArea, QWidget, QFrame, QSizePolicy, QListWidgetItem,
)
from PySide6.QtCore import Qt

import config
from core.audit import log_action, read_actions, log_path
from gui.main_window import (
    BG, PRIMARY, PRIMARY_DARK, DANGER, DANGER_DARK, ACCENT, ACCENT_DARK,
    TEXT, LABEL, SUBTLE, BORDER, PANEL_BG,
)
from gui.scan_list import (
    _make_themed_card, _center_on_parent, _show_result, _ConfirmDialog,
    _NameInputDialog, _enable_touch_scroll,
)
from gui import supervisor


# ── Change PIN ──────────────────────────────────────────────────────────────

class _ChangePinDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self.setFixedSize(400, 260)

        layout = _make_themed_card(self, PRIMARY)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title = QLabel('Change Supervisor PIN')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f'color: {PRIMARY}; font-size: 13pt; font-weight: bold; border: none;')
        layout.addWidget(title)

        def _pin_field(placeholder):
            e = QLineEdit()
            e.setEchoMode(QLineEdit.EchoMode.Password)
            e.setPlaceholderText(placeholder)
            e.setMinimumHeight(36)
            e.setStyleSheet(
                f'QLineEdit {{ background: {BG}; color: {TEXT};'
                f'  border: 1px solid {BORDER}; border-radius: 6px;'
                f'  padding: 0 10px; font-size: 12pt; }} '
                f'QLineEdit:focus {{ border: 2px solid {PRIMARY}; }}')
            return e

        self._current = _pin_field('Current PIN')
        self._new     = _pin_field('New PIN (4+ digits)')
        self._confirm = _pin_field('Confirm new PIN')
        for e in (self._current, self._new, self._confirm):
            layout.addWidget(e)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()

        cancel = QPushButton('Cancel')
        cancel.setMinimumHeight(34)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {LABEL};'
            f'  border: 1px solid {BORDER}; border-radius: 6px;'
            f'  padding: 0 18px; font-size: 11pt; }} '
            f'QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }}')
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)

        save = QPushButton('Save')
        save.setMinimumHeight(34)
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.setStyleSheet(
            f'QPushButton {{ background: {PRIMARY}; color: white; border: none;'
            f'  border-radius: 6px; padding: 0 18px; font-size: 11pt;'
            f'  font-weight: bold; }} '
            f'QPushButton:hover {{ background: {PRIMARY_DARK}; }}')
        save.clicked.connect(self._on_save)
        btn_row.addWidget(save)
        layout.addLayout(btn_row)

    def showEvent(self, event):
        super().showEvent(event)
        _center_on_parent(self)
        self._current.setFocus()

    def _on_save(self):
        if self._new.text() != self._confirm.text():
            _show_result(self, 'PIN Mismatch',
                         'The new PIN and confirmation do not match.',
                         kind='error')
            return
        ok, reason = supervisor.set_pin(self._current.text(), self._new.text())
        if not ok:
            _show_result(self, 'PIN Not Changed', reason, kind='error')
            return
        log_action('pin_changed')
        _show_result(self, 'PIN Changed', 'Supervisor PIN has been updated.')
        self.accept()


# ── Device Identity ─────────────────────────────────────────────────────────

def open_device_id(parent):
    dlg = _NameInputDialog(
        parent, f'Device ID (currently "{config.DEVICE}")',
        initial=config.DEVICE, ok_label='Save')
    if dlg.exec() != dlg.DialogCode.Accepted:
        return
    new_id = dlg.name
    if not new_id or new_id == config.DEVICE:
        return
    if any(c in new_id for c in '/\\:*?"<>| \t\n\r'):
        _show_result(parent, 'Invalid Device ID',
                     'Device ID cannot contain whitespace or path characters.',
                     kind='error')
        return
    old_id = config.DEVICE
    if not config.patch_config_py('device_id', new_id):
        _show_result(parent, 'Device ID',
                     'Could not write the new Device ID to config.py (disk error).',
                     kind='error')
        return
    log_action('device_id_changed', old=old_id, new=new_id)
    _show_result(
        parent, 'Device ID Saved',
        f'Device ID set to "{new_id}".\n\n'
        f'Restart the app for all screens to pick up the change.')


# ── DEV_MODE toggle ─────────────────────────────────────────────────────────

def open_dev_mode_toggle(parent):
    new_value = not config.DEV_MODE
    msg = (
        f'DEV_MODE is currently {"ON" if config.DEV_MODE else "OFF"}.\n\n'
        f'Turn it {"OFF" if config.DEV_MODE else "ON"}?\n\n'
    )
    if new_value:
        msg += ('With DEV_MODE ON, scans produce mock data only — no real '
                'ROS drivers or sensors are launched.')
    else:
        msg += ('With DEV_MODE OFF, the app requires ROS + all hardware '
                'connected. Use for production scans.')
    msg += '\n\nApp restart required.'
    confirm = _ConfirmDialog(
        parent, 'Toggle DEV_MODE', msg,
        kind='error' if new_value else 'info',
        yes_label='Confirm', no_label='Cancel')
    if confirm.exec() != confirm.DialogCode.Accepted:
        return
    if not config.patch_config_py('dev_mode', new_value):
        _show_result(parent, 'DEV_MODE',
                     'Could not write the setting to config.py (disk error).',
                     kind='error')
        return
    log_action('dev_mode_toggled', new_value=new_value)
    _show_result(parent, 'DEV_MODE Saved',
                 f'DEV_MODE set to {"ON" if new_value else "OFF"}. '
                 f'Restart the app to apply.')


# ── Manage CSV lists ────────────────────────────────────────────────────────

class _ManageCsvDialog(QDialog):
    def __init__(self, parent, *, title: str, csv_path: str, column: str,
                 audit_tag: str):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self._csv_path = csv_path
        self._column = column
        self._audit_tag = audit_tag
        self.setFixedSize(520, 480)

        layout = _make_themed_card(self, PRIMARY)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        hdr = QLabel(title)
        hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr.setStyleSheet(
            f'color: {PRIMARY}; font-size: 13pt; font-weight: bold; border: none;')
        layout.addWidget(hdr)

        self._list = QListWidget()
        self._list.setStyleSheet(
            f'QListWidget {{ background: {PANEL_BG};'
            f'  border: 1px solid {BORDER}; border-radius: 6px;'
            f'  padding: 4px; font-size: 11pt; color: {TEXT}; }} '
            f'QListWidget::item {{ padding: 8px 10px; border-radius: 4px; }} '
            f'QListWidget::item:selected {{ background: {PRIMARY}; color: white; }}')
        layout.addWidget(self._list, stretch=1)
        self._reload()

        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        add_btn = QPushButton('+ Add')
        add_btn.setMinimumHeight(36)
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(
            f'QPushButton {{ background: {ACCENT}; color: white; border: none;'
            f'  border-radius: 6px; padding: 0 16px; font-size: 11pt;'
            f'  font-weight: bold; }} '
            f'QPushButton:hover {{ background: {ACCENT_DARK}; }}')
        add_btn.clicked.connect(self._on_add)
        action_row.addWidget(add_btn)

        del_btn = QPushButton('🗑  Delete Selected')
        del_btn.setMinimumHeight(36)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setStyleSheet(
            f'QPushButton {{ background: {DANGER}; color: white; border: none;'
            f'  border-radius: 6px; padding: 0 16px; font-size: 11pt;'
            f'  font-weight: bold; }} '
            f'QPushButton:hover {{ background: {DANGER_DARK}; }}')
        del_btn.clicked.connect(self._on_delete)
        action_row.addWidget(del_btn)

        action_row.addStretch()

        close = QPushButton('Done')
        close.setMinimumHeight(36)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {LABEL};'
            f'  border: 1px solid {BORDER}; border-radius: 6px;'
            f'  padding: 0 18px; font-size: 11pt; font-weight: bold; }} '
            f'QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }}')
        close.clicked.connect(self.accept)
        action_row.addWidget(close)

        layout.addLayout(action_row)

    def showEvent(self, event):
        super().showEvent(event)
        _center_on_parent(self)

    def _read_values(self) -> list:
        if not os.path.exists(self._csv_path):
            return []
        try:
            with open(self._csv_path, newline='') as f:
                reader = csv.DictReader(f)
                return sorted({
                    row[self._column] for row in reader
                    if row.get(self._column)
                })
        except (OSError, ValueError):
            return []

    def _write_values(self, values: list):
        os.makedirs(os.path.dirname(self._csv_path), exist_ok=True)
        tmp = self._csv_path + '.tmp'
        try:
            with open(tmp, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([self._column])
                for v in values:
                    writer.writerow([v])
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._csv_path)
        except OSError as e:
            _show_result(self, 'Save Failed', str(e), kind='error')

    def _reload(self):
        self._list.clear()
        for v in self._read_values():
            QListWidgetItem(v, self._list)

    def _on_add(self):
        dlg = _NameInputDialog(self, 'New entry', ok_label='Add')
        if dlg.exec() != dlg.DialogCode.Accepted or not dlg.name:
            return
        values = self._read_values()
        if dlg.name.lower() in (v.lower() for v in values):
            _show_result(self, 'Already Exists',
                         f'"{dlg.name}" is already in the list.', kind='error')
            return
        values = sorted(values + [dlg.name])
        self._write_values(values)
        log_action(f'{self._audit_tag}_added', value=dlg.name)
        self._reload()

    def _on_delete(self):
        item = self._list.currentItem()
        if not item:
            return
        value = item.text()
        confirm = _ConfirmDialog(
            self, 'Delete Entry',
            f'Remove "{value}" from the list?\n\n'
            f'Scans already recorded with this name are unaffected.',
            kind='error', yes_label='Delete', no_label='Cancel')
        if confirm.exec() != confirm.DialogCode.Accepted:
            return
        values = [v for v in self._read_values() if v != value]
        self._write_values(values)
        log_action(f'{self._audit_tag}_removed', value=value)
        self._reload()


def open_manage_sites(parent):
    _ManageCsvDialog(parent, title='Manage Sites',
                     csv_path=config.SITES_CSV, column='site',
                     audit_tag='site').exec()


def open_manage_incharges(parent):
    _ManageCsvDialog(parent, title='Manage Scan Incharges',
                     csv_path=config.INCHARGE_CSV, column='name',
                     audit_tag='incharge').exec()


# ── Action Log viewer ───────────────────────────────────────────────────────

class _ActionLogDialog(QDialog):
    def __init__(self, parent=None, limit: int = 200):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self.setFixedSize(720, 500)

        layout = _make_themed_card(self, PRIMARY)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        title = QLabel('Action Log')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f'color: {PRIMARY}; font-size: 13pt; font-weight: bold; border: none;')
        layout.addWidget(title)

        entries = read_actions(limit)
        if not entries:
            empty = QLabel('No actions recorded yet.')
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(
                f'color: {SUBTLE}; font-size: 11pt; padding: 40px; border: 0;')
            layout.addWidget(empty, stretch=1)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet('background: transparent; border: 0;')
            host = QWidget()
            host.setStyleSheet(f'background: {BG}; border: 0;')
            col = QVBoxLayout(host)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(6)
            for e in entries:
                col.addWidget(self._row(e))
            col.addStretch()
            scroll.setWidget(host)
            layout.addWidget(scroll, stretch=1)
            _enable_touch_scroll(scroll)

        footer = QHBoxLayout()
        footer.addStretch()
        path_lbl = QLabel(f'Full log: {log_path()}')
        path_lbl.setStyleSheet(
            f'color: {SUBTLE}; font-size: 9pt; border: 0;')
        footer.addWidget(path_lbl)
        footer.addStretch()

        close = QPushButton('Close')
        close.setMinimumHeight(34)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(
            f'QPushButton {{ background: {PRIMARY}; color: white; border: none;'
            f'  border-radius: 6px; padding: 0 22px; font-size: 11pt;'
            f'  font-weight: bold; }} '
            f'QPushButton:hover {{ background: {PRIMARY_DARK}; }}')
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)

    def showEvent(self, event):
        super().showEvent(event)
        _center_on_parent(self)

    def _row(self, entry: dict) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            f'QFrame {{ background-color: {PANEL_BG};'
            f'  border: 1px solid {BORDER}; border-radius: 6px; }} '
            f'QLabel {{ background: transparent; border: 0; }}')
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)

        action = entry.get('action', '?')
        ts = (entry.get('ts') or '').replace('T', ' ')
        header = QLabel(f'<b>{action}</b>  ·  <span style="color:{SUBTLE}">{ts}</span>')
        header.setTextFormat(Qt.TextFormat.RichText)
        header.setStyleSheet(f'color: {TEXT}; font-size: 11pt;')
        lay.addWidget(header)

        details = entry.get('details') or {}
        if details:
            detail_lbl = QLabel(
                ' · '.join(f'{k}: {v}' for k, v in details.items()))
            detail_lbl.setStyleSheet(f'color: {LABEL}; font-size: 10pt;')
            detail_lbl.setWordWrap(True)
            lay.addWidget(detail_lbl)
        return card


def open_action_log(parent):
    _ActionLogDialog(parent).exec()


def open_change_pin(parent):
    _ChangePinDialog(parent).exec()
