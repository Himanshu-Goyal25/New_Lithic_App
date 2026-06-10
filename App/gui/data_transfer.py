"""Data Transfer page — copy scans to external storage or free up disk space."""

import os
import shutil
import subprocess
import threading

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
)
from PySide6.QtCore import Qt, Signal, Slot

import config
from gui.main_window import (
    PRIMARY, PRIMARY_DARK, TEXT, LABEL, SUBTLE, BORDER, PANEL_BG,
    _GradientLabel,
)
from gui.scan_list import (
    ScanListWidget, copy_scans_with_dialog, delete_scans_with_confirm,
    primary_btn_style, _show_result,
)
from gui.scan_stats import (
    find_external_mount, format_size, ensure_external_drives_mounted,
)


class DataTransferPage(QWidget):
    _eject_done = Signal(str, bool, str)   # mount_path, ok, error_message

    def __init__(self, shell):
        super().__init__()
        self._shell = shell
        self._ejecting = False
        self._eject_done.connect(self._on_eject_done, Qt.ConnectionType.QueuedConnection)
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 16)
        outer.setSpacing(12)

        title = _GradientLabel('DATA TRANSFER', PRIMARY, PRIMARY_DARK)
        title.setStyleSheet('font-size: 22pt; font-weight: bold;')
        outer.addWidget(title)

        self._ext_card = self._build_ext_card()
        outer.addWidget(self._ext_card)

        # Action row
        head = QHBoxLayout()
        head.setSpacing(10)

        self._sel_lbl = QLabel('—')
        self._sel_lbl.setStyleSheet(
            f'color: {LABEL}; font-size: 11pt; font-weight: bold;')
        head.addWidget(self._sel_lbl)
        head.addStretch()

        self._copy_btn = QPushButton('→  Copy to External')
        self._copy_btn.setMinimumHeight(34)
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.setStyleSheet(primary_btn_style(accent=True))
        self._copy_btn.clicked.connect(self._copy_selected)
        head.addWidget(self._copy_btn)

        self._free_btn = QPushButton('🗑  Delete')
        self._free_btn.setMinimumHeight(34)
        self._free_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._free_btn.setStyleSheet(primary_btn_style(danger=True))
        self._free_btn.clicked.connect(self._free_up_space)
        head.addWidget(self._free_btn)

        refresh = QPushButton('↻  Refresh')
        refresh.setMinimumHeight(34)
        refresh.setMinimumWidth(110)
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.setStyleSheet(primary_btn_style())
        refresh.clicked.connect(self._reload)
        head.addWidget(refresh)
        outer.addLayout(head)

        # Meta row
        meta_row = QHBoxLayout()
        meta_row.setSpacing(12)
        self._total_lbl = QLabel('—')
        self._total_lbl.setStyleSheet(f'color: {SUBTLE}; font-size: 11pt;')
        meta_row.addWidget(self._total_lbl)
        meta_row.addStretch()

        self._select_all_btn = QPushButton('Select All')
        self._select_all_btn.setMinimumHeight(28)
        self._select_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._select_all_btn.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {PRIMARY};'
            f'  border: 1px solid {BORDER}; border-radius: 6px; padding: 0 12px;'
            f'  font-size: 10pt; font-weight: bold; }} '
            f'QPushButton:hover {{ border-color: {PRIMARY}; }}'
        )
        self._select_all_btn.clicked.connect(self._toggle_select_all)
        meta_row.addWidget(self._select_all_btn)
        outer.addLayout(meta_row)

        # Scan list
        self._list = ScanListWidget(os.path.join(config.DUMP_PATH, 'dumps'))
        self._list.selection_changed.connect(self._on_selection_changed)
        outer.addWidget(self._list, stretch=1)

        self._reload()

    def showEvent(self, event):
        super().showEvent(event)
        self._reload()

    def on_show(self):
        self._reload()

    # ── External-storage card ───────────────────────────────────────────────

    def _build_ext_card(self) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            f'QFrame {{ background-color: {PANEL_BG}; border: 1px solid {BORDER};'
            f'  border-radius: 10px; }} '
            f'QLabel {{ background: transparent; border: 0; }}'
        )
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        icon = QLabel('💾')
        icon.setStyleSheet('font-size: 22pt;')
        layout.addWidget(icon)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        title = QLabel('EXTERNAL STORAGE')
        title.setStyleSheet(
            f'color: {SUBTLE}; font-size: 10pt; font-weight: bold;')
        text_col.addWidget(title)

        self._ext_status = QLabel('Detecting…')
        self._ext_status.setStyleSheet(
            f'color: {TEXT}; font-size: 12pt; font-weight: bold;')
        text_col.addWidget(self._ext_status)

        self._ext_detail = QLabel('')
        self._ext_detail.setStyleSheet(f'color: {LABEL}; font-size: 11pt;')
        text_col.addWidget(self._ext_detail)
        layout.addLayout(text_col, stretch=1)

        self._eject_btn = QPushButton('⏏  Eject')
        self._eject_btn.setMinimumHeight(34)
        self._eject_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._eject_btn.setStyleSheet(primary_btn_style())
        self._eject_btn.clicked.connect(self._eject_external)
        self._eject_btn.setVisible(False)
        layout.addWidget(self._eject_btn)

        rescan = QPushButton('Detect')
        rescan.setMinimumHeight(34)
        rescan.setCursor(Qt.CursorShape.PointingHandCursor)
        rescan.setStyleSheet(primary_btn_style())
        rescan.clicked.connect(self._refresh_ext)
        layout.addWidget(rescan)

        return card

    def _refresh_ext(self):
        # Fallback automount: if pcmanfm/udisks didn't mount a plugged-in
        # USB partition, do it ourselves before we look for mountpoints.
        # No-op when everything is already mounted.
        try:
            ensure_external_drives_mounted()
        except Exception:
            pass

        ext = find_external_mount()
        if not ext:
            self._ext_status.setText('Not detected')
            self._ext_detail.setText('Plug in a USB drive to copy scans.')
            self._copy_btn.setEnabled(False)
            self._eject_btn.setVisible(False)
            return
        self._ext_status.setText(ext)
        try:
            usage = shutil.disk_usage(ext)
            free_gb  = usage.free  / 1024 ** 3
            total_gb = usage.total / 1024 ** 3
            self._ext_detail.setText(
                f'{free_gb:.1f} GB free of {total_gb:.1f} GB')
        except Exception:
            self._ext_detail.setText('External drive ready.')
        self._copy_btn.setEnabled(len(self._list.selected_scans()) > 0)
        self._eject_btn.setVisible(True)
        self._eject_btn.setEnabled(True)
        self._eject_btn.setText('⏏  Eject')

    # ── Eject worker ────────────────────────────────────────────────────────

    def _eject_external(self):
        if self._ejecting:
            return
        ext = find_external_mount()
        if not ext:
            self._refresh_ext()
            return

        self._ejecting = True
        self._eject_btn.setEnabled(False)
        self._eject_btn.setText('Ejecting…')

        threading.Thread(
            target=self._eject_worker, args=(ext,), daemon=True).start()

    def _eject_worker(self, mount_path: str):
        try:
            result = self._do_unmount(mount_path)
        except Exception as e:
            result = {'ok': False, 'error': f'Unexpected: {e}'}
        self._eject_done.emit(
            mount_path, bool(result.get('ok')), str(result.get('error', '')))

    @Slot(str, bool, str)
    def _on_eject_done(self, mount_path: str, ok: bool, error: str):
        self._ejecting = False

        from core.audit import log_action
        if ok:
            log_action('disk_unmounted', mount=mount_path)
            _show_result(
                self, 'Ejected',
                f'{mount_path} unmounted safely.\n'
                'It is now safe to remove the drive.')
        else:
            log_action('disk_unmount_failed', mount=mount_path, error=error)
            _show_result(
                self, 'Eject Failed',
                (error or 'Unknown error.') +
                '\n\nClose any open files on the drive and try again.',
                kind='error')

        self._reload()

    def _do_unmount(self, mount_path: str) -> dict:
        try:
            subprocess.run(['sync'], timeout=30, check=False)

            device = None
            r = subprocess.run(
                ['findmnt', '-n', '-o', 'SOURCE', mount_path],
                timeout=3, capture_output=True, text=True)
            if r.returncode == 0:
                device = r.stdout.strip() or None

            if device:
                r = subprocess.run(
                    ['udisksctl', 'unmount', '-b', device],
                    timeout=30, capture_output=True, text=True)
                if r.returncode == 0:
                    subprocess.run(
                        ['udisksctl', 'power-off', '-b', device],
                        timeout=10, capture_output=True)
                    return {'ok': True}
                udisks_err = (r.stderr.strip() or r.stdout.strip()
                              or 'udisksctl unmount failed')
            else:
                udisks_err = 'block device not found'

            r = subprocess.run(
                ['umount', mount_path],
                timeout=15, capture_output=True, text=True)
            if r.returncode == 0:
                return {'ok': True}
            return {'ok': False,
                    'error': (r.stderr.strip() or udisks_err)}

        except subprocess.TimeoutExpired:
            return {'ok': False,
                    'error': 'Unmount timed out — the drive may still be writing.'}
        except FileNotFoundError as e:
            return {'ok': False,
                    'error': f'Required tool missing: {e.filename}'}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    # ── List interaction ────────────────────────────────────────────────────

    def _reload(self):
        self._list.reload()
        self._refresh_ext()

    def _on_selection_changed(self, selected: int, total: int):
        self._sel_lbl.setText(f'Selected: {selected} / {total}')

        total_size = sum(r.info.get('size_bytes', 0) for r in self._list._rows)
        sel_size   = sum(s.get('size_bytes', 0) for s in self._list.selected_scans())
        if selected:
            self._total_lbl.setText(
                f'{total} scan(s)  ·  total {format_size(total_size)}'
                f'  ·  selected {format_size(sel_size)}'
            )
        else:
            self._total_lbl.setText(
                f'{total} scan(s)  ·  total {format_size(total_size)}'
            )

        ext_ok = find_external_mount() is not None
        self._copy_btn.setEnabled(selected > 0 and ext_ok)
        self._free_btn.setEnabled(selected > 0)

        all_selected = (selected == total and total > 0)
        self._select_all_btn.setText('Deselect All' if all_selected else 'Select All')

    def _toggle_select_all(self):
        if self._select_all_btn.text() == 'Select All':
            self._list.select_all()
        else:
            self._list.deselect_all()

    def _copy_selected(self):
        ext = find_external_mount()
        copy_scans_with_dialog(self._list.selected_scans(), ext, self)

    def _free_up_space(self):
        if delete_scans_with_confirm(self._list.selected_scans(), self):
            self._reload()
