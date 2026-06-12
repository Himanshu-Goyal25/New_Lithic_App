"""Shared scan list + every themed dialog used across the app.

Themed dialogs (replace QMessageBox everywhere):
  - _ConfirmDialog       yes/no with kind = 'info' | 'error'
  - _ResultDialog        single-OK; use _show_result() helper
  - _NameInputDialog     single-line text input
  - _FolderPickerDialog  scoped folder browser with rename / new / delete
  - _CopyDialog          progress dialog for copy worker
  - _RecoveryDialog      orphan-scan recovery on startup

Plus the public helpers:
  - copy_scans_with_dialog
  - delete_scans_with_confirm
  - offer_orphan_recovery
  - primary_btn_style
  - _make_themed_card  (used by every overlay-style dialog)
  - _center_on_parent
  - _enable_touch_scroll
  - _show_result
"""

import csv
import errno
import json
import os
import shutil
import subprocess
import time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QCheckBox, QScrollArea, QScroller, QSizePolicy, QDialog, QProgressBar,
    QLineEdit, QListWidget, QListWidgetItem, QRadioButton, QButtonGroup,
)
from PySide6.QtCore import Qt, Signal, QThread

import config
from gui.main_window import (
    BG, PRIMARY, PRIMARY_DARK, ACCENT, ACCENT_DARK, TEXT, LABEL, SUBTLE,
    BORDER, PANEL_BG, DANGER, DANGER_DARK, DISABLED_BG, DISABLED_TEXT,
)
from gui.scan_stats import list_scans, format_size, format_duration


def _enable_touch_scroll(scroll_area: QScrollArea):
    """Turn finger-drag into a smooth kinetic flick."""
    try:
        QScroller.grabGesture(
            scroll_area.viewport(),
            QScroller.ScrollerGestureType.LeftMouseButtonGesture)
    except Exception:
        pass


# ── Themed dialog helpers ────────────────────────────────────────────────────

def _center_on_parent(dialog):
    p = dialog.parentWidget()
    if p is not None:
        top = p.window().geometry()
        dialog.move(
            top.x() + (top.width() - dialog.width()) // 2,
            top.y() + (top.height() - dialog.height()) // 2)


def _make_themed_card(dialog: QDialog, border_color: str) -> QVBoxLayout:
    """Turn a frameless QDialog into a rounded, themed card."""
    dialog.setModal(True)
    dialog.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    card = QFrame()
    card.setStyleSheet(
        f'QFrame {{ background: {BG}; border: 2px solid {border_color};'
        f'  border-radius: 12px; }} '
        'QLabel { background: transparent; border: 0; }'
    )
    outer.addWidget(card)

    content = QVBoxLayout(card)
    return content


def primary_btn_style(danger: bool = False, accent: bool = False) -> str:
    if danger:
        bg, hover = DANGER, DANGER_DARK
    elif accent:
        bg, hover = ACCENT, ACCENT_DARK
    else:
        bg, hover = PRIMARY, PRIMARY_DARK
    return (
        f'QPushButton {{ background: {bg}; color: white; border: none;'
        f'  border-radius: 6px; padding: 0 16px; font-size: 11pt; font-weight: bold; }}'
        f'QPushButton:hover {{ background: {hover}; }}'
        f'QPushButton:disabled {{ background: {DISABLED_BG}; color: {DISABLED_TEXT}; }}'
    )


# ── Single scan row ──────────────────────────────────────────────────────────

class ScanRow(QFrame):
    selection_changed = Signal()

    def __init__(self, info: dict, parent=None, selectable: bool = True):
        super().__init__(parent)
        self.info = info
        self._selectable = selectable
        self.setStyleSheet(
            f'ScanRow {{ background-color: {PANEL_BG}; border: 1px solid {BORDER};'
            f'  border-radius: 10px; }} '
            f'ScanRow:hover {{ border: 1px solid {ACCENT}; }} '
            f'QLabel {{ background: transparent; border: 0; }}'
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._build_ui()

    def _build_ui(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 10, 14, 10)
        outer.setSpacing(12)

        if self._selectable:
            self.checkbox = QCheckBox()
            self.checkbox.setStyleSheet(
                'QCheckBox::indicator { width: 20px; height: 20px; }')
            self.checkbox.stateChanged.connect(lambda _: self.selection_changed.emit())
            outer.addWidget(self.checkbox, alignment=Qt.AlignmentFlag.AlignVCenter)
        else:
            self.checkbox = None

        left_col = QVBoxLayout()
        left_col.setSpacing(2)

        site = QLabel(self.info.get('site', '—'))
        site.setStyleSheet(
            f'color: {PRIMARY}; font-size: 13pt; font-weight: bold;')
        left_col.addWidget(site)

        ft = self.info.get('floor_type', '')
        floor_label = ft if ft == 'Ground Floor' else f'{ft} {self.info.get("floor_num", "")}'
        meta = QLabel(
            f'{floor_label}'
            f'  ·  {self.info.get("scan_part", "")}'
            f'  ·  {self.info.get("incharge", "")}'
            f'  ·  {self.info.get("device", "")}'
        )
        meta.setStyleSheet(f'color: {LABEL}; font-size: 11pt;')
        left_col.addWidget(meta)

        path = QLabel(self.info.get('scan_folder', ''))
        path.setStyleSheet(f'color: {SUBTLE}; font-size: 10pt;')
        path.setWordWrap(True)
        left_col.addWidget(path)
        outer.addLayout(left_col, stretch=1)

        right_col = QVBoxLayout()
        right_col.setSpacing(2)
        right_col.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)

        size_lbl = QLabel(format_size(self.info.get('size_bytes', 0)))
        size_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        size_lbl.setStyleSheet(
            f'color: {TEXT}; font-size: 13pt; font-weight: bold;')
        right_col.addWidget(size_lbl)

        dur_lbl = QLabel(f'⏱  {format_duration(self.info.get("duration_seconds"))}')
        dur_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        dur_lbl.setStyleSheet(f'color: {LABEL}; font-size: 11pt;')
        right_col.addWidget(dur_lbl)

        when_txt = '—'
        when = self.info.get('stopped_at') or self.info.get('mtime')
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(when)
            when_txt = dt.strftime('%d %b %Y  ·  %H:%M')
        except Exception:
            pass
        ts_lbl = QLabel(when_txt)
        ts_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        ts_lbl.setStyleSheet(f'color: {SUBTLE}; font-size: 11pt;')
        right_col.addWidget(ts_lbl)
        outer.addLayout(right_col)

    def is_selected(self) -> bool:
        return self.checkbox.isChecked() if self.checkbox else False

    def set_selected(self, on: bool):
        if self.checkbox:
            self.checkbox.setChecked(on)

    def mousePressEvent(self, event):
        if self.checkbox and event.button() == Qt.MouseButton.LeftButton:
            self.checkbox.toggle()
        super().mousePressEvent(event)


# ── Scrollable list of ScanRows ──────────────────────────────────────────────

class ScanListWidget(QWidget):
    selection_changed = Signal(int, int)   # (selected_count, total_count)

    def __init__(self, root_dir: str, parent=None, selectable: bool = True):
        super().__init__(parent)
        self._root = root_dir
        self._rows: list = []
        self._selectable = selectable
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f'background: {BG};')

        host = QWidget()
        host.setStyleSheet(f'background: {BG};')
        self._list_layout = QVBoxLayout(host)
        self._list_layout.setSpacing(8)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.addStretch()
        scroll.setWidget(host)
        outer.addWidget(scroll)
        _enable_touch_scroll(scroll)

    def reload(self):
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._rows = []

        scans = list_scans(self._root)
        if not scans:
            empty = QLabel('No scans recorded yet.')
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(
                f'color: {SUBTLE}; font-size: 11pt; padding: 40px; '
                f'background: transparent; border: 0;')
            self._list_layout.insertWidget(0, empty)
            self.selection_changed.emit(0, 0)
            return

        for info in scans:
            row = ScanRow(info, selectable=self._selectable)
            if self._selectable:
                row.selection_changed.connect(self._emit_selection)
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)
            self._rows.append(row)
        self._emit_selection()

    def _emit_selection(self):
        sel = sum(1 for r in self._rows if r.is_selected())
        self.selection_changed.emit(sel, len(self._rows))

    def selected_scans(self) -> list:
        return [r.info for r in self._rows if r.is_selected()]

    def select_all(self):
        for r in self._rows:
            r.set_selected(True)

    def deselect_all(self):
        for r in self._rows:
            r.set_selected(False)


