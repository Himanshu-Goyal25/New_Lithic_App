"""Shared device-status widget — shows LiDAR / IMU / Thermal health.

Architecture
------------
ONE singleton `DeviceMonitor` runs the polling loop and opens the serial
port. Every `DeviceStatusPanel` (Home banner + Scan Setup sidebar)
subscribes to its `results_changed` signal and renders the most recent
result. Panels never probe themselves, so they can't race for the port.

  DeviceMonitor.instance()
   │
   ├─ QTimer 5 s ─┐
   ├─ refresh_now()─┤   single thread → _actually_check()
   │              └─→ results_changed.emit(results)
   │
   ├─ DeviceStatusPanel (Home banner)
   └─ DeviceStatusPanel (Scan Setup sidebar)

For this device:
  - LiDAR   : Hesai (ICMP ping)
  - IMU     : Xsens MTi via FTDI UART (lsusb + open serial + look for 0xFA 0xFF)
  - Thermal : Seek (lsusb vendor ID)
"""

import os
import subprocess
import threading
import time

from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
)
from PySide6.QtCore import Qt, QTimer, Signal, QObject

import config
from gui.main_window import (
    PRIMARY, PRIMARY_DARK, LABEL, SUBTLE, BORDER, PANEL_BG, SUCCESS, DANGER,
)

READY_COLOR = SUCCESS
FAIL_COLOR  = DANGER

_DEVICES = [
    ('lidar',   'LiDAR'),
    ('imu',     'IMU'),
    # ('thermal', 'Thermal'),  # disabled — no thermal camera right now
]


# ── Singleton monitor ──────────────────────────────────────────────────────

class DeviceMonitor(QObject):
    """The only thing that actually probes the hardware.

    Every panel subscribes to `results_changed`. Probes run in a worker
    thread; the signal delivers the result back to the GUI thread.
    """

    results_changed = Signal(dict)     # {key: (ok, text)}

    _instance: 'DeviceMonitor | None' = None

    @classmethod
    def instance(cls) -> 'DeviceMonitor':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self._tick)
        self._busy = False
        self._paused = False           # set True while a scan is running
        self._latest: dict = {}
        self._subscribers = 0

    # ── lifecycle ──────────────────────────────────────────────────────────
    def attach(self):
        """Called by every panel on construction. Starts the timer on the
        first attach, idempotent for subsequent ones."""
        self._subscribers += 1
        if not self._timer.isActive():
            self._timer.start()
            # Kick off an immediate probe so the first attach doesn't have
            # to wait 5 s for fresh data.
            self._tick()

    def detach(self):
        """Optional — counterpart to attach(). Currently we leave the
        timer running for the life of the app; this hook exists in case
        we ever want to stop polling when all panels are destroyed."""
        self._subscribers = max(0, self._subscribers - 1)
        # Intentionally do not stop the timer — the next panel construction
        # would just start it again, and the panel-build pattern destroys
        # and re-creates child widgets on theme change.

    # ── public API ─────────────────────────────────────────────────────────
    @property
    def latest(self) -> dict:
        """Read-only snapshot of the last probe result."""
        return dict(self._latest)

    def refresh_now(self):
        """Trigger an immediate probe (from the Refresh button)."""
        if self._paused:
            return
        self._tick()

    def pause(self):
        """Stop polling and wait for any in-flight probe to finish.

        Called by scan_player when a scan starts. The IMU probe opens
        /dev/ttyUSB0 to read the Xsens MTi preamble; while it's open,
        the xsens_mti_node driver shares the UART byte-stream with us
        and loses ~9 IMU msgs per probe. Six probes over a 30-s bag
        = ~54 lost msgs — exactly the gap we kept seeing.

        Blocks for up to ~2 s if a probe is in flight, to make sure
        /dev/ttyUSB0 is fully released before the driver opens it.
        """
        self._paused = True
        if self._timer.isActive():
            self._timer.stop()
        # Synchronously wait for any in-flight probe to release the
        # serial port. The probe thread sets self._busy = False when it
        # returns (worst case ~1.2 s for the serial timeout).
        deadline = time.time() + 2.0
        while self._busy and time.time() < deadline:
            time.sleep(0.05)

    def resume(self):
        """Resume polling after a scan. Triggers one immediate probe
        so panels don't keep showing stale data from before the scan."""
        self._paused = False
        if self._subscribers > 0 and not self._timer.isActive():
            self._timer.start()
            self._tick()

    # ── internals ──────────────────────────────────────────────────────────
    def _tick(self):
        # Re-entrancy guard: if a probe is already in flight, skip this tick.
        # This is what makes the design race-free — we never have two
        # concurrent reads from /dev/ttyUSB0.
        if self._busy or self._paused:
            return
        self._busy = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            results = _actually_check()
        except Exception as e:
            results = {k: (False, f'Probe error: {e}') for k, _ in _DEVICES}
        finally:
            self._busy = False
        self._latest = results
        # Cross-thread signal emit — Qt automatically uses QueuedConnection
        # because subscribers (the panels) live in the GUI thread.
        self.results_changed.emit(results)


