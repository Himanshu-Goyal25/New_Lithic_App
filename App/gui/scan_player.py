"""Scan-player page — embeddable widget with overlays + async stop."""

import os
import sys
import json
import datetime
import threading

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QTextEdit, QGroupBox, QSizePolicy, QFrame,
    QGraphicsOpacityEffect,
)
from PySide6.QtCore import (
    Qt, Slot, QTimer, Signal, QPropertyAnimation, QEasingCurve,
    QFileSystemWatcher,
)
from PySide6.QtGui import QPixmap, QImage, QColor, QPainter

import config
from core.ros_controller import RosController
from core.qa_worker      import QAWorker
from gui.main_window import (
    BG, PRIMARY, PRIMARY_DARK, TEXT, LABEL, SUBTLE, DANGER, DANGER_DARK,
    BORDER, SUCCESS, SUCCESS_DARK, WARNING, VIDEO_BG, CONSOLE_BG,
    DISABLED_BG, DISABLED_TEXT, CHIP_BG, BTN_HOVER_WASH,
)


# Driver labels for the device-pill row
_DRIVER_LABELS = {
    'hesai': 'LiDAR',
    'xsens': 'IMU',
    'seek':  'Thermal',
}

# Topic display labels for the video switcher
_TOPIC_LABELS = {
    '/seek_camera/displayImage': 'Thermal',
}


# Driver key → config.BUFFER key (slack subtracted from threshold for WARN).
_DRIVER_BUFFER_KEY = {
    'hesai': 'lidar',
    'xsens': 'imu',
    'seek':  'seek',
}


def _bag_status(driver: str, count: int, expected: int) -> str:
    """Same status ladder QAWorker uses, computed per-driver for the
    closed-bag console summary."""
    buf = config.BUFFER.get(_DRIVER_BUFFER_KEY.get(driver, ''), 0)
    if count >= expected:
        return 'OK'
    if count >= expected - buf:
        return 'WARN'
    if count > 0:
        return 'LOW'
    return 'MISS'