# ── Copy worker + dialog ─────────────────────────────────────────────────────

class _OutOfSpaceError(Exception):
    pass


class _CopyWorker(QThread):
    # Use `object` so Qt's 32-bit C int doesn't truncate >2.1 GB counts.
    progress = Signal(object, object, str)
    done     = Signal(bool, str)

    _CHUNK = 1024 * 1024

    def __init__(self, scans: list, dest: str):
        super().__init__()
        self.scans = scans
        self.dest = dest
        self._cancel = False
        self._bytes_done = 0
        self._total_bytes = max(
            1, sum(int(s.get('size_bytes', 0) or 0) for s in scans))
        self._current_name = ''
        self._skipped = 0
        self._missing_names = []

    def cancel(self):
        self._cancel = True

    def run(self):
        copied = 0
        partial = []   # names where some files were unreadable
        for s in self.scans:
            if self._cancel:
                self.done.emit(False, f'Cancelled after copying {copied} scan(s).')
                return
            src = s.get('scan_folder')
            if not src or not os.path.isdir(src):
                continue
            self._current_name = os.path.basename(src.rstrip('/'))
            target = os.path.join(self.dest, self._current_name)
            if os.path.exists(target):
                target = target + '_copy'
            self._skipped = 0
            self._missing_names = []
            try:
                self._copytree_bytes(src, target)
                if self._cancel:
                    self.done.emit(False, f'Cancelled after copying {copied} scan(s).')
                    return
                # Post-copy verification: walk both trees, diff the sets
                # of relative paths. Catches any file that os.walk
                # silently dropped (broken NTFS dirent in scandir output)
                # AND confirms the atomic rename actually promoted every
                # successful copy. Anything missing is added to _skipped.
                self._verify_against_source(src, target)
                copied += 1
                notes = []
                if self._skipped:
                    notes.append(f'{self._skipped} unreadable')
                if self._missing_names:
                    notes.append(f'{len(self._missing_names)} missing in destination')
                if notes:
                    partial.append(f'{self._current_name} ({", ".join(notes)})')
            except _OutOfSpaceError:
                shutil.rmtree(target, ignore_errors=True)
                self.done.emit(
                    False,
                    f'Ran out of space on the USB while copying '
                    f'"{self._current_name}". Copied {copied} scan(s) before '
                    f'the failure; the incomplete one was removed.')
                return
            except Exception as e:
                self.done.emit(False, f'Copy failed for {self._current_name}: {e}')
                return
        self.progress.emit(self._total_bytes, self._total_bytes, '')

        # Final flush so the OS reports "Copied" only when bytes are
        # actually on the device. `os.sync()` is global, but on this
        # kiosk there's no other heavy I/O, and it lets the operator
        # eject the USB the instant the dialog says "Copied".
        self._current_name = 'flushing to disk…'
        self.progress.emit(self._total_bytes, self._total_bytes,
                           self._current_name)
        try:
            os.sync()
        except Exception:
            pass

        msg = f'Copied {copied} scan(s) to {self.dest}.'
        if partial:
            msg += ('\n\nSome files could not be read (NTFS metadata '
                    'corruption from a prior crash) — they were skipped:\n  '
                    + '\n  '.join(partial[:5]))
        self.done.emit(True, msg)

    def _copytree_bytes(self, src: str, dst: str):
        os.makedirs(dst, exist_ok=True)
        for root, _dirs, files in os.walk(src, onerror=self._on_walk_error):
            if self._cancel:
                return
            rel = os.path.relpath(root, src)
            target_dir = os.path.join(dst, rel) if rel != '.' else dst
            os.makedirs(target_dir, exist_ok=True)
            for fname in files:
                if self._cancel:
                    return
                self._copy_file(
                    os.path.join(root, fname), os.path.join(target_dir, fname))

    def _on_walk_error(self, _err):
        # Broken NTFS dirent in this subtree. Count it and keep walking
        # rather than aborting the whole scan.
        self._skipped += 1

    @staticmethod
    def _rel_files(root: str) -> set:
        """Set of relative file paths under `root`. Ignores walk errors
        (already counted via _on_walk_error during the copy pass) and
        any leftover .part sidecars (a .part means an in-flight write
        we already tried to clean up — not a 'real' file)."""
        out = set()
        for r, _dirs, files in os.walk(root, onerror=lambda _e: None):
            rel_r = os.path.relpath(r, root)
            for f in files:
                if f.endswith('.part'):
                    continue
                out.add(f if rel_r == '.' else os.path.join(rel_r, f))
        return out

    def _verify_against_source(self, src: str, dst: str) -> None:
        """Diff src vs dst file trees AFTER the copy. Any source-side
        path that's absent from the destination is a file we believed
        we'd handled but isn't actually there — record it as missing.

        Cheap (just stat() calls) and catches the entire class of
        failures where os.walk silently drops entries on a broken
        filesystem.
        """
        try:
            src_set = self._rel_files(src)
            dst_set = self._rel_files(dst)
        except OSError:
            return
        missing = sorted(src_set - dst_set)
        if missing:
            self._missing_names.extend(missing)
            self._skipped += len(missing)

    def _copy_file(self, src: str, dst: str):
        # Stream to a sidecar file so an interrupted copy never produces
        # a half-written file with the FINAL name (operator can't visually
        # distinguish corrupt from complete). The promote step at the
        # bottom is an atomic rename — the destination either fully
        # exists or doesn't.
        part = dst + '.part'
        try:
            os.remove(part)        # clean any leftover from a prior aborted run
        except OSError:
            pass

        try:
            with open(src, 'rb') as fsrc, open(part, 'wb') as fdst:
                while True:
                    if self._cancel:
                        os.remove(part) if os.path.exists(part) else None
                        return
                    chunk = fsrc.read(self._CHUNK)
                    if not chunk:
                        break
                    fdst.write(chunk)
                    self._bytes_done += len(chunk)
                    self.progress.emit(
                        self._bytes_done, self._total_bytes, self._current_name)
                # Push the kernel page cache to the device BEFORE close.
                # Without this, close() returns as soon as data is queued
                # in RAM — an early USB eject would truncate the tail.
                fdst.flush()
                os.fsync(fdst.fileno())
        except OSError as e:
            if e.errno == errno.ENOSPC:
                try:
                    os.remove(part)
                except OSError:
                    pass
                raise _OutOfSpaceError() from e
            # Per-file IO error (e.g. broken NTFS dirent on `ntfs3`).
            # Discard the partial output file, count it, and continue.
            try:
                os.remove(part)
            except OSError:
                pass
            self._skipped += 1
            return

        # Size sanity check — catches silent truncation (an `ntfs3` read
        # bug, a flaky USB controller, etc.).
        try:
            if os.path.getsize(src) != os.path.getsize(part):
                os.remove(part)
                self._skipped += 1
                return
        except OSError:
            try:
                os.remove(part)
            except OSError:
                pass
            self._skipped += 1
            return

        # Preserve mtime/perms on the sidecar before the rename.
        try:
            shutil.copystat(src, part, follow_symlinks=False)
        except OSError:
            pass

        # Atomic promote: the destination only ever appears in its
        # complete form. A force-kill mid-copy leaves a `.part` file,
        # which the operator can recognise as incomplete.
        try:
            os.replace(part, dst)
        except OSError:
            try:
                os.remove(part)
            except OSError:
                pass
            self._skipped += 1