# ── Panel ──────────────────────────────────────────────────────────────────

class DeviceStatusPanel(QFrame):
    """Reusable device health panel — just a view over DeviceMonitor.

    Parameters
    ----------
    horizontal : bool
        Horizontal row (Home banner) vs vertical column (Scan Setup sidebar).
    """

    def __init__(self, horizontal: bool = False, parent=None):
        super().__init__(parent)
        self._horizontal = horizontal
        self._dev_rows = {}

        if horizontal:
            self.setStyleSheet(
                f'DeviceStatusPanel {{ background-color: {PANEL_BG}; '
                f'border: 1px solid {BORDER}; border-radius: 12px; }} '
                'QLabel { background: transparent; border: 0; }'
            )
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:
            self.setStyleSheet(
                'DeviceStatusPanel { background: transparent; border: 0; } '
                'QLabel { background: transparent; border: 0; }'
            )

        self._build_ui()

        # Subscribe to the shared monitor + start it.
        monitor = DeviceMonitor.instance()
        monitor.results_changed.connect(
            self._apply_device_results, Qt.ConnectionType.QueuedConnection)
        monitor.attach()

        # If the monitor has a fresh result already, paint it immediately
        # so the panel never spends 5 seconds stuck on "Checking…".
        if monitor.latest:
            self._apply_device_results(monitor.latest)

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        if self._horizontal:
            self._build_horizontal()
        else:
            self._build_vertical()

    def _build_vertical(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        hdr = QLabel('DEVICES')
        hdr.setStyleSheet(
            f'color: {PRIMARY}; font-size: 10pt; font-weight: bold;')
        layout.addWidget(hdr)

        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setFixedHeight(1)
        div.setStyleSheet(f'background-color: {BORDER}; border: 0;')
        layout.addWidget(div)

        for key, label in _DEVICES:
            row = QHBoxLayout()
            row.setSpacing(8)
            row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

            dot = QLabel('●')
            dot.setFixedWidth(14)
            dot.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter)
            dot.setStyleSheet(f'color: {SUBTLE}; font-size: 14px;')

            name_lbl = QLabel(label)
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
            name_lbl.setStyleSheet(f'color: {LABEL}; font-size: 11pt; font-weight: bold;')

            status_lbl = QLabel('Checking…')
            status_lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
            status_lbl.setStyleSheet(f'color: {SUBTLE}; font-size: 10pt;')

            row.addWidget(dot)
            row.addWidget(name_lbl)
            row.addStretch()
            row.addWidget(status_lbl)
            layout.addLayout(row)
            self._dev_rows[key] = (dot, status_lbl)

        layout.addStretch()
        layout.addWidget(self._make_refresh_btn())

    def _build_horizontal(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(18)

        hdr = QLabel('DEVICES')
        hdr.setStyleSheet(f'color: {PRIMARY}; font-size: 9pt; font-weight: bold;')
        layout.addWidget(hdr)

        for i, (key, label) in enumerate(_DEVICES):
            if i > 0:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.VLine)
                sep.setFixedWidth(1)
                sep.setStyleSheet(f'background-color: {BORDER}; border: 0;')
                layout.addWidget(sep)

            tile = QHBoxLayout()
            tile.setSpacing(8)
            tile.setAlignment(Qt.AlignmentFlag.AlignVCenter)

            dot = QLabel('●')
            dot.setFixedWidth(14)
            dot.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter)
            dot.setStyleSheet(f'color: {SUBTLE}; font-size: 14px;')
            tile.addWidget(dot)

            col = QVBoxLayout()
            col.setSpacing(1)

            name_lbl = QLabel(label)
            name_lbl.setStyleSheet(
                f'color: {LABEL}; font-size: 11pt; font-weight: bold;')
            col.addWidget(name_lbl)

            status_lbl = QLabel('Checking…')
            status_lbl.setStyleSheet(f'color: {SUBTLE}; font-size: 11pt;')
            col.addWidget(status_lbl)
            tile.addLayout(col)

            layout.addLayout(tile)
            self._dev_rows[key] = (dot, status_lbl)

        layout.addStretch()
        layout.addWidget(self._make_refresh_btn())

    def _make_refresh_btn(self) -> QPushButton:
        btn = QPushButton('↻  Refresh')
        btn.setFixedHeight(30)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f'QPushButton {{ background-color: {PRIMARY}; color: white;'
            f'  border-radius: 6px; font-size: 10pt; font-weight: bold;'
            f'  padding: 0 14px; border: none; }} '
            f'QPushButton:hover {{ background-color: {PRIMARY_DARK}; }}'
        )
        # Refresh asks the shared monitor to probe now — every subscriber
        # gets the result via the same signal path.
        btn.clicked.connect(self._on_refresh_clicked)
        return btn

    def _on_refresh_clicked(self):
        # Visual hint while the probe is in flight (max ~1.2 s).
        for key in self._dev_rows:
            dot, status_lbl = self._dev_rows[key]
            dot.setStyleSheet(f'color: {SUBTLE}; font-size: 14px;')
            status_lbl.setText('Checking…')
        DeviceMonitor.instance().refresh_now()

    # ── result rendering ───────────────────────────────────────────────────

    def _apply_device_results(self, results: dict):
        for key, (ok, text) in results.items():
            if key not in self._dev_rows:
                continue
            dot, status_lbl = self._dev_rows[key]
            color = READY_COLOR if ok else FAIL_COLOR
            dot.setStyleSheet(f'color: {color}; font-size: 14px;')
            status_lbl.setText(text)
            status_lbl.setStyleSheet(
                f'color: {color}; font-size: 11pt; font-weight: bold;')