class ScanPlayerPage(QWidget):
    """Live scan view — emits next_scan_requested when user is done."""

    scan_state_changed   = Signal(bool)
    next_scan_requested  = Signal()
    _ros_stop_done       = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.metadata         = {}
        self.ros              = RosController()
        self.qa               = QAWorker(self)
        self.scan_folder      = None
        self.is_scanning      = False
        self._is_stopping        = False
        self._finish_stop_called = False
        self._stop_generation    = 0
        self._stop_overlay       = None
        self._startup_guide      = None
        self._after_stop         = None
        self._latest_frames   = {}
        self._scan_start_time = None
        self._elapsed_timer   = QTimer(self)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._bag_count       = 0

        # QA signal routing: auto-stop on terminate, log everything else.
        self.qa.terminate.connect(self._on_qa_terminate)
        self.qa.log.connect(self._on_qa_log)

        # Bag rotation tracking. Driven by inotify (QFileSystemWatcher)
        # NOT by polling — every disk read we do on the scan folder
        # competes with rosbag's writes (same /media/cm5-v1/DATA disk)
        # and causes ~50 IMU msgs/30 s to be dropped from the bag.
        self._bag_active_name   = None
        self._bag_index         = 0

        self._scan_dir_watcher = QFileSystemWatcher(self)
        self._scan_dir_watcher.directoryChanged.connect(
            self._on_scan_folder_changed)

        # 1-Hz timer that recolours the LiDAR / IMU pills based on
        # ros.driver_live() — green if the driver's ROS node is in
        # `rosnode list`, red otherwise. Only runs while scanning.
        self._pill_timer = QTimer(self)
        self._pill_timer.setInterval(1000)
        self._pill_timer.timeout.connect(self._refresh_device_pills)

        self._ros_stop_done.connect(
            self._finish_stop, Qt.ConnectionType.QueuedConnection)
        self.ros.launch_died.connect(self._on_launch_died)
        self.ros.log.connect(self._on_ros_log)

        self._build_ui()
        self._set_scanning(False)

    # ── Public entry ────────────────────────────────────────────────────────

    def begin(self, metadata: dict):
        self.metadata = metadata
        self._refresh_info_header()
        self.scan_folder = None
        self._set_scanning(False)
        self.console.clear()
        self._info_elapsed.setText('—')
        self._info_bags.setText('—')
        self._info_size.setText('—')
        self._info_folder.setText('—')
        if self.video_panel is not None:
            self.video_panel.clear()
            self.video_panel.setText('No Signal')
        self._latest_frames.clear()
        self.log('Ready to start scan.')

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(12, 8, 12, 8)

        root.addLayout(self._build_header())
        root.addLayout(self._build_video_row(), stretch=1)
        root.addWidget(self._build_console())
        root.addLayout(self._build_buttons())

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        self.status_dot = QLabel('●')
        self.status_dot.setStyleSheet(f'color: {SUBTLE}; font-size: 20px;')
        row.addWidget(self.status_dot)

        self.info = QLabel('—')
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setStyleSheet('font-size: 11pt;')
        row.addWidget(self.info)
        row.addStretch()
        return row

    def _refresh_info_header(self):
        m = self.metadata
        if not m:
            self.info.setText('—')
            return
        ft = m.get('floor_type', '')
        floor_label = ft if ft == 'Ground Floor' else f'{ft} {m.get("floor_num", "")}'
        self.info.setText(
            f'<b style="color:{PRIMARY}">{m.get("site", "—")}</b>'
            f'<span style="color:{LABEL}">  ·  '
            f'{floor_label}  ·  '
            f'{m.get("scan_part", "")}  ·  '
            f'{m.get("incharge", "")}  ·  </span>'
            f'<span style="color:{PRIMARY_DARK}">{m.get("device", "")}</span>'
        )

    def _build_device_box(self) -> QGroupBox:
        box = QGroupBox('Devices')
        box.setStyleSheet(
            f'QGroupBox {{ color: {LABEL}; font-size: 10pt; font-weight: bold;'
            f'  border: 1px solid {BORDER}; border-radius: 6px;'
            f'  margin-top: 6px; padding-top: 4px; }} '
            f'QGroupBox::title {{ subcontrol-origin: margin; left: 10px; }}'
        )
        layout = QGridLayout(box)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 4, 8, 4)

        # All drivers active for this device — no toggle, but we still
        # render the pills so the player visually mirrors the reference.
        self.device_checks = {}
        _positions = {
            'hesai': (0, 0, 1, 1),
            'xsens': (0, 1, 1, 1),
            'seek':  (1, 0, 1, 2),
        }
        for driver, topics_dict in config.DRIVERS.items():
            # Skip watchdog-only entries (e.g. rosbag) that have no
            # source topics — they aren't sensors, so they don't belong
            # in the LiDAR/IMU readiness row. The QA watchdog still
            # monitors them and raises an alert overlay if they die.
            if not topics_dict:
                continue
            label = _DRIVER_LABELS.get(driver, driver.upper())
            btn = QPushButton(label)
            btn.setEnabled(False)   # locked — launch file has no driver-conditional args
            btn.setMinimumHeight(32)
            btn.setMinimumWidth(70)
            # Pills start in idle (grey). Once a scan is running, the
            # `_pill_timer` recolours them based on last-message age.
            btn.setStyleSheet(self._device_btn_style('idle'))
            r, c, rs, cs = _positions.get(driver, (len(self.device_checks), 0, 1, 2))
            layout.addWidget(btn, r, c, rs, cs)
            self.device_checks[driver] = btn
        return box

    @staticmethod
    def _device_btn_style(state: str = 'idle') -> str:
        """state: 'idle' | 'live' | 'stale' | 'dead'

        live  — last message < 2 s ago (data flowing)
        stale — 2-6 s since last message (driver slow / glitch)
        dead  — > 6 s OR never received any message
        idle  — not scanning
        """
        colors = {
            'idle':  CHIP_BG,
            'live':  SUCCESS,
            'stale': WARNING,
            'dead':  DANGER,
        }
        bg = colors.get(state, CHIP_BG)
        text = SUBTLE if state == 'idle' else 'white'
        return (
            f'QPushButton {{ border-radius: 6px; background-color: {bg};'
            f'  color: {text}; font-size: 10pt; font-weight: bold; border: none; }} '
            f'QPushButton:disabled {{ background-color: {bg}; color: {text}; }}'
        )

    def _build_video_row(self) -> QHBoxLayout:
        # ── State holders kept so rest of the class can still reference
        # them safely (e.g. `_on_frame` checks `_active_topic` and
        # `_latest_frames` for any future re-enabled thermal feed). ──
        self._topics       = list(config.VIEW_TOPIC.keys())
        self._active_topic = self._topics[0] if self._topics else None
        self.video_labels  = {}
        self._topic_btns   = {}
        self.video_panel   = None
        self._video_caption = None

        info_panel = self._build_info_panel()
        device_box = self._build_device_box()

        # Without a live video feed, the layout is just:
        #   [ status/info panel (wide) ]   [ device box ]
        # When the thermal camera is re-enabled (config.VIEW_TOPIC non-
        # empty), we restore the original video + switcher layout.
        if not self._topics:
            self._status_card = self._build_status_card()

            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(self._status_card, stretch=1)

            right_col = QVBoxLayout()
            right_col.setSpacing(6)
            right_col.addWidget(info_panel, stretch=1)
            right_col.addWidget(device_box)
            row.addLayout(right_col)
            return row

        # ─── Original layout (kept for when thermal comes back) ───────
        outer = QHBoxLayout()
        outer.setSpacing(8)

        self.video_panel = QLabel('No Signal')
        self.video_panel.setObjectName('video_feed')
        self.video_panel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_panel.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.video_panel.setMinimumSize(320, 180)
        self.video_panel.setScaledContents(False)
        self.video_panel.setStyleSheet(
            f'background-color: {VIDEO_BG}; border: 1px solid {BORDER};'
            f'border-radius: 4px; color: {LABEL}; font-size: 12px;'
        )
        for topic in self._topics:
            self.video_labels[topic] = self.video_panel
        outer.addWidget(self.video_panel, stretch=1)

        switcher = QVBoxLayout()
        switcher.setSpacing(6)
        switcher.addStretch()
        for topic in self._topics:
            label = _TOPIC_LABELS.get(topic, topic.split('/')[-1].title())
            btn = QPushButton(label)
            btn.setFixedWidth(80)
            btn.setMinimumHeight(38)
            btn.setCheckable(True)
            btn.setChecked(topic == self._active_topic)
            btn.setStyleSheet(self._switcher_btn_style(topic == self._active_topic))
            btn.clicked.connect(lambda _checked=False, t=topic: self._switch_topic(t))
            switcher.addWidget(btn)
            self._topic_btns[topic] = btn
        switcher.addStretch()

        cap_text = (_TOPIC_LABELS.get(self._active_topic,
                    self._active_topic.split('/')[-1])
                    if self._active_topic else '')
        self._video_caption = QLabel(cap_text)
        self._video_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._video_caption.setStyleSheet(f'font-size: 10pt; color: {SUBTLE};')

        panel_col = QVBoxLayout()
        panel_col.setSpacing(4)
        panel_col.addWidget(self._video_caption)
        panel_col.addLayout(outer, stretch=1)

        right_col = QVBoxLayout()
        right_col.setSpacing(6)
        right_col.addWidget(info_panel, stretch=1)
        right_col.addWidget(device_box)

        wrap = QHBoxLayout()
        wrap.setSpacing(10)
        wrap.addLayout(panel_col, stretch=1)
        wrap.addLayout(switcher)
        wrap.addLayout(right_col)
        return wrap

    def _build_status_card(self) -> QWidget:
        """Replaces the live video feed with a minimal per-driver status.

        Layout:
            ● RECORDING / IDLE / STOPPED          (overall scan state)
            ────────────────────────────────
            LiDAR                          [LIVE]
            IMU                            [LIVE]

        No live Hz, no live bag size, no live msg counts — those were
        the source of confusion and back-pressure on the publishers.
        The per-bag close summary in the console reads from the bag
        file itself, which is the authoritative number anyway.
        """
        card = QFrame()
        card.setObjectName('statusCard')
        card.setStyleSheet(
            f'#statusCard {{ background-color: {VIDEO_BG};'
            f'  border: 1px solid {BORDER}; border-radius: 8px; }}'
            'QLabel { background: transparent; border: 0; }'
        )
        card.setMinimumSize(360, 200)

        col = QVBoxLayout(card)
        col.setContentsMargins(24, 18, 24, 18)
        col.setSpacing(14)

        # ── Top: overall scan state ────────────────────────────────────
        top = QHBoxLayout()
        top.setSpacing(10)
        top.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._status_big_dot = QLabel('●')
        self._status_big_dot.setStyleSheet(
            f'color: {SUBTLE}; font-size: 28pt;')
        # Opacity-effect + property-animation drives the "pulse" while
        # recording. Built up-front so toggling later is just .start()
        # / .stop() — no widget rebuild.
        self._dot_opacity = QGraphicsOpacityEffect(self._status_big_dot)
        self._dot_opacity.setOpacity(1.0)
        self._status_big_dot.setGraphicsEffect(self._dot_opacity)

        self._dot_pulse = QPropertyAnimation(self._dot_opacity, b'opacity', self)
        self._dot_pulse.setDuration(900)               # one fade cycle (ms)
        self._dot_pulse.setStartValue(1.0)
        self._dot_pulse.setKeyValueAt(0.5, 0.30)       # dim mid-cycle
        self._dot_pulse.setEndValue(1.0)
        self._dot_pulse.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._dot_pulse.setLoopCount(-1)               # forever, until stopped
        top.addWidget(self._status_big_dot)

        self._status_big_text = QLabel('IDLE')
        self._status_big_text.setStyleSheet(
            f'color: {SUBTLE}; font-size: 18pt; font-weight: bold;'
            f'letter-spacing: 3px;')
        top.addWidget(self._status_big_text)
        col.addLayout(top)

        div1 = QFrame()
        div1.setFrameShape(QFrame.Shape.HLine)
        div1.setFixedHeight(1)
        div1.setStyleSheet(f'background-color: {BORDER}; border: 0;')
        col.addWidget(div1)

        # ── Per-driver liveness rows ───────────────────────────────────
        self._sensor_rows: dict = {}   # driver -> dict of labels
        for driver, topics_dict in config.DRIVERS.items():
            # Skip watchdog-only entries (e.g. rosbag) that have no
            # source topics — they aren't sensors and don't belong in
            # the liveness card. The QA watchdog still monitors them.
            if not topics_dict:
                continue
            row, refs = self._make_sensor_row(driver)
            col.addLayout(row)
            self._sensor_rows[driver] = refs

        col.addStretch()
        return card

    def _make_sensor_row(self, driver: str):
        """One sensor row inside the status card.

        Layout:  Name                          [STATE]
        Status = whether the driver's ROS node is currently registered
        with the master (`rosnode list`).
        """
        row = QHBoxLayout()
        row.setSpacing(10)
        row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        name = QLabel(_DRIVER_LABELS.get(driver, driver.upper()))
        name.setStyleSheet(
            f'color: {TEXT}; font-size: 14pt; font-weight: bold;')

        state = QLabel('—')
        state.setFixedWidth(72)
        state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        state.setStyleSheet(
            f'color: white; font-size: 10pt; font-weight: bold;'
            f'background: {SUBTLE}; border-radius: 4px; padding: 4px 8px;'
            f'letter-spacing: 1px;')

        row.addWidget(name)
        row.addStretch()
        row.addWidget(state)
        return row, {'name': name, 'state': state}

    def _update_status_card(self, scanning: bool):
        """Repaint the status card when the scan state changes."""
        if not hasattr(self, '_status_big_dot'):
            return
        if scanning:
            self._status_big_dot.setStyleSheet(
                f'color: {SUCCESS}; font-size: 28pt;')
            self._status_big_text.setStyleSheet(
                f'color: {SUCCESS}; font-size: 18pt; font-weight: bold;'
                f'letter-spacing: 3px;')
            self._status_big_text.setText('RECORDING')
            # Start the pulse — fade 1.0 → 0.3 → 1.0 forever, ~0.9 s
            # per cycle. The "REC" dot on TV cameras pulses at this
            # tempo for the same reason: unmistakable at a glance.
            if hasattr(self, '_dot_pulse'):
                self._dot_pulse.start()
        else:
            # Freeze pulse + restore full opacity before painting the
            # static idle state. Without the explicit setOpacity(1.0),
            # the dot would be stuck at whatever opacity the animation
            # left behind when we stopped it mid-cycle.
            if hasattr(self, '_dot_pulse'):
                self._dot_pulse.stop()
            if hasattr(self, '_dot_opacity'):
                self._dot_opacity.setOpacity(1.0)
            self._status_big_dot.setStyleSheet(
                f'color: {SUBTLE}; font-size: 28pt;')
            self._status_big_text.setStyleSheet(
                f'color: {SUBTLE}; font-size: 18pt; font-weight: bold;'
                f'letter-spacing: 3px;')
            self._status_big_text.setText(
                'STOPPED' if self.scan_folder else 'IDLE')
            # Reset sensor rows
            if hasattr(self, '_sensor_rows'):
                for refs in self._sensor_rows.values():
                    refs['state'].setText('—')
                    refs['state'].setStyleSheet(
                        f'color: white; font-size: 10pt; font-weight: bold;'
                        f'background: {SUBTLE}; border-radius: 4px;'
                        f'padding: 4px 8px; letter-spacing: 1px;')

    def _build_info_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(220)
        panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        panel.setStyleSheet(
            f'background-color: {BG}; border-left: 1px solid {BORDER};')
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 8, 10, 8)
        layout.setSpacing(3)

        def _section(title: str, value_attr: str):
            t = QLabel(title)
            t.setStyleSheet(f'color: {SUBTLE}; font-size: 11pt; border: none;')
            t.setContentsMargins(0, 10, 0, 0)
            v = QLabel('—')
            v.setStyleSheet(
                f'color: {TEXT}; font-size: 16pt; font-weight: bold; border: none;')
            v.setContentsMargins(0, 3, 0, 0)
            setattr(self, value_attr, v)
            layout.addWidget(t)
            layout.addWidget(v)

        _section('DURATION', '_info_elapsed')
        _section('BAGS',     '_info_bags')
        _section('BAG SIZE', '_info_size')

        folder_title = QLabel('FOLDER')
        folder_title.setStyleSheet(f'color: {SUBTLE}; font-size: 11pt; border: none;')
        folder_title.setContentsMargins(0, 10, 0, 0)
        layout.addWidget(folder_title)

        self._info_folder = QLabel('—')
        self._info_folder.setStyleSheet(
            f'color: {LABEL}; font-size: 10pt; font-weight: bold; border: none;')
        self._info_folder.setWordWrap(True)
        self._info_folder.setFixedWidth(196)
        self._info_folder.setContentsMargins(0, 3, 0, 0)
        layout.addWidget(self._info_folder)
        layout.addStretch()
        return panel

    @staticmethod
    def _switcher_btn_style(active: bool) -> str:
        if active:
            return (
                f'QPushButton {{ background-color: {PRIMARY}; color: white;'
                f'  border-radius: 6px; font-size: 11pt; font-weight: bold; border: none; }} '
                f'QPushButton:hover {{ background-color: {PRIMARY_DARK}; }}'
            )
        return (
            f'QPushButton {{ background-color: {CHIP_BG}; color: {LABEL};'
            f'  border-radius: 6px; font-size: 11pt; border: 1px solid {BORDER}; }} '
            f'QPushButton:hover {{ background-color: {BTN_HOVER_WASH}; color: {PRIMARY}; }}'
        )

    def _switch_topic(self, topic: str):
        self._active_topic = topic
        for t, btn in self._topic_btns.items():
            btn.setChecked(t == topic)
            btn.setStyleSheet(self._switcher_btn_style(t == topic))
        self._video_caption.setText(
            _TOPIC_LABELS.get(topic, topic.split('/')[-1]))
        if topic in self._latest_frames:
            self.video_panel.setPixmap(self._latest_frames[topic])
        else:
            self.video_panel.clear()
            self.video_panel.setText('No Signal')

    def _build_console(self) -> QTextEdit:
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setFixedHeight(115)
        self.console.setStyleSheet(
            f'background-color: {CONSOLE_BG}; color: {TEXT};'
            f'font-family: "DejaVu Sans Mono", "Liberation Mono",'
            f' "Ubuntu Mono", Consolas, Menlo, monospace;'
            f'font-size: 12pt;'
            f'border: 1px solid {BORDER}; border-radius: 4px;'
        )
        return self.console

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        self.start_btn = QPushButton('▶  Start Scan')
        self.start_btn.setMinimumHeight(42)
        self.start_btn.setStyleSheet(
            f'QPushButton {{ background-color: {PRIMARY}; color: white;'
            f'  border-radius: 6px; font-size: 13pt; font-weight: bold; border: none; }} '
            f'QPushButton:hover {{ background-color: {PRIMARY_DARK}; }} '
            f'QPushButton:disabled {{ background-color: {DISABLED_BG}; color: {DISABLED_TEXT}; }}'
        )
        self.start_btn.clicked.connect(self._start_scan)
        row.addWidget(self.start_btn, stretch=1)

        self.stop_btn = QPushButton('■   Stop Scan')
        self.stop_btn.setMinimumHeight(42)
        self.stop_btn.setStyleSheet(
            f'QPushButton {{ background-color: {DANGER}; color: white;'
            f'  border-radius: 6px; font-size: 13pt; font-weight: bold; border: none; }} '
            f'QPushButton:hover {{ background-color: {DANGER_DARK}; }} '
            f'QPushButton:disabled {{ background-color: {DISABLED_BG}; color: {DISABLED_TEXT}; }}'
        )
        self.stop_btn.clicked.connect(self._stop_scan)
        row.addWidget(self.stop_btn, stretch=1)

        self.next_btn = QPushButton('▶▶  Next Scan')
        self.next_btn.setMinimumHeight(42)
        self.next_btn.setVisible(False)
        self.next_btn.setStyleSheet(
            f'QPushButton {{ background-color: {SUCCESS}; color: white;'
            f'  border-radius: 6px; font-size: 13pt; font-weight: bold; border: none; }} '
            f'QPushButton:hover {{ background-color: {SUCCESS_DARK}; }}'
        )
        self.next_btn.clicked.connect(self._next_scan)
        row.addWidget(self.next_btn, stretch=1)
        return row

    # ── State ───────────────────────────────────────────────────────────────

    def _set_scanning(self, scanning: bool):
        self.is_scanning = scanning
        scan_has_run = self.scan_folder is not None
        self.start_btn.setEnabled(not scanning and not scan_has_run)
        self.stop_btn.setEnabled(scanning)
        self.next_btn.setVisible(not scanning and scan_has_run)
        colour = SUCCESS if scanning else SUBTLE
        self.status_dot.setStyleSheet(f'color: {colour}; font-size: 20px;')
        # Repaint the big status card (thermal-disabled layout only).
        self._update_status_card(scanning)
        self.scan_state_changed.emit(scanning)

    def _freeze_recording_ui(self):
        """Snap the visible 'recording' indicators to a stopping state
        the instant Stop is clicked — before ROS teardown finishes.

        Does NOT flip is_scanning / buttons / scan_state_changed; those
        stay locked until _finish_stop confirms ROS is actually down."""
        # Header dot → neutral
        self.status_dot.setStyleSheet(f'color: {SUBTLE}; font-size: 20px;')

        # Big status card: kill the pulse, restore opacity, show STOPPING.
        if hasattr(self, '_dot_pulse'):
            self._dot_pulse.stop()
        if hasattr(self, '_dot_opacity'):
            self._dot_opacity.setOpacity(1.0)
        if hasattr(self, '_status_big_dot'):
            self._status_big_dot.setStyleSheet(
                f'color: {SUBTLE}; font-size: 28pt;')
            self._status_big_text.setStyleSheet(
                f'color: {SUBTLE}; font-size: 18pt; font-weight: bold;'
                f'letter-spacing: 3px;')
            self._status_big_text.setText('STOPPING')

        # Device pills back to idle grey — drivers are being torn down,
        # any LIVE/DEAD reading from here on is meaningless.
        for btn in self.device_checks.values():
            btn.setStyleSheet(self._device_btn_style('idle'))

    # ── Scan lifecycle ──────────────────────────────────────────────────────

    def _make_scan_folder(self) -> str:
        now        = datetime.datetime.now()
        month_year = now.strftime('%B_%Y').lower()
        date_str   = now.strftime('%d_%b').upper()
        timestamp  = now.strftime('%Y%m%d_%H%M%S')
        m = self.metadata
        floor_seg = (m['floor_type'] if m['floor_type'] == 'Ground Floor'
                     else f'{m["floor_type"]}{m["floor_num"]}')
        name = (
            f'{m["site"]}_{floor_seg}'
            f'_{m["scan_part"]}_{timestamp}'
        ).replace(' ', '_')
        path = os.path.join(config.DUMP_PATH, 'dumps', month_year, date_str, name)
        os.makedirs(path, exist_ok=True)
        return path

    def _start_scan(self):
        self.scan_folder = self._make_scan_folder()

        # CRITICAL: pause the device-readiness monitor before launching
        # roslaunch. The IMU readiness probe opens /dev/ttyUSB0 for ~1 s
        # to read the Xsens MTi preamble, and Linux lets two processes
        # share a TTY — meaning the bytes from the IMU get split between
        # our probe and the xsens_mti_node driver. Each probe steals
        # ~9 IMU msgs; 6 probes per 30 s = the ~50 msg/bag drop. Pausing
        # blocks for up to 2 s if a probe is in flight, so by the time
        # roslaunch's xsens_mti_node opens the port we've fully released it.
        from gui.device_status import DeviceMonitor
        DeviceMonitor.instance().pause()

        # Reset per-bag tracking — new scan starts at bag 0.
        self._bag_active_name = None
        self._bag_index       = 0

        # Watch the scan folder via inotify (vs. polling) so rosbag's
        # writes aren't fighting our reads for the same disk.
        if self.scan_folder:
            self._scan_dir_watcher.addPath(self.scan_folder)

        # Subscribe BEFORE launch so the spin thread picks up our handler.
        self.ros.frame_received.connect(self._on_frame)
        for topic, msg_type in config.VIEW_TOPIC.items():
            self.ros.subscribe(topic, msg_type)

        # Launch ROS. The launch file's only required arg is the scan
        # folder rosbag should write into.
        args = {'data_path': self.scan_folder}
        self.ros.launch(config.LAUNCH_FILE, args, metadata=self.metadata)

        self.log(f'Scan folder: {self.scan_folder}')
        self.log('Launching ROS drivers.')

        self._scan_start_time = datetime.datetime.now()
        self._bag_count = 0
        self._info_elapsed.setText('00:00')
        self._info_bags.setText('0')
        self._info_size.setText('0 MB')
        parts = self.scan_folder.split('/')
        try:
            idx = parts.index('dumps')
            rel = '/'.join(parts[idx:])
        except ValueError:
            rel = self.scan_folder
        self._info_folder.setText(rel.replace('/', ' / '))
        self._info_folder.setToolTip(self.scan_folder)
        self._elapsed_timer.start(1000)
        # Start the device-pill freshness check + paint once immediately
        # so the operator gets feedback within the first second of the scan.
        self._pill_timer.start()
        self._refresh_device_pills()

        # Real-time QA: driver-liveness watchdog, disk check, per-bag
        # threshold check. Auto-terminates the scan on any fatal issue.
        self.qa.start(self.scan_folder, self.ros)

        self._set_scanning(True)
        self.log('Scan started.')

        # Startup guidance overlay: STAY STILL → ROTATE → START MOVING.
        # IMU needs ~45 s of static initialisation, then a brief
        # rotate-in-place calibration, before the operator starts walking.
        self._startup_guide = _StartupGuide(self)
        self._startup_guide.setGeometry(self.rect())
        self._startup_guide.show()
        self._startup_guide.raise_()

        # File-based tripwire: 60 s after Start, if the scan folder
        # still has zero `.bag` and zero `.bag.active`, something
        # between the launch and the disk is broken — rosbag died
        # silently, the path was wrong, or the disk is read-only.
        # The node-liveness watchdog catches "rosbag never came up";
        # this catches the rarer "rosbag is alive but not writing".
        # Captures _stop_generation so a Stop + restart doesn't fire
        # an old tripwire against a new scan.
        gen = self._stop_generation
        QTimer.singleShot(60_000,
                          lambda g=gen: self._check_first_bag_appeared(g))

        from core.audit import log_action
        log_action(
            'scan_started',
            site=self.metadata.get('site'),
            folder=self.scan_folder)

    def _stop_scan(self, after=None):
        if not self.is_scanning:
            return
        if self._is_stopping:
            if after:
                existing = self._after_stop
                self._after_stop = (
                    (lambda e=existing, a=after: (e(), a()))
                    if existing else after)
            return

        self._is_stopping        = True
        self._finish_stop_called = False
        self._after_stop         = after

        self.stop_btn.setEnabled(False)
        self.stop_btn.setText('Stopping…')

        # Stop QA timers immediately so they don't fire during teardown
        # (a stale tick after the master goes down would spuriously
        # report "driver not responding").
        self.qa.stop()

        # Freeze the recording UI right now, BEFORE ros.stop() runs in a
        # background thread. Otherwise the elapsed counter keeps ticking
        # and the RECORDING pulse keeps animating while ROS is being
        # killed — the operator sees "still recording" until _finish_stop
        # finally fires, which is misleading.
        self._elapsed_timer.stop()
        self._pill_timer.stop()
        self._freeze_recording_ui()

        try:
            self.ros.frame_received.disconnect(self._on_frame)
        except (TypeError, RuntimeError):
            pass

        self.log('Stopping scan — please wait…')

        self._stop_overlay = _StoppingOverlay(self)
        self._stop_overlay.setGeometry(self.rect())
        self._stop_overlay.show()
        self._stop_overlay.raise_()

        self._stop_generation += 1
        gen = self._stop_generation
        threading.Thread(
            target=self._run_ros_stop, args=(gen,), daemon=True).start()
        # Generation-guarded like the 60-s tripwire: without `gen`, a
        # deadline armed by a previous Stop could fire mid-teardown of a
        # LATER stop (Stop A → Next Scan → Start B → Stop B inside A's
        # 30-s window) and force _finish_stop while ros.stop() is still
        # running.
        QTimer.singleShot(
            30_000, lambda g=gen: self._stop_deadline_expired(g))

    def _run_ros_stop(self, gen):
        try:
            self.ros.stop()
        except Exception as e:
            print(f'[scan_player] ros.stop() raised: {e}', file=sys.stderr)
        finally:
            if gen == self._stop_generation:
                self._ros_stop_done.emit()

    @Slot()
    def _finish_stop(self):
        if self._finish_stop_called:
            return
        self._finish_stop_called = True

        if self._stop_overlay is not None:
            self._stop_overlay.hide()
            self._stop_overlay.deleteLater()
            self._stop_overlay = None

        # Defensive: always clear startup guide reference
        try:
            if self._startup_guide is not None:
                self._startup_guide.cancel()
        except Exception:
            pass
        self._startup_guide = None

        self.stop_btn.setText('■   Stop Scan')
        self._set_scanning(False)
        self._write_metadata()
        self.log('Scan stopped.')

        from core.audit import log_action
        log_action('scan_stopped', folder=self.scan_folder)

        self._elapsed_timer.stop()
        self._pill_timer.stop()
        # Detach the scan-folder inotify watcher — the directory still
        # exists but we don't care about further changes.
        if self.scan_folder:
            try:
                self._scan_dir_watcher.removePath(self.scan_folder)
            except Exception:
                pass
        # Pills back to idle (grey) — scan is over, no data to track.
        for btn in self.device_checks.values():
            btn.setStyleSheet(self._device_btn_style('idle'))
        self._latest_frames.clear()
        if self.video_panel is not None:
            self.video_panel.clear()
            self.video_panel.setText('No Signal')

        self._is_stopping = False

        # Resume the device-readiness monitor now that the xsens driver
        # has released /dev/ttyUSB0. Safe to probe again.
        from gui.device_status import DeviceMonitor
        DeviceMonitor.instance().resume()

        after = self._after_stop
        self._after_stop = None
        if after:
            try:
                after()
            except Exception as e:
                print(f'[scan_player] after-stop callback raised: {e}',
                      file=sys.stderr)

    def _stop_deadline_expired(self, gen: int):
        if gen != self._stop_generation:
            return   # stale deadline from a previous stop
        if self._finish_stop_called:
            return
        self.log(
            'WARNING: stop sequence exceeded 30s — forcing UI recovery. '
            'Some ROS processes may still be alive.')
        self._finish_stop()

    # ── Frame handling ──────────────────────────────────────────────────────

    @Slot(str, QImage)
    def _on_frame(self, topic: str, img: QImage):
        # No video panel in the thermal-disabled layout — drop frames.
        if self.video_panel is None:
            return
        if self.video_panel.width() <= 0 or self.video_panel.height() <= 0:
            return
        pix = QPixmap.fromImage(img).scaled(
            self.video_panel.width(), self.video_panel.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._latest_frames[topic] = pix
        if topic == self._active_topic:
            self.video_panel.setPixmap(pix)

    def _on_launch_died(self):
        if self.is_scanning and not self._is_stopping:
            self._auto_terminate('roslaunch process died unexpectedly')

    def _auto_terminate(self, reason: str):
        self._stop_scan(after=lambda: self._show_qa_alert(reason))

    def _show_qa_alert(self, reason: str):
        alert = _AlertOverlay(self, title='Scan Terminated', message=reason)
        alert.setGeometry(self.rect())
        alert.show()
        alert.raise_()

    # ── QA signal handlers ─────────────────────────────────────────────────
    def _on_qa_terminate(self, reason: str):
        """Slot for QAWorker.terminate — auto-stops the scan and pops
        an alert overlay describing why."""
        if self.is_scanning and not self._is_stopping:
            self._auto_terminate(reason)

    def _on_qa_log(self, msg: str, level: str):
        """Slot for QAWorker.log — routes through the standard console
        logger so QA messages get the same timestamp + colour treatment
        as everything else."""
        # Prefix so it's easy to tell QA noise from app noise in app.log
        self.log(f'QA: {msg}', level=level)

    # ── Metadata ────────────────────────────────────────────────────────────

    def _write_metadata(self):
        if not self.scan_folder:
            return
        data = {
            **self.metadata,
            'scan_folder': self.scan_folder,
            'started_at':  self._scan_start_time.isoformat() if self._scan_start_time else None,
            'stopped_at':  datetime.datetime.now().isoformat(),
        }
        target = os.path.join(self.scan_folder, 'scan_info.json')
        tmp = target + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except OSError as e:
            self.log(f'Failed to write scan_info.json: {e}')
            try:
                os.remove(tmp)
            except OSError:
                pass

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _next_scan(self):
        self.next_scan_requested.emit()

    def _refresh_device_pills(self):
        """Repaint the LIVE/DEAD pills + status-card sensor rows.

        Driven by `RosController.driver_live(driver)`, which in turn
        consults `_NodeMonitor` — a single `rosnode list` poll every
        few seconds. Binary: the driver's ROS node is either registered
        with the master (LIVE) or it isn't (DEAD).
        """
        if not self.is_scanning:
            return

        # Bag rotation detection now lives in _on_scan_folder_changed
        # (inotify) — no per-tick listdir here, keeps rosbag's writes
        # on /media/cm5-v1/DATA undisturbed by competing reads.

        for driver, btn in self.device_checks.items():
            # `ros.driver_live(driver)` consults `rosnode list` — True
            # means the driver's ROS node is registered with the master.
            alive = False
            if hasattr(self.ros, 'driver_live'):
                try:
                    alive = self.ros.driver_live(driver)
                except Exception:
                    alive = False

            state = 'live' if alive else 'dead'

            # Sidebar pill colour (small chips below the card)
            btn.setStyleSheet(self._device_btn_style(state))

            # Big sensor row inside the status card
            refs = getattr(self, '_sensor_rows', {}).get(driver)
            if refs is not None:
                bg, text = ((SUCCESS, 'LIVE') if alive
                            else (DANGER, 'DEAD'))
                refs['state'].setText(text)
                refs['state'].setStyleSheet(
                    f'color: white; font-size: 10pt; font-weight: bold;'
                    f'background: {bg}; border-radius: 4px;'
                    f'padding: 4px 8px; letter-spacing: 1px;')

    def _check_first_bag_appeared(self, gen: int):
        """60-second startup tripwire. Fired once via QTimer.singleShot
        from `_start_scan`. If the scan folder still has zero `.bag`
        and zero `.bag.active` files by now, recording never started
        on disk — terminate the scan and surface a clear error.

        `gen` is captured from `_stop_generation` at scheduling time;
        we only act if it still matches, so a Stop + new scan can't
        be killed by a stale tripwire from the previous scan.
        """
        if gen != self._stop_generation:
            return
        if not self.is_scanning or self._is_stopping:
            return
        if not self.scan_folder or not os.path.isdir(self.scan_folder):
            return
        try:
            entries = os.listdir(self.scan_folder)
        except OSError:
            return
        has_any_bag = any(
            f.endswith('.bag') or f.endswith('.bag.active')
            for f in entries)
        if has_any_bag:
            return
        self.log(
            'No bag file appeared in 60 s — rosbag is alive but not '
            'writing to disk. Check disk permissions, free space, and '
            'the data_path arg.',
            level='ERROR')
        self._auto_terminate(
            'No data was recorded in the first 60 seconds. The '
            'recorder is alive but no .bag file was written — check '
            'disk health and that the scan folder is writable.')

    def _check_bag_rotation(self):
        """Detect when rosbag has rolled the .bag.active to a new file.

        On rotation: read the just-closed bag's per-topic counts
        directly from the bag index (authoritative, matches what
        terminal-launch produces) and log them as a console summary.
        """
        if not self.scan_folder or not os.path.isdir(self.scan_folder):
            return
        try:
            actives = [f for f in os.listdir(self.scan_folder)
                       if f.endswith('.bag.active')]
        except OSError:
            return
        current = actives[0] if actives else None
        if current == self._bag_active_name:
            return  # no change

        # If we had a previous bag, it just closed. Read its index for
        # the per-driver msg counts; these are the SAME numbers that
        # `rosbag info <file>` would print.
        if self._bag_active_name is not None:
            closed_name = self._bag_active_name[:-len('.active')]
            closed_path = os.path.join(self.scan_folder, closed_name)
            try:
                sz = os.path.getsize(closed_path)
            except OSError:
                sz = 0
            size_str = (f'{sz/1024**3:.2f} GB' if sz >= 1024**3
                        else f'{sz/1024**2:.1f} MB')

            bag_counts = self._read_bag_topic_counts(closed_path)
            self._bag_index += 1

            if bag_counts is None:
                # rosbag.Bag() raised — bag is corrupt or truncated.
                # Don't print the per-driver block (we have no counts),
                # log an explicit ERROR and terminate the scan immediately.
                self.log(
                    f'Bag {self._bag_index} ({closed_name}) appears '
                    f'corrupt — could not read index.',
                    level='ERROR')
                if self.is_scanning and not self._is_stopping:
                    self._auto_terminate(
                        f'Bag {self._bag_index} failed to open — '
                        f'recording is producing corrupt data. '
                        f'Check disk health.')
            else:
                per_driver = {}
                for driver, topics_dict in config.DRIVERS.items():
                    # Skip watchdog-only entries (e.g. rosbag) that
                    # have no source topics — they have no counts to
                    # display and would just clutter the BAG block.
                    if not topics_dict:
                        continue
                    count    = sum(bag_counts.get(t, 0) for t in topics_dict)
                    expected = sum(topics_dict.values())
                    per_driver[driver] = (count, expected)

                # HTML for the on-screen console; &nbsp; preserves indent
                # (regular spaces collapse in rich-text mode).
                indent = '&nbsp;' * 6
                lines = [f'Bag {self._bag_index} closed']
                for driver, (count, expected) in per_driver.items():
                    label  = _DRIVER_LABELS.get(driver, driver.upper())
                    status = _bag_status(driver, count, expected)
                    lines.append(
                        f'{indent}{label:<8s} {count:>6,} / {expected:>6,}  '
                        f'{status}'.replace(' ', '&nbsp;'))
                lines.append(
                    f'{indent}{"Size":<8s} {size_str:>6s}'.replace(
                        ' ', '&nbsp;'))
                self.log('<br>'.join(lines), level='BAG')

        self._bag_active_name = current

    @staticmethod
    def _read_bag_topic_counts(bag_path: str):
        """Authoritative per-topic message count from a closed bag.

        Reads the bag's index (no message decoding) — same numbers
        `rosbag info <file>` prints.

        Returns:
            dict[topic -> count]  on successful open (may be empty if
                                   the bag really has no messages),
            None                  if the bag file cannot be opened or
                                   indexed — i.e. corrupt / truncated.
        """
        try:
            import rosbag
        except ImportError:
            return {}
        try:
            with rosbag.Bag(bag_path, 'r') as bag:
                info = bag.get_type_and_topic_info()
            # `info.topics` is dict[topic_name -> TopicTuple(msg_type,
            # message_count, connections, frequency)].
            return {t: tt.message_count for t, tt in info.topics.items()}
        except Exception:
            return None

    def _tick_elapsed(self):
        """Update the DURATION label. No disk I/O — BAGS and BAG SIZE are
        refreshed by `_on_scan_folder_changed` (inotify) instead, so that
        rosbag's writes to /media/cm5-v1/DATA aren't competing with our
        polling reads for inode locks and page cache."""
        if not self._scan_start_time:
            return
        elapsed = datetime.datetime.now() - self._scan_start_time
        total = int(elapsed.total_seconds())
        h, rem = divmod(total, 3600)
        m, s   = divmod(rem, 60)
        self._info_elapsed.setText(
            f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}')

    def _on_scan_folder_changed(self, _path: str):
        """Inotify callback — fires when a file is added to or removed
        from the scan folder (i.e. each bag rotation, not on the
        currently-active bag growing in size).

        Roughly once per 30 s, instead of the 1 Hz polling we used
        before. That's a 30× reduction in disk reads on the same
        filesystem rosbag is writing to — and rosbag's per-topic
        queue stops overflowing during contention windows."""
        if not self.is_scanning or not self.scan_folder:
            return
        # Drive bag rotation detection + refresh the info panel.
        self._check_bag_rotation()
        self._refresh_bag_info_labels()

    def _refresh_bag_info_labels(self):
        """Single place that does the listdir + getsize. Called from the
        inotify handler only — never from a periodic timer."""
        if not self.scan_folder or not os.path.isdir(self.scan_folder):
            return
        try:
            bags = [f for f in os.listdir(self.scan_folder)
                    if f.endswith('.bag')]
            self._info_bags.setText(str(len(bags)))
            total = sum(os.path.getsize(os.path.join(self.scan_folder, f))
                        for f in bags)
            if total >= 1024 ** 3:
                self._info_size.setText(f'{total / 1024**3:.2f} GB')
            else:
                self._info_size.setText(f'{total / 1024**2:.1f} MB')
        except OSError:
            pass

    # Levels permitted to appear in the on-screen console. The raw 'ROS'
    # firehose from roslaunch stdout is filtered out — only meaningful
    # events reach the operator.
    _VISIBLE_LOG_LEVELS = {'INFO', 'WARN', 'ERROR', 'BAG'}

    _LEVEL_COLORS = {
        'INFO':  None,        # default text colour
        'WARN':  WARNING,
        'ERROR': DANGER,
        'BAG':   TEXT,        # bag-closed summary — plain white text
    }

    def log(self, msg: str, level: str = 'INFO'):
        """Append a line to the on-screen console + scan's app.log.

        Levels not in `_VISIBLE_LOG_LEVELS` are dropped from the console
        but still go to app.log for post-mortem analysis.
        """
        # Always tee to app.log on disk so nothing is ever lost.
        self.ros.app_log(f'[{level}] {msg}')

        if level not in self._VISIBLE_LOG_LEVELS:
            return

        ts = datetime.datetime.now().strftime('%H:%M:%S')
        color = self._LEVEL_COLORS.get(level)
        if color:
            line = (f'<span style="color:{SUBTLE};">[{ts}]</span> '
                    f'<span style="color:{color}; font-weight:bold;">'
                    f'[{level}] {msg}</span>')
        else:
            line = (f'<span style="color:{SUBTLE};">[{ts}]</span> '
                    f'<span>{msg}</span>')
        self.console.append(line)

    def _on_ros_log(self, msg: str, level: str):
        """ROS controller signal — routed through `log()` which drops the
        noisy 'ROS' level by default but keeps WARN/ERROR through."""
        self.log(msg, level=level)

    # ── Close confirmation (called by shell when scanning) ──────────────────

    def request_close_with_confirm(self, on_confirm=None):
        if hasattr(self, '_overlay') and self._overlay.isVisible():
            self._overlay.raise_()
            return
        self._overlay = _OverlayConfirm(self)
        self._overlay.setGeometry(self.rect())
        self._overlay.show()
        self._overlay.raise_()
        def _confirm():
            self._overlay.hide()
            self._stop_scan(after=on_confirm)
        self._overlay.yes_clicked.connect(_confirm)
        self._overlay.no_clicked.connect(self._overlay.hide)


# ── Overlays ─────────────────────────────────────────────────────────────────

class _OverlayConfirm(QWidget):
    yes_clicked = Signal()
    no_clicked  = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        card = QFrame(self)
        card.setFixedSize(380, 200)
        card.setStyleSheet(
            f'QFrame {{ background-color: {BG}; border: 2px solid {PRIMARY};'
            f'border-radius: 12px; }}'
        )

        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 22, 28, 20)
        layout.setSpacing(14)

        title_lbl = QLabel('Scan in progress')
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(
            f'color: {PRIMARY}; font-size: 13pt; font-weight: bold; border: none;')
        layout.addWidget(title_lbl)

        msg_lbl = QLabel('A scan is running.\nStop it and continue?')
        msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg_lbl.setStyleSheet(f'color: {TEXT}; font-size: 11pt; border: none;')
        layout.addWidget(msg_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)

        no_btn = QPushButton('No')
        no_btn.setMinimumHeight(40)
        no_btn.setStyleSheet(
            f'QPushButton {{ background-color: {PRIMARY}; color: white; border: none;'
            f'  border-radius: 8px; font-size: 11pt; font-weight: bold; }} '
            f'QPushButton:hover {{ background-color: {PRIMARY_DARK}; }}'
        )
        no_btn.clicked.connect(self.no_clicked)

        yes_btn = QPushButton('Yes, Stop')
        yes_btn.setMinimumHeight(40)
        yes_btn.setStyleSheet(
            f'QPushButton {{ background-color: {DANGER}; color: white; border: none;'
            f'  border-radius: 8px; font-size: 11pt; font-weight: bold; }} '
            f'QPushButton:hover {{ background-color: {DANGER_DARK}; }}'
        )
        yes_btn.clicked.connect(self.yes_clicked)

        btn_row.addWidget(no_btn)
        btn_row.addWidget(yes_btn)
        layout.addLayout(btn_row)

        self._card = card

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._card.move(
            (self.width()  - self._card.width())  // 2,
            (self.height() - self._card.height()) // 2,
        )

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 150))