class _CopyDialog(QDialog):
    def __init__(self, total_bytes: int, parent=None):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self._total_bytes = max(1, total_bytes)
        self.setFixedWidth(460)

        layout = _make_themed_card(self, PRIMARY)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(12)

        title = QLabel('Copying scans')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f'color: {PRIMARY}; font-size: 13pt; font-weight: bold; border: none;')
        layout.addWidget(title)

        self._label = QLabel('Preparing…')
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setWordWrap(True)
        self._label.setStyleSheet(f'color: {TEXT}; font-size: 11pt; border: none;')
        layout.addWidget(self._label)

        self._size_label = QLabel(f'0 B of {format_size(self._total_bytes)}')
        self._size_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._size_label.setStyleSheet(
            f'color: {SUBTLE}; font-size: 10pt; border: none;')
        layout.addWidget(self._size_label)

        self._bar = QProgressBar()
        self._bar.setRange(0, 10000)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(12)
        self._bar.setStyleSheet(
            f'QProgressBar {{ border: 1px solid {BORDER}; border-radius: 6px;'
            f'  background: {PANEL_BG}; }} '
            f'QProgressBar::chunk {{ background: {PRIMARY}; border-radius: 6px; }}'
        )
        layout.addWidget(self._bar)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._cancel_btn = QPushButton('Cancel')
        self._cancel_btn.setMinimumHeight(36)
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.setStyleSheet(primary_btn_style(danger=True))
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._cancel_cb = None

    def showEvent(self, event):
        super().showEvent(event)
        _center_on_parent(self)

    def set_cancel_callback(self, cb):
        self._cancel_cb = cb

    def update_progress(self, bytes_done, total_bytes, current_name: str):
        pct = int(int(bytes_done) * 10000 / max(1, int(total_bytes)))
        self._bar.setValue(min(pct, 10000))
        self._size_label.setText(
            f'{format_size(int(bytes_done))} of {format_size(int(total_bytes))}')
        if current_name:
            self._label.setText(f'Copying {current_name}')

    def _on_cancel(self):
        if self._cancel_cb:
            self._cancel_cb()
        self._cancel_btn.setEnabled(False)
        self._label.setText('Cancelling…')


