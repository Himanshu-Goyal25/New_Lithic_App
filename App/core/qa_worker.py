"""Real-time QA monitor for an active scan.

Three checks run in parallel from scan start until stop:

  1. **Watchdog** (every 10 s) — for each non-seek driver, is its ROS
     node alive in `rosnode list`? A driver that never came up within
     30 s, or that disappeared mid-scan, terminates the scan.

  2. **Disk** (every 10 s) — terminate if free space on the dump
     volume drops below `cfg.MIN_DISK_GB`.

  3. **Bag check** (on each `.bag.active → .bag` rename, via
     QFileSystemWatcher) — open the just-closed bag, read its per-
     topic message counts, and compare them against the thresholds
     in `cfg.DRIVERS`. The 0-th bag is partial because rosbag starts
     mid-window so per-30 s thresholds don't apply; only MISSING
     (count = 0) on a non-seek topic is fatal at bag 0. From bag 1
     onward both LOW (≥ 0 but below threshold − buffer) and MISSING
     are fatal.

Seek topics, if present, are always non-fatal (the camera is
optional). With seek currently disabled in config, they're not part
of cfg.DRIVERS so the per-bag loop just doesn't see them.
"""

import os
import shutil
import time
import glob
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, QFileSystemWatcher, Signal

import config as cfg


# Topic-to-label mapping for QA output (covers both currently-active
# topics and the optional seek topics for when those come back).
_LABEL = {
    '/hesai/pandar':                          'LiDAR',
    '/imu/data':                              'IMU',
    '/seek_camera/displayImage':              'Thermal Display',
    '/seek_camera/temperatureImageCelcius':   'Thermal Temp',
}


def _buffer_for(topic: str) -> int:
    """Pick the right entry from cfg.BUFFER for a topic — driver-aware."""
    if '/pandar' in topic:
        return cfg.BUFFER.get('lidar', 0)
    if '/imu' in topic:
        return cfg.BUFFER.get('imu', 0)
    if '/seek' in topic:
        return cfg.BUFFER.get('seek', 0)
    return 0