class _AlertOverlay(QWidget):
    def __init__(self, parent, title: str, message: str):
        super().__init__(parent)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        # Wayland / RPi touch quirk: child-widget overlays sometimes don't
        # synthesize tap → click unless we explicitly claim touch events.
        # WA_AcceptTouchEvents lets Qt route the touch through this widget
        # tree; AA_SynthesizeMouseForUnhandledTouchEvents (set app-wide in
        # main.py) then converts unhandled touches into clicks on the OK
        # button below.
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)

        card = QFrame(self)
        # Card is sized generously so multi-line QA reasons (one bullet
        # per failing driver, up to ~3) fit without clipping. Fixed
        # size keeps the centring math in resizeEvent simple.
        card.setFixedSize(560, 340)
        card.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        card.setStyleSheet(
            f'QFrame {{ background-color: {BG}; border: 2px solid {DANGER};'
            f'border-radius: 12px; }}'
        )

        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 22, 28, 20)
        layout.setSpacing(12)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(
            f'color: {DANGER}; font-size: 14pt; font-weight: bold; border: none;')
        layout.addWidget(title_lbl)

        # Multi-line QA reasons render best left-aligned with a
        # monospace font so count/threshold columns line up. Plain
        # single-line reasons still look fine in this style.
        msg_lbl = QLabel(message)
        msg_lbl.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        msg_lbl.setWordWrap(True)
        msg_lbl.setTextFormat(Qt.TextFormat.PlainText)
        msg_lbl.setStyleSheet(
            f'color: {TEXT}; font-size: 11pt; border: none;'
            f'font-family: "DejaVu Sans Mono", "Liberation Mono",'
            f' Consolas, monospace;')
        layout.addWidget(msg_lbl)

        hint_lbl = QLabel('Scan has been stopped. Use Next Scan to continue.')
        hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_lbl.setWordWrap(True)
        hint_lbl.setStyleSheet(f'color: {SUBTLE}; font-size: 11pt; border: none;')
        layout.addWidget(hint_lbl)

        ok_btn = QPushButton('OK')
        # Touch-friendly target: 52 px is comfortably above the 44 px
        # Apple/Google minimum, and the cursor: pointer styling gives
        # the kiosk operator a visual cue that this is tappable.
        ok_btn.setMinimumHeight(52)
        ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ok_btn.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        ok_btn.setStyleSheet(
            f'QPushButton {{ background-color: {DANGER}; color: white; border: none;'
            f'  border-radius: 8px; font-size: 13pt; font-weight: bold; padding: 6px 24px; }} '
            f'QPushButton:hover {{ background-color: {DANGER_DARK}; }} '
            f'QPushButton:pressed {{ background-color: {DANGER_DARK}; }}'
        )
        ok_btn.clicked.connect(self.deleteLater)
        layout.addWidget(ok_btn)
        # Give the OK button focus on show so keyboard / synthesized
        # events have a guaranteed target.
        self._ok_btn = ok_btn

        self._card = card

    def showEvent(self, event):
        super().showEvent(event)
        # Touch + keyboard both need a focus target; without this,
        # synthesized tap-to-click events can arrive with no focused
        # widget and Qt silently drops them on some Wayland builds.
        self._ok_btn.setFocus()
        self.raise_()
        self.activateWindow()

    def mousePressEvent(self, event):
        # Swallow taps on the dim backdrop (anywhere outside the card)
        # so they don't fall through to the page behind — the overlay
        # is modal in intent.
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._card.move(
            (self.width()  - self._card.width())  // 2,
            (self.height() - self._card.height()) // 2,
        )

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 150))