# ── Result / Confirm / Input dialogs ─────────────────────────────────────────

class _ResultDialog(QDialog):
    def __init__(self, parent, title: str, message: str, kind: str = 'info'):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self.setFixedWidth(420)
        color = DANGER if kind == 'error' else PRIMARY
        hover = DANGER_DARK if kind == 'error' else PRIMARY_DARK

        layout = _make_themed_card(self, color)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(14)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(
            f'color: {color}; font-size: 13pt; font-weight: bold; border: none;')
        layout.addWidget(title_lbl)

        msg_lbl = QLabel(message)
        msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet(f'color: {TEXT}; font-size: 11pt; border: none;')
        layout.addWidget(msg_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton('OK')
        ok_btn.setMinimumHeight(36)
        ok_btn.setMinimumWidth(100)
        ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ok_btn.setStyleSheet(
            f'QPushButton {{ background: {color}; color: white; border: none;'
            f'  border-radius: 6px; padding: 0 22px; font-size: 11pt;'
            f'  font-weight: bold; }} '
            f'QPushButton:hover {{ background: {hover}; }}'
        )
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def showEvent(self, event):
        super().showEvent(event)
        _center_on_parent(self)


def _show_result(parent, title: str, message: str, kind: str = 'info'):
    _ResultDialog(parent, title, message, kind).exec()


class _ConfirmDialog(QDialog):
    def __init__(self, parent, title: str, message: str,
                 kind: str = 'info',
                 yes_label: str = 'OK', no_label: str = 'Cancel'):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self.setFixedWidth(420)
        color = DANGER if kind == 'error' else PRIMARY
        hover = DANGER_DARK if kind == 'error' else PRIMARY_DARK

        layout = _make_themed_card(self, color)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(14)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(
            f'color: {color}; font-size: 13pt; font-weight: bold; border: none;')
        layout.addWidget(title_lbl)

        msg_lbl = QLabel(message)
        msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet(f'color: {TEXT}; font-size: 11pt; border: none;')
        layout.addWidget(msg_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()

        no = QPushButton(no_label)
        no.setMinimumHeight(36)
        no.setMinimumWidth(100)
        no.setCursor(Qt.CursorShape.PointingHandCursor)
        no.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {LABEL};'
            f'  border: 1px solid {BORDER}; border-radius: 6px; padding: 0 20px;'
            f'  font-size: 11pt; font-weight: bold; }} '
            f'QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }}'
        )
        no.clicked.connect(self.reject)
        btn_row.addWidget(no)

        yes = QPushButton(yes_label)
        yes.setMinimumHeight(36)
        yes.setMinimumWidth(100)
        yes.setCursor(Qt.CursorShape.PointingHandCursor)
        yes.setStyleSheet(
            f'QPushButton {{ background: {color}; color: white; border: none;'
            f'  border-radius: 6px; padding: 0 20px; font-size: 11pt;'
            f'  font-weight: bold; }} '
            f'QPushButton:hover {{ background: {hover}; }}'
        )
        yes.clicked.connect(self.accept)
        btn_row.addWidget(yes)
        layout.addLayout(btn_row)

    def showEvent(self, event):
        super().showEvent(event)
        _center_on_parent(self)


class _NameInputDialog(QDialog):
    def __init__(self, parent, title: str, initial: str = '',
                 ok_label: str = 'OK'):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self.setFixedWidth(420)

        layout = _make_themed_card(self, PRIMARY)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(
            f'color: {PRIMARY}; font-size: 12pt; font-weight: bold; border: none;')
        layout.addWidget(title_lbl)

        self._edit = QLineEdit(initial)
        self._edit.setMinimumHeight(36)
        self._edit.setStyleSheet(
            f'QLineEdit {{ background: {BG}; color: {TEXT};'
            f'  border: 1px solid {BORDER}; border-radius: 6px;'
            f'  padding: 0 10px; font-size: 11pt; }} '
            f'QLineEdit:focus {{ border: 2px solid {PRIMARY}; }}'
        )
        self._edit.returnPressed.connect(self.accept)
        if initial:
            self._edit.selectAll()
        layout.addWidget(self._edit)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()

        cancel = QPushButton('Cancel')
        cancel.setMinimumHeight(34)
        cancel.setMinimumWidth(90)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {LABEL};'
            f'  border: 1px solid {BORDER}; border-radius: 6px; padding: 0 18px;'
            f'  font-size: 11pt; font-weight: bold; }} '
            f'QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }}'
        )
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)

        ok = QPushButton(ok_label)
        ok.setMinimumHeight(34)
        ok.setMinimumWidth(90)
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(
            f'QPushButton {{ background: {PRIMARY}; color: white; border: none;'
            f'  border-radius: 6px; padding: 0 18px; font-size: 11pt;'
            f'  font-weight: bold; }} '
            f'QPushButton:hover {{ background: {PRIMARY_DARK}; }}'
        )
        ok.clicked.connect(self.accept)
        btn_row.addWidget(ok)
        layout.addLayout(btn_row)

    def showEvent(self, event):
        super().showEvent(event)
        _center_on_parent(self)
        self._edit.setFocus()

    @property
    def name(self) -> str:
        return self._edit.text().strip()