class QAWorker(QObject):
    """Real-time per-scan QA. Emits `terminate(reason)` when the scan
    should be auto-stopped, plus `bag_checked(results)` after each bag
    file closes so the player can surface a summary."""

    terminate    = Signal(str)            # reason string
    bag_checked  = Signal(list)           # list of {label, topic, count, threshold, status}
    log          = Signal(str, str)       # (message, level)

    # ── tuning constants ──────────────────────────────────────────────
    WATCHDOG_INTERVAL_MS = 10_000          # how often the watchdog fires
    DISK_INTERVAL_MS     = 10_000
    NEVER_STARTED_TIMEOUT_S = 30            # driver-never-up timeout
    DEBOUNCE_TICKS       = 2                # consecutive missed ticks before terminate

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scan_folder      = ''
        self._start_time       = 0.0
        self._bag_index        = 0
        self._ros_ctrl         = None
        self._driver_fail_n: dict = {}     # driver -> consecutive missed ticks
        self._driver_ever_up: dict = {}    # driver -> bool

        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.setInterval(self.WATCHDOG_INTERVAL_MS)
        self._watchdog_timer.timeout.connect(self._check_watchdog)

        self._disk_timer = QTimer(self)
        self._disk_timer.setInterval(self.DISK_INTERVAL_MS)
        self._disk_timer.timeout.connect(self._check_disk)

        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_dir_changed)

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self, scan_folder: str, ros_ctrl):
        self._scan_folder    = scan_folder
        self._start_time     = time.time()
        self._bag_index      = 0
        self._ros_ctrl       = ros_ctrl
        self._driver_fail_n  = {d: 0 for d in cfg.DRIVERS}
        self._driver_ever_up = {d: False for d in cfg.DRIVERS}
        self._seen_bags: set = set()

        if os.path.exists(scan_folder):
            self._watcher.addPath(scan_folder)

        self._watchdog_timer.start()
        self._disk_timer.start()

    def stop(self):
        self._watchdog_timer.stop()
        self._disk_timer.stop()
        if self._scan_folder:
            try:
                self._watcher.removePath(self._scan_folder)
            except Exception:
                pass
        self._ros_ctrl = None

    # ── watchdog (driver-node liveness) ─────────────────────────────────
    def _check_watchdog(self):
        if cfg.DEV_MODE:
            self.log.emit('QA: drivers OK (DEV_MODE)', 'INFO')
            return

        if not self._ros_ctrl:
            return

        if not self._ros_ctrl.is_running():
            self.log.emit('roslaunch process died', 'ERROR')
            self.terminate.emit('roslaunch process died')
            return

        elapsed = time.time() - self._start_time
        # Seek isn't fatal — kept here so we still log it if present.
        seek_drivers = {'seek'}

        for driver in cfg.DRIVERS:
            try:
                alive = self._ros_ctrl.driver_live(driver)
            except Exception:
                alive = False

            label = _DRIVER_LABEL.get(driver, driver.upper())

            if alive:
                self._driver_ever_up[driver] = True
                self._driver_fail_n[driver]  = 0
                continue

            is_seek = driver in seek_drivers

            # rosbag isn't a "publisher" — it's the recorder. Different
            # phrasing so the alert overlay is unambiguous for operators.
            is_recorder = (driver == 'rosbag')
            never_msg   = (
                f'{label} never started — no bags will be written '
                f'({self.NEVER_STARTED_TIMEOUT_S} s timeout)'
                if is_recorder
                else f'{label} never started '
                     f'({self.NEVER_STARTED_TIMEOUT_S} s timeout)'
            )
            dead_msg    = (
                f'{label} died — recording has stopped'
                if is_recorder
                else f'{label} stopped publishing'
            )

            if not self._driver_ever_up[driver]:
                # Driver hasn't been seen yet. Give it NEVER_STARTED_TIMEOUT_S
                # to come up before declaring it failed.
                if elapsed > self.NEVER_STARTED_TIMEOUT_S:
                    self.log.emit(never_msg, 'ERROR')
                    if not is_seek:
                        self.terminate.emit(never_msg)
                        return
            else:
                # Driver was up, now isn't. Debounce so a transient
                # rosnode hiccup doesn't kill an otherwise-good scan.
                self._driver_fail_n[driver] += 1
                n = self._driver_fail_n[driver]
                self.log.emit(
                    f'{label} not responding ({n}/{self.DEBOUNCE_TICKS})',
                    'WARN')
                if n >= self.DEBOUNCE_TICKS:
                    self.log.emit(dead_msg, 'ERROR')
                    if not is_seek:
                        self.terminate.emit(dead_msg)
                        return

    # ── disk check ──────────────────────────────────────────────────────
    # We check TWO disks every tick:
    #   1. cfg.DUMP_PATH      — where rosbag is writing scan data (NVMe).
    #      Filling this stops recording but the OS keeps running.
    #   2. '/'                — the root filesystem (SD card on this Pi).
    #      Filling this is far worse: bash can't fork, systemd can't
    #      write its journal, the kernel runs out of inotify/anonymous
    #      memory — the entire OS goes catatonic. ROS session logs
    #      (~/.ros/log/*), /tmp, journald, and process memory all live
    #      on `/`, so a long scan can silently fill it.
    _ROOT_MIN_GB = 1.5    # absolute minimum free on /
    _ROOT_FS    = '/'

    def _check_disk(self):
        if cfg.DEV_MODE:
            return
        try:
            free_gb = shutil.disk_usage(cfg.DUMP_PATH).free / 1e9
        except Exception:
            free_gb = None
        if free_gb is not None and free_gb < cfg.MIN_DISK_GB:
            msg = f'Disk space critical on data drive: {free_gb:.1f} GB free'
            self.log.emit(msg, 'ERROR')
            self.terminate.emit(msg)
            return

        try:
            root_free_gb = shutil.disk_usage(self._ROOT_FS).free / 1e9
        except Exception:
            return
        if root_free_gb < self._ROOT_MIN_GB:
            msg = (f'Root filesystem critical: {root_free_gb:.1f} GB free '
                   f'on {self._ROOT_FS}. Stopping before OS becomes '
                   f'unresponsive. Free space on the SD card / boot drive.')
            self.log.emit(msg, 'ERROR')
            self.terminate.emit(msg)

    # ── bag check (per-bag thresholds) ─────────────────────────────────
    def _on_dir_changed(self, path):
        bags = set(glob.glob(os.path.join(path, '*.bag')))
        new_bags = bags - self._seen_bags
        for bag_path in sorted(new_bags):
            self._seen_bags.add(bag_path)
            self._check_bag(bag_path)

    def _check_bag(self, bag_path: str):
        idx = self._bag_index
        self._bag_index += 1

        try:
            import rosbag
            with rosbag.Bag(bag_path, 'r') as bag:
                info = bag.get_type_and_topic_info()
                counts = {k: v.message_count for k, v in info.topics.items()}
        except Exception as e:
            self.log.emit(f'QA: cannot open bag for check: {e}', 'WARN')
            return

        results = []
        offenders = []   # detail lines for the terminate reason

        for driver, topics in cfg.DRIVERS.items():
            is_seek = (driver == 'seek')
            for topic, threshold in topics.items():
                buf   = _buffer_for(topic)
                count = counts.get(topic, 0)
                label = _LABEL.get(topic, topic)

                if count >= threshold:
                    status = 'OK'
                elif count >= threshold - buf:
                    status = 'WARN'
                elif count > 0:
                    status = 'LOW'
                else:
                    status = 'MISSING'

                results.append({
                    'label':     label,
                    'topic':     topic,
                    'count':     count,
                    'threshold': threshold,
                    'status':    status,
                })

                # Per-topic status lines are emitted by the bag-rotation
                # log in scan_player (see `_check_bag_rotation`), which
                # renders the same count/expect/status row in white text.
                # We only need to drive the terminate decision here.

                # Termination rules:
                #   - Seek topics never trigger termination
                #   - MISSING (count = 0) always fatal on non-seek
                #   - LOW fatal only from bag 1 onward (bag 0 is partial)
                if not is_seek and status in ('LOW', 'MISSING'):
                    if status == 'MISSING' or idx > 0:
                        offenders.append(
                            f'{label} {count:,}/{threshold:,} [{status}]')

        self.bag_checked.emit(results)

        if offenders:
            # Build a reason string the operator can act on. The
            # _AlertOverlay's body is word-wrapped, so newlines are
            # safe and improve readability for multiple offenders.
            detail = '\n'.join(f'  • {o}' for o in offenders)
            reason = (f'Bag {idx} QA failed — data below threshold:\n'
                      f'{detail}')
            self.log.emit(reason, 'ERROR')
            self.terminate.emit(reason)


# Display label per driver (kept module-local for use by watchdog).
_DRIVER_LABEL = {
    'hesai':  'LiDAR',
    'xsens':  'IMU',
    'seek':   'Thermal',
    'rosbag': 'Recorder',
}