class _StoppingOverlay(QWidget):
    """No buttons — hidden when `_finish_stop` runs."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        card = QFrame(self)
        card.setFixedSize(360, 140)
        card.setStyleSheet(
            f'QFrame {{ background-color: {BG}; border: 2px solid {PRIMARY};'
            f' border-radius: 12px; }}'
        )

        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 22, 28, 20)
        layout.setSpacing(10)

        self._title_lbl = QLabel('Stopping and saving scan')
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setStyleSheet(
            f'color: {PRIMARY}; font-size: 14pt; font-weight: bold; border: none;')
        layout.addWidget(self._title_lbl)

        hint_lbl = QLabel('Please wait — do not close the window.')
        hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_lbl.setStyleSheet(
            f'color: {LABEL}; font-size: 10pt; border: none;')
        layout.addWidget(hint_lbl)

        self._card = card
        self._dot_count = 0
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.start(400)

    def _tick(self):
        self._dot_count = (self._dot_count + 1) % 4
        dots = '.' * self._dot_count
        self._title_lbl.setText(f'Stopping and saving scan{dots}')

    def hide(self):
        self._anim_timer.stop()
        super().hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._card.move(
            (self.width()  - self._card.width())  // 2,
            (self.height() - self._card.height()) // 2,
        )

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 150))


class _StartupGuide(QWidget):
    """Three-phase startup guidance shown right after Scan Start.

        0–45 s : STAY STILL              (IMU static initialisation)
       45–60 s : ROTATE — STAY IN PLACE  (rotate the scanner without walking)
       60–65 s : START MOVING            (then fades out)

    Click-through so the Stop button stays reachable underneath.
    """

    finished = Signal()   # emitted once the fade-out completes

    # (title, subtitle, accent_color_key, duration_sec)
    _PHASES = [
        ('STAY STILL',
         'Keep the scanner perfectly still while IMU initialises.',
         'primary', 45),
        ('ROTATE — STAY IN PLACE',
         'Rotate the scanner slowly. Do not move from your position.',
         'warning', 20),
        ('START MOVING',
         'You may begin walking the scan path.',
         'success', 3),
    ]

    def __init__(self, parent):
        super().__init__(parent)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        card = QFrame(self)
        card.setFixedSize(560, 240)
        self._card = card

        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(12)

        self._title = QLabel()
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle = QLabel()
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle.setWordWrap(True)
        self._countdown = QLabel()
        self._countdown.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addStretch(1)
        layout.addWidget(self._title)
        layout.addWidget(self._subtitle)
        layout.addWidget(self._countdown)
        layout.addStretch(1)

        self._color_map = {
            'primary': PRIMARY,
            'warning': WARNING,
            'success': SUCCESS,
        }

        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(1.0)

        self._phase_idx  = -1
        self._secs_left  = 0
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)

        self._enter_phase(0)
        self._tick_timer.start(1000)

    def _enter_phase(self, idx: int):
        self._phase_idx = idx
        title, sub, color_key, dur = self._PHASES[idx]
        color = self._color_map[color_key]

        self._card.setStyleSheet(
            f'QFrame {{ background-color: {BG}; border: 3px solid {color};'
            f'border-radius: 14px; }}'
        )
        self._title.setStyleSheet(
            f'color: {color}; font-size: 28pt; font-weight: bold; border: none;')
        self._subtitle.setStyleSheet(
            f'color: {TEXT}; font-size: 12pt; border: none;')
        self._countdown.setStyleSheet(
            f'color: {SUBTLE}; font-size: 11pt; border: none;')

        self._title.setText(title)
        self._subtitle.setText(sub)
        self._secs_left = dur
        self._update_countdown()

    def _update_countdown(self):
        if self._phase_idx == len(self._PHASES) - 1:
            self._countdown.setText('')
            return
        self._countdown.setText(f'{self._secs_left}s remaining')

    def _tick(self):
        self._secs_left -= 1
        if self._secs_left > 0:
            self._update_countdown()
            return
        if self._phase_idx + 1 < len(self._PHASES):
            self._enter_phase(self._phase_idx + 1)
            return
        self._fade_out()

    def _fade_out(self):
        self._tick_timer.stop()
        anim = QPropertyAnimation(self._opacity, b'opacity', self)
        anim.setDuration(700)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        anim.finished.connect(self._on_faded)
        anim.start()
        self._fade_anim = anim   # keep alive until finished

    def _on_faded(self):
        self.hide()
        self.deleteLater()

    def cancel(self):
        """Tear down immediately if the scan stops mid-sequence."""
        self._tick_timer.stop()
        self.hide()
        self.deleteLater()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._card.move(
            (self.width()  - self._card.width())  // 2,
            (self.height() - self._card.height()) // 2,
        )

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 130))