# ── Folder picker (USB destination chooser) ──────────────────────────────────

class _FolderPickerDialog(QDialog):
    def __init__(self, parent, root: str):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self._root = os.path.realpath(root)
        self._current = self._root
        self._chosen = None

        self.setFixedSize(720, 460)

        outer = _make_themed_card(self, PRIMARY)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(10)

        title_lbl = QLabel('Choose Destination Folder')
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(
            f'color: {PRIMARY}; font-size: 13pt; font-weight: bold; border: none;')
        outer.addWidget(title_lbl)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self._up_btn = self._action_btn('↑  Up', PRIMARY, PRIMARY_DARK)
        self._up_btn.clicked.connect(self._go_up)
        path_row.addWidget(self._up_btn)

        self._path_lbl = QLabel()
        self._path_lbl.setStyleSheet(
            f'color: {LABEL}; font-size: 11pt; border: 1px solid {BORDER};'
            f'  border-radius: 6px; padding: 6px 10px; background: {PANEL_BG};')
        path_row.addWidget(self._path_lbl, stretch=1)
        outer.addLayout(path_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        new_btn = self._action_btn('+  New Folder', ACCENT, ACCENT_DARK)
        new_btn.clicked.connect(self._new_folder)
        action_row.addWidget(new_btn)

        self._rename_btn = self._action_btn('✎  Rename', PRIMARY, PRIMARY_DARK)
        self._rename_btn.clicked.connect(self._rename_folder)
        action_row.addWidget(self._rename_btn)

        self._delete_btn = self._action_btn('🗑  Delete', DANGER, DANGER_DARK)
        self._delete_btn.clicked.connect(self._delete_folder)
        action_row.addWidget(self._delete_btn)
        action_row.addStretch()
        outer.addLayout(action_row)

        self._list = QListWidget()
        self._list.setStyleSheet(
            f'QListWidget {{ background: {PANEL_BG}; border: 1px solid {BORDER};'
            f'  border-radius: 6px; padding: 4px; font-size: 11pt; color: {TEXT}; }} '
            f'QListWidget::item {{ padding: 8px 10px; border-radius: 4px; }} '
            f'QListWidget::item:selected {{ background: {PRIMARY}; color: white; }}'
        )
        self._list.itemSelectionChanged.connect(self._update_action_enabled)
        self._list.itemDoubleClicked.connect(self._open_folder)
        outer.addWidget(self._list, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton('Cancel')
        cancel.setMinimumHeight(36)
        cancel.setMinimumWidth(100)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {LABEL};'
            f'  border: 1px solid {BORDER}; border-radius: 6px; padding: 0 20px;'
            f'  font-size: 11pt; font-weight: bold; }} '
            f'QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }}'
        )
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)

        select_btn = QPushButton('Copy Here')
        select_btn.setMinimumHeight(36)
        select_btn.setMinimumWidth(120)
        select_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        select_btn.setStyleSheet(
            f'QPushButton {{ background: {PRIMARY}; color: white; border: none;'
            f'  border-radius: 6px; padding: 0 22px; font-size: 11pt;'
            f'  font-weight: bold; }} '
            f'QPushButton:hover {{ background: {PRIMARY_DARK}; }}'
        )
        select_btn.clicked.connect(self._on_select)
        btn_row.addWidget(select_btn)
        outer.addLayout(btn_row)

        self._reload()

    @staticmethod
    def _action_btn(text: str, bg: str, hover: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setMinimumHeight(32)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f'QPushButton {{ background: {bg}; color: white; border: none;'
            f'  border-radius: 6px; padding: 0 14px; font-size: 10pt;'
            f'  font-weight: bold; }} '
            f'QPushButton:hover {{ background: {hover}; }} '
            f'QPushButton:disabled {{ background: {DISABLED_BG}; color: {DISABLED_TEXT}; }}'
        )
        return btn

    def showEvent(self, event):
        super().showEvent(event)
        _center_on_parent(self)

    @property
    def chosen_path(self):
        return self._chosen

    def _reload(self):
        self._path_lbl.setText(self._current)
        self._up_btn.setEnabled(
            os.path.realpath(self._current) != self._root)
        self._list.clear()
        try:
            entries = sorted(
                e for e in os.listdir(self._current)
                if os.path.isdir(os.path.join(self._current, e))
            )
        except (PermissionError, OSError):
            entries = []
        for name in entries:
            item = QListWidgetItem(f'📁   {name}')
            item.setData(Qt.ItemDataRole.UserRole, name)
            self._list.addItem(item)
        self._update_action_enabled()

    def _update_action_enabled(self):
        has_selection = self._list.currentItem() is not None
        self._rename_btn.setEnabled(has_selection)
        self._delete_btn.setEnabled(has_selection)

    def _go_up(self):
        parent_dir = os.path.dirname(self._current.rstrip('/'))
        if (os.path.realpath(parent_dir) == self._root
                or os.path.realpath(parent_dir).startswith(self._root + os.sep)):
            self._current = parent_dir
            self._reload()

    def _open_folder(self, item):
        name = item.data(Qt.ItemDataRole.UserRole)
        path = os.path.join(self._current, name)
        if os.path.isdir(path):
            self._current = path
            self._reload()

    def _selected_name(self) -> str:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ''

    @staticmethod
    def _validate_name(name: str) -> str:
        if not name:
            return 'Folder name cannot be empty.'
        if '/' in name or '\\' in name or name in ('.', '..'):
            return 'Folder name contains illegal characters.'
        return ''

    def _new_folder(self):
        dlg = _NameInputDialog(self, 'New folder name', ok_label='Create')
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.name
        err = self._validate_name(name)
        if err:
            _show_result(self, 'Invalid Name', err, kind='error')
            return
        target = os.path.join(self._current, name)
        if os.path.exists(target):
            _show_result(self, 'Already Exists',
                         f'A folder named "{name}" already exists here.',
                         kind='error')
            return
        try:
            os.makedirs(target)
        except OSError as e:
            _show_result(self, 'Create Failed', str(e), kind='error')
            return
        self._reload()
        self._select_by_name(name)

    def _rename_folder(self):
        old = self._selected_name()
        if not old:
            return
        dlg = _NameInputDialog(self, f'Rename "{old}" to',
                               initial=old, ok_label='Rename')
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new = dlg.name
        if new == old:
            return
        err = self._validate_name(new)
        if err:
            _show_result(self, 'Invalid Name', err, kind='error')
            return
        dst = os.path.join(self._current, new)
        if os.path.exists(dst):
            _show_result(self, 'Already Exists',
                         f'A folder named "{new}" already exists here.',
                         kind='error')
            return
        try:
            os.rename(os.path.join(self._current, old), dst)
        except OSError as e:
            _show_result(self, 'Rename Failed', str(e), kind='error')
            return
        self._reload()
        self._select_by_name(new)

    def _delete_folder(self):
        name = self._selected_name()
        if not name:
            return
        path = os.path.join(self._current, name)
        total = 0
        try:
            for r, _d, fs in os.walk(path):
                for f in fs:
                    try:
                        total += os.path.getsize(os.path.join(r, f))
                    except OSError:
                        pass
        except Exception:
            pass
        confirm = _ConfirmDialog(
            self, 'Delete Folder',
            f'Permanently delete "{name}"?\n'
            f'Size: {format_size(total)}\n\n'
            f'This cannot be undone.',
            kind='error', yes_label='Delete', no_label='Cancel')
        if confirm.exec() != QDialog.DialogCode.Accepted:
            return
        if not _force_remove_tree(path):
            _show_result(
                self, 'Delete Failed',
                'Could not fully remove the folder.\n\n'
                'The NTFS partition metadata is likely corrupt from a '
                'previous crash. Unmount the drive and run:\n'
                '    sudo ntfsfix /dev/nvme0n1p1',
                kind='error')
            return
        self._reload()

    def _select_by_name(self, name: str):
        for i in range(self._list.count()):
            if self._list.item(i).data(Qt.ItemDataRole.UserRole) == name:
                self._list.setCurrentRow(i)
                return

    def _on_select(self):
        self._chosen = self._current
        self.accept()