# ── Probing logic (pure functions) ──────────────────────────────────────────

def _host_has_ip(ip: str) -> bool:
    try:
        out = subprocess.check_output(
            ['ip', '-o', 'addr', 'show'],
            timeout=3, stderr=subprocess.DEVNULL).decode()
        return any(
            part == ip or part.startswith(ip + '/')
            for line in out.splitlines()
            for part in line.split()
        )
    except Exception:
        return True


def _lsusb_has_vid(vid: str) -> bool:
    try:
        out = subprocess.check_output(
            ['lsusb'], timeout=3, stderr=subprocess.DEVNULL).decode().lower()
        return f'id {vid.lower()}:' in out
    except Exception:
        return False


def _probe_xsens_imu():
    """Three-tier IMU check.

    1. FTDI USB cable enumerated?           (cheap)
    2. /dev/ttyUSBn exists?                  (cheap)
    3. Xsens MTi preamble 0xFA 0xFF visible  (open serial 1 s — definitive)

    Tier 3 is the real test: an FTDI cable plugged in with no IMU on the
    other end still passes tiers 1+2. Reading the Xsens packet preamble
    proves the IMU is actually transmitting.
    """
    # Tier 1: FTDI cable on the USB bus
    if not _lsusb_has_vid(config.XSENS_FTDI_VID):
        return (False, 'FTDI not found')

    # Tier 2: TTY device node exists
    port = config.XSENS_SERIAL_PORT
    if not os.path.exists(port):
        return (False, 'No serial port')

    # Tier 3: protocol-level — is the IMU actually streaming?
    try:
        import serial
    except ImportError:
        return (True, 'Cable OK (install pyserial)')

    try:
        with serial.Serial(port, config.XSENS_SERIAL_BAUD, timeout=1.2) as ser:
            ser.reset_input_buffer()
            data = ser.read(1024)
            if len(data) == 0:
                return (False, 'Silent — IMU not transmitting')
            if b'\xfa\xff' in data:
                return (True, 'Ready')
            return (False, f'No MTi preamble ({len(data)}B @ {config.XSENS_SERIAL_BAUD})')
    except PermissionError:
        # Another process has the port (likely the ROS driver running).
        # Treat as "running fine" rather than "not found".
        return (True, 'Port in use')
    except OSError as e:
        return (False, f'Port error: {e.errno}')
    except Exception:
        return (False, 'Probe error')


def _actually_check() -> dict:
    """Run all three device checks. Called by DeviceMonitor on a worker
    thread — never call directly from the GUI thread (LiDAR ping + serial
    read can take up to ~4 s combined)."""
    if config.DEV_MODE:
        return {k: (True, 'DEV MODE') for k, _ in _DEVICES}

    results = {}

    # LiDAR (Hesai) — ICMP ping
    try:
        if not config.LIDAR_IP:
            results['lidar'] = (False, 'No IP configured')
        else:
            ret = subprocess.call(
                ['ping', '-c', '1', '-W', '1', config.LIDAR_IP],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            if ret != 0:
                results['lidar'] = (False, 'Unreachable')
            elif config.LIDAR_HOST_IP and not _host_has_ip(config.LIDAR_HOST_IP):
                results['lidar'] = (False, f'Host IP {config.LIDAR_HOST_IP} not set')
            else:
                results['lidar'] = (True, 'Ready')
    except Exception:
        results['lidar'] = (False, 'Error')

    # IMU (Xsens via FTDI) — FTDI + serial protocol check
    results['imu'] = _probe_xsens_imu()

    # Thermal disabled — re-enable by un-commenting the entry in _DEVICES
    # above and the lines below.
    # results['thermal'] = (
    #     (True, 'Ready') if _lsusb_has_vid(config.SEEK_USB_VID)
    #     else (False, 'Not found'))

    return results