# ── Public copy / delete API ─────────────────────────────────────────────────

def copy_scans_with_dialog(scans: list, dest: str, parent: QWidget) -> bool:
    if not scans:
        _show_result(parent, 'Nothing Selected',
                     'Select one or more scans first.')
        return False
    if not dest or not os.path.isdir(dest):
        _show_result(parent, 'No Destination',
                     'No external storage detected. Plug in a USB drive.',
                     kind='error')
        return False

    picker = _FolderPickerDialog(parent, dest)
    if picker.exec() != QDialog.DialogCode.Accepted or not picker.chosen_path:
        return False
    target_dir = picker.chosen_path

    total_bytes = sum(int(s.get('size_bytes', 0) or 0) for s in scans)

    try:
        free = shutil.disk_usage(target_dir).free
    except OSError:
        free = None
    if free is not None and free < total_bytes:
        _show_result(
            parent, 'Insufficient Space',
            f'This batch needs {format_size(total_bytes)} but only '
            f'{format_size(free)} is free on the USB.',
            kind='error')
        return False

    dlg = _CopyDialog(total_bytes, parent)
    worker = _CopyWorker(scans, target_dir)
    dlg.set_cancel_callback(worker.cancel)
    worker.progress.connect(dlg.update_progress)

    state = {'ok': False, 'msg': ''}
    def _on_done(ok: bool, msg: str):
        state['ok']  = ok
        state['msg'] = msg
        dlg.accept()
    worker.done.connect(_on_done)
    worker.start()
    dlg.exec()
    worker.wait()

    from core.audit import log_action
    log_action(
        'copy_completed' if state['ok'] else 'copy_failed',
        scans=len(scans), dest=target_dir, bytes=total_bytes)
    if state['ok']:
        _show_result(parent, 'Copy Complete', state['msg'])
    else:
        _show_result(parent, 'Copy Result',
                     state['msg'] or 'Copy did not complete.', kind='error')
    return state['ok']


def delete_scans_with_confirm(scans: list, parent: QWidget) -> bool:
    if not scans:
        _show_result(parent, 'Nothing Selected',
                     'Select one or more scans to delete.')
        return False

    total_size = sum(s.get('size_bytes', 0) for s in scans)
    confirm = _ConfirmDialog(
        parent, 'Delete Scans',
        f'Permanently delete {len(scans)} scan(s)?\n'
        f'Total size: {format_size(total_size)}\n\n'
        f'This cannot be undone.',
        kind='error', yes_label='Delete', no_label='Cancel')
    if confirm.exec() != QDialog.DialogCode.Accepted:
        return False

    failed = []
    for s in scans:
        path = s.get('scan_folder')
        if not path:
            continue
        if not _force_remove_tree(path):
            failed.append(
                f'{os.path.basename(path)}: '
                f'could not fully remove (NTFS metadata may be corrupt — '
                f'unmount and run `sudo ntfsfix /dev/nvme0n1p1`)')

    from core.audit import log_action
    log_action(
        'scans_deleted',
        count=len(scans) - len(failed),
        failed=len(failed),
        total_bytes=total_size)

    if failed:
        _show_result(
            parent, 'Some Deletes Failed',
            f'Deleted {len(scans) - len(failed)} of {len(scans)}.\n\n'
            + '\n'.join(failed[:5]),
            kind='error')
    else:
        _show_result(parent, 'Deleted',
                     f'Deleted {len(scans)} scan(s).')
    return True


# ── Orphan-scan recovery ─────────────────────────────────────────────────────

def _force_remove_tree(path: str) -> bool:
    """Delete a directory tree, robust to NTFS metadata corruption.

    The DATA partition is NTFS (ntfs3 driver), and a crashed `rosbag
    record` can leave directory entries whose stat() returns garbage
    (`ls -la` shows `-?????????`). Python's `shutil.rmtree` walks via
    os.scandir + stat and aborts halfway on those entries — that's
    what causes "delete left some bytes behind".

    Layered approach:
      1) shutil.rmtree (fast, clean trees).
      2) Best-effort: chmod -R u+w to fix any read-only NTFS files.
      3) `rm -rf` which uses unlinkat() directly, no pre-stat.
      4) Verify the folder is actually gone.
    """
    if not os.path.exists(path):
        return True
    try:
        shutil.rmtree(path)
    except Exception:
        pass
    if not os.path.exists(path):
        return True
    # Fallback: shell out. rm -rf survives broken dirents.
    try:
        subprocess.run(
            ['chmod', '-R', 'u+w', path],
            timeout=10, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        subprocess.run(
            ['rm', '-rf', '--', path],
            timeout=60, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return not os.path.exists(path)


def _release_stale_rosbag_locks(folder: str) -> None:
    """Defensive cleanup before promoting an orphan scan.

    An orphan exists because a previous run was force-killed without
    going through the normal Stop path. roslaunch / rosbag / driver
    processes are NOT spawned in their own session (so the GUI dies
    but they don't), and they can still be running, holding any
    `*.bag.active` file open. That:

      - prevents `shutil.rmtree` on some filesystems,
      - silently corrupts copies (we'd read a file being written),
      - leaves disk space tied up after a "delete" succeeds.

    SIGTERM the stragglers, give Linux a moment to release file
    descriptors, then rename `*.bag.active` → `*.bag` so copy/delete
    treat them uniformly. The file may not be re-indexed; the user
    can run `rosbag reindex` on it later if they need playback.
    """
    try:
        # 'record' is the actual rosbag recorder binary — a launch-file
        # <node pkg="rosbag" type="record"> runs as `record`, NOT as
        # `rosbag` (that's only the CLI wrapper). Without it here, a
        # straggler recorder still holds the .bag.active open while we
        # rename it below — the exact corruption recovery exists to fix.
        subprocess.run(
            ['killall', '-q', '-TERM',
             'rosbag', 'record', 'roslaunch',
             'hesai_ros_driver_node', 'xsens_mti_node',
             'seek_driver'],
            timeout=3, check=False)
    except Exception:
        pass
    time.sleep(0.4)
    try:
        for f in os.listdir(folder):
            if not f.endswith('.bag.active'):
                continue
            src = os.path.join(folder, f)
            dst = os.path.join(folder, f[:-len('.active')])
            if os.path.exists(dst):
                continue
            try:
                os.rename(src, dst)
            except OSError:
                pass
    except OSError:
        pass


def _recover_orphan(orphan: dict) -> None:
    parsed = orphan.get('parsed') or {}
    folder = orphan['scan_folder']
    _release_stale_rosbag_locks(folder)
    data = {
        'site':        parsed.get('site') or os.path.basename(folder),
        'floor_type':  parsed.get('floor_type') or 'Floor',
        'floor_num':   parsed.get('floor_num', 0),
        'scan_part':   parsed.get('scan_part') or '(recovered)',
        'incharge':    '(recovered)',
        'device':      config.DEVICE,
        'scan_folder': folder,
        'started_at':  parsed.get('started_guess'),
        'stopped_at':  orphan.get('mtime') or None,
        'recovered':   True,
    }
    target = os.path.join(folder, 'scan_info.json')
    tmp = target + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class _OrphanRow(QFrame):
    def __init__(self, orphan: dict, parent=None):
        super().__init__(parent)
        self.orphan = orphan
        self.setStyleSheet(
            f'_OrphanRow {{ background-color: {PANEL_BG};'
            f'  border: 1px solid {BORDER}; border-radius: 8px; }} '
            'QLabel { background: transparent; border: 0; } '
            'QRadioButton { background: transparent; border: 0; }'
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(4)

        parsed = orphan.get('parsed') or {}
        heading = parsed.get('site') or os.path.basename(orphan['scan_folder'])
        sub_bits = []
        if parsed.get('floor_type'):
            ft = parsed['floor_type']
            if ft == 'Ground Floor':
                sub_bits.append(ft)
            elif parsed.get('floor_num') is not None:
                sub_bits.append(f"{ft} {parsed['floor_num']}")
        if parsed.get('scan_part'):
            sub_bits.append(parsed['scan_part'])

        title = QLabel(heading)
        title.setStyleSheet(
            f'color: {PRIMARY}; font-size: 12pt; font-weight: bold; border: none;')
        outer.addWidget(title)

        when = (orphan.get('mtime') or '')[:16].replace('T', ' ')
        detail = QLabel(
            (' · '.join(sub_bits) + ('  ·  ' if sub_bits else '')) +
            f'{orphan["bag_count"]} bags  ·  {format_size(orphan["size_bytes"])}'
            f'  ·  {when or "?"}'
        )
        detail.setStyleSheet(f'color: {LABEL}; font-size: 10pt; border: none;')
        outer.addWidget(detail)

        path = QLabel(orphan['scan_folder'])
        path.setStyleSheet(f'color: {SUBTLE}; font-size: 9pt; border: none;')
        path.setWordWrap(True)
        outer.addWidget(path)

        radio_row = QHBoxLayout()
        radio_row.setSpacing(18)
        self._group = QButtonGroup(self)
        for key, label, color in [
            ('leave',   'Leave',   LABEL),
            ('recover', 'Recover', PRIMARY),
            ('delete',  'Delete',  DANGER),
        ]:
            rb = QRadioButton(label)
            rb.setProperty('action', key)
            rb.setCursor(Qt.CursorShape.PointingHandCursor)
            rb.setStyleSheet(
                f'QRadioButton {{ color: {color}; font-size: 11pt;'
                f'  font-weight: bold; }}')
            if key == 'leave':
                rb.setChecked(True)
            self._group.addButton(rb)
            radio_row.addWidget(rb)
        radio_row.addStretch()
        outer.addLayout(radio_row)

    def action(self) -> str:
        btn = self._group.checkedButton()
        return btn.property('action') if btn else 'leave'


class _RecoveryDialog(QDialog):
    def __init__(self, orphans: list, parent=None):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self._orphans = orphans
        self.setFixedSize(720, 500)

        layout = _make_themed_card(self, PRIMARY)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        title = QLabel('Incomplete Scans Found')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f'color: {PRIMARY}; font-size: 14pt; font-weight: bold; border: none;')
        layout.addWidget(title)

        msg = QLabel(
            f'{len(orphans)} scan folder(s) have recorded data but no scan '
            f'metadata — likely interrupted by a crash or power loss.\n'
            f'Choose what to do with each. Leave keeps it on disk and shows '
            f'this dialog again next launch.'
        )
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        msg.setStyleSheet(f'color: {TEXT}; font-size: 10pt; border: none;')
        layout.addWidget(msg)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet('background: transparent; border: 0;')
        host = QWidget()
        host.setStyleSheet(f'background: {BG}; border: 0;')
        col = QVBoxLayout(host)
        col.setSpacing(8)
        col.setContentsMargins(0, 0, 0, 0)
        self._rows = []
        for o in orphans:
            row = _OrphanRow(o)
            col.addWidget(row)
            self._rows.append(row)
        col.addStretch()
        scroll.setWidget(host)
        layout.addWidget(scroll, stretch=1)
        _enable_touch_scroll(scroll)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton('Cancel')
        cancel.setMinimumHeight(36)
        cancel.setMinimumWidth(100)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {LABEL};'
            f'  border: 1px solid {BORDER}; border-radius: 6px; padding: 0 20px;'
            f'  font-size: 11pt; font-weight: bold; }} '
            f'QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }}'
        )
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)

        apply_btn = QPushButton('Apply')
        apply_btn.setMinimumHeight(36)
        apply_btn.setMinimumWidth(120)
        apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_btn.setStyleSheet(
            f'QPushButton {{ background: {PRIMARY}; color: white; border: none;'
            f'  border-radius: 6px; padding: 0 22px; font-size: 11pt;'
            f'  font-weight: bold; }} '
            f'QPushButton:hover {{ background: {PRIMARY_DARK}; }}'
        )
        apply_btn.clicked.connect(self._on_apply)
        btn_row.addWidget(apply_btn)
        layout.addLayout(btn_row)

    def showEvent(self, event):
        super().showEvent(event)
        _center_on_parent(self)

    def _on_apply(self):
        recovered = deleted = 0
        failed = []
        for row in self._rows:
            action = row.action()
            if action == 'leave':
                continue
            folder = row.orphan['scan_folder']
            name = os.path.basename(folder)
            if action == 'recover':
                try:
                    _recover_orphan(row.orphan)
                    recovered += 1
                except OSError as e:
                    failed.append(f'recover {name}: {e}')
            elif action == 'delete':
                try:
                    shutil.rmtree(folder)
                    deleted += 1
                except OSError as e:
                    failed.append(f'delete {name}: {e}')

        self.accept()

        if not (recovered or deleted or failed):
            return
        parts = []
        if recovered:
            parts.append(f'Recovered {recovered} scan(s).')
        if deleted:
            parts.append(f'Deleted {deleted} folder(s).')
        if failed:
            parts.append('\nFailed:')
            parts.extend(failed[:5])
            if len(failed) > 5:
                parts.append(f'…and {len(failed) - 5} more.')
        _show_result(
            self.parent(),
            'Recovery Complete',
            '\n'.join(parts),
            kind='error' if failed else 'info',
        )


def offer_orphan_recovery(parent, dumps_root: str) -> bool:
    from gui.scan_stats import find_orphan_scans
    orphans = find_orphan_scans(dumps_root)
    if not orphans:
        return False
    dlg = _RecoveryDialog(orphans, parent)
    dlg.exec()
    return True
