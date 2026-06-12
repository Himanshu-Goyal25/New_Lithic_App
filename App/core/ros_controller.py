"""ROS launch + subscription manager.

Public API matches the reference device's `RosController` so the port of
scan_player.py drops in unchanged:

  - ros = RosController()
  - ros.frame_received.connect(slot)        # signal(topic: str, image: QImage)
  - ros.launch_died.connect(slot)
  - ros.log.connect(slot)                   # signal(msg: str, level: str)
  - ros.launch(LAUNCH_FILE, args, metadata=...)
  - ros.subscribe(topic, msg_type)
  - ros.stop()                              # synchronous; callers may wrap in a thread
  - ros.app_log(msg)
  - ros.is_running()
  - ros.driver_live(driver_key)             # bool: rosnode for that driver is up
  - RosController.shutdown_roscore()        # class method, called at app exit
"""
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal, QThread
from PySide6.QtGui import QImage

import config as cfg

# rospy imported lazily so the app can still run with ROS env missing
_rospy = None
_Image = None
_node_started = False
_node_lock = threading.Lock()


def _lazy_import_rospy():
    global _rospy, _Image
    if _rospy is not None:
        return True
    try:
        import rospy
        from sensor_msgs.msg import Image
        _rospy = rospy
        _Image = Image
        return True
    except ImportError:
        return False


def _image_to_qimage(msg):
    """Convert sensor_msgs/Image to QImage. Returns None on failure."""
    try:
        import numpy as np
        data = bytes(msg.data)
        enc = (msg.encoding or '').lower()
        if enc in ('mono8', '8uc1'):
            arr = np.frombuffer(data, dtype='uint8').reshape(msg.height, msg.width).copy()
            return QImage(arr.data, msg.width, msg.height, msg.width,
                          QImage.Format.Format_Grayscale8).copy()
        if enc == 'rgb8':
            arr = np.frombuffer(data, dtype='uint8').reshape(msg.height, msg.width, 3).copy()
            return QImage(arr.data, msg.width, msg.height, msg.width * 3,
                          QImage.Format.Format_RGB888).copy()
        if enc == 'bgr8':
            arr = np.frombuffer(data, dtype='uint8').reshape(msg.height, msg.width, 3)
            arr = arr[:, :, ::-1].copy()
            return QImage(arr.data, msg.width, msg.height, msg.width * 3,
                          QImage.Format.Format_RGB888).copy()
        # Grayscale fallback
        arr = np.frombuffer(data, dtype='uint8')
        size = msg.height * msg.width
        if len(arr) >= size:
            arr = arr[:size].reshape(msg.height, msg.width).copy()
            return QImage(arr.data, msg.width, msg.height, msg.width,
                          QImage.Format.Format_Grayscale8).copy()
    except Exception:
        pass
    return None


class _ROSSpinThread(QThread):
    """Initialises rospy node, subscribes to topics, spins until stopped."""

    frame_received = Signal(str, QImage)   # topic, image
    log            = Signal(str, str)      # message, level

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_flag = False

    def stop(self):
        # Set the flag so the spin loop exits within ~100 ms.
        #
        # We deliberately DO NOT call _rospy.signal_shutdown() here:
        # signal_shutdown is a one-way switch — once tripped, rospy is
        # dead for the lifetime of this process, future subscribers
        # never receive callbacks, and re-init fails. That's exactly
        # what made "Next Scan" leave LiDAR/IMU stuck at dead/0 Hz.
        # Unregister of individual subs happens at the end of run().
        self._stop_flag = True

    def run(self):
        global _node_started

        # DEV_MODE: emit coloured mock frames for any view topics so
        # the player's video panel has something to render. Driver
        # liveness (LIVE/DEAD pills) is reported by _NodeMonitor in
        # the same RosController, which has its own DEV_MODE shortcut.
        if cfg.DEV_MODE:
            from PySide6.QtGui import QColor
            view_topics = list(cfg.VIEW_TOPIC.keys())
            self.log.emit('DEV_MODE: mock view-topic frames active', 'INFO')
            hue = 0
            while not self._stop_flag:
                for topic in view_topics:
                    img = QImage(320, 240, QImage.Format.Format_RGB888)
                    img.fill(QColor.fromHsv(hue % 360, 180, 100))
                    self.frame_received.emit(topic, img)
                hue += 5
                time.sleep(0.1)
            return

        # If there are no view topics at all, nothing to subscribe to.
        # Bail out without initialising rospy — driver liveness comes
        # from _NodeMonitor (rosnode list) and recording from rosbag,
        # neither of which need this thread.
        view_topics = set(cfg.VIEW_TOPIC.keys())
        if not view_topics:
            self.log.emit('Spin thread idle (no view topics)', 'INFO')
            while not self._stop_flag:
                time.sleep(0.5)
            return

        if not _lazy_import_rospy():
            self.log.emit('rospy not available — live feed disabled', 'WARN')
            return

        # Wait for rosmaster
        for _ in range(20):
            if self._stop_flag:
                return
            try:
                _rospy.get_master().getSystemState()
                break
            except Exception:
                time.sleep(0.5)
        else:
            self.log.emit('ROS master not reachable — live feed disabled', 'WARN')
            return

        with _node_lock:
            if not _node_started:
                try:
                    _rospy.init_node('lithic_collector', anonymous=True,
                                     disable_signals=True)
                    _node_started = True
                except Exception as e:
                    self.log.emit(f'rospy init failed: {e}', 'WARN')
                    return

        # Image-only subscribers — used for the thermal preview if/when
        # the seek camera is re-enabled. AnyMsg subscribers were dropped
        # entirely because driver liveness is reported by `rosnode list`
        # via _NodeMonitor, and rosbag handles message recording directly.
        # queue_size=2 is enough for a live preview: latest frame wins,
        # older ones are discarded.
        subs = []
        for topic in view_topics:
            def _img_cb(msg, t=topic):
                qimg = _image_to_qimage(msg)
                if qimg:
                    self.frame_received.emit(t, qimg)
            subs.append(_rospy.Subscriber(topic, _Image, _img_cb,
                                          queue_size=2))

        self.log.emit('ROS image subscriber active', 'INFO')

        while not self._stop_flag and not _rospy.is_shutdown():
            time.sleep(0.1)

        for s in subs:
            try:
                s.unregister()
            except Exception:
                pass


class _NodeMonitor:
    """Polls `rosnode list` periodically and reports which expected
    nodes are currently registered with the master.

    Much simpler and more reliable than the previous Hz approach —
    `rosnode list` is one subprocess call per second, the output is
    flushed cleanly (no pipe-buffering surprise), and "is the node
    running?" is exactly the question the operator is asking when
    they look at the status card.

    A node disappearing from the list means the driver process has
    exited (cleanly or otherwise) — which IS the failure mode the
    operator wants to see at a glance.
    """

    # Polling once per second cumulatively interferes enough with the
    # xsens UART read loop to cost ~50 msgs / 30-s bag — every probe
    # forks bash + sources setup.bash + opens an XML-RPC socket, which
    # hits shared kernel structures even though it runs on cores 2-3.
    # 5 s is more than fast enough for "is the driver alive" feedback.
    POLL_INTERVAL_S = 5.0
    LIVE_TIMEOUT_S  = 12.0    # 2 missed polls before flipping to DEAD

    def __init__(self):
        self._present: set    = set()
        self._last_ok: float  = 0.0    # epoch of last successful poll
        self._stop_flag       = False
        self._thread          = None
        self._lock            = threading.Lock()

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self):
        # DEV_MODE: mock all expected nodes as live.
        if cfg.DEV_MODE:
            with self._lock:
                self._present  = set(cfg.DRIVER_NODES.values())
                self._last_ok  = time.time()
            return
        self._stop_flag = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_flag = True
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        # Clear so the next launch starts from a clean slate.
        with self._lock:
            self._present.clear()
            self._last_ok = 0.0

    # ── poller ──────────────────────────────────────────────────────────
    def _loop(self):
        setup = os.path.expanduser(cfg.SETUP_BASH)
        cmd = f'source {setup} && rosnode list 2>/dev/null'
        while not self._stop_flag:
            try:
                r = subprocess.run(
                    ['bash', '-c', cmd],
                    capture_output=True, text=True, timeout=3,
                )
                if r.returncode == 0:
                    nodes = {n.strip() for n in r.stdout.splitlines() if n.strip()}
                    now = time.time()
                    with self._lock:
                        self._present = nodes
                        self._last_ok = now
            except Exception:
                # Master not up yet, or transient — try again next tick.
                pass
            # Sleep in small slices so stop() returns quickly.
            for _ in range(int(self.POLL_INTERVAL_S * 10)):
                if self._stop_flag:
                    return
                time.sleep(0.1)

    # ── queries ─────────────────────────────────────────────────────────
    def is_live(self, node_name: str) -> bool:
        """True iff the most recent successful poll showed this node
        AND that poll was recent (within LIVE_TIMEOUT_S)."""
        with self._lock:
            if time.time() - self._last_ok > self.LIVE_TIMEOUT_S:
                return False
            return node_name in self._present

    def present_nodes(self) -> set:
        with self._lock:
            return set(self._present)


class RosController(QObject):
    """Manages roslaunch subprocess and live ROS subscriptions."""

    frame_received = Signal(str, QImage)   # (topic, image)
    launch_died    = Signal()
    log            = Signal(str, str)      # (message, level)
    stop_done      = Signal()              # async stop finished

    # Class-level so a persistent roscore survives across launches /
    # RosController instances. roslaunch normally spawns its own master,
    # which then dies when we kill roslaunch — that's what broke the
    # second scan ("Next Scan" → all sensors stuck dead/0 Hz). Owning
    # roscore ourselves means killing roslaunch leaves the master alive,
    # and rospy reuses the existing connection.
    _roscore_proc = None   # subprocess.Popen for the detached roscore

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._thread = None
        self._monitor_thread = None
        self._monitor_stop = False
        self._stop_gen = 0
        self._launch_gen = 0       # bumped by stop() to cancel an in-flight launch
        self._launching = False    # True from launch() until roslaunch is spawned
        # Makes the launch worker's "check _launch_gen, then act" atomic
        # against stop()'s "bump _launch_gen". Held only for microsecond
        # operations (Popen / thread starts) — NEVER across sleeps or
        # joins, so it cannot deadlock.
        self._llock = threading.Lock()
        self._scan_folder = None
        self._app_log_path = None
        self._requested_topics = set()
        # Per-driver liveness comes from a single `rosnode list` poll
        # — simple, reliable, and matches what an operator would check
        # from a terminal. The spin thread is now only used for image
        # display (view topics).
        self._nodes = _NodeMonitor()

    # ----------------------------------------------------------------- launch
    def launch(self, launch_file: str, args: dict, metadata: dict | None = None):
        """Start roslaunch with the supplied {arg_name: value} dict.

        For this device the dict typically contains:
            'data_path':  '<scan folder>'
        """
        setup = os.path.expanduser(cfg.SETUP_BASH)
        launch_file = os.path.expanduser(launch_file)

        # Remember the scan folder so app_log() can write into it.
        data_path = args.get('data_path')
        if data_path:
            self._scan_folder  = data_path
            self._app_log_path = os.path.join(data_path, 'app.log')
            self._write_log_header(launch_file, args, metadata)

        arg_str = ' '.join(f'{k}:={v}' for k, v in args.items())
        cmd = f'source {setup} && roslaunch {launch_file} {arg_str}'

        # Heavy work (roscore bring-up can take ~10 s on a cold boot,
        # plus a 3 s settle before the spin thread) runs on a worker so
        # the GUI doesn't freeze between the Start tap and the player
        # becoming live. is_running() reports True while _launching so
        # the QA watchdog doesn't misread the bring-up window as
        # "roslaunch process died".
        self._launching  = True
        self._launch_gen += 1
        gen = self._launch_gen
        threading.Thread(
            target=self._launch_worker,
            args=(gen, launch_file, cmd, arg_str),
            daemon=True).start()

    def _launch_worker(self, gen: int, launch_file: str, cmd: str,
                       arg_str: str):
        try:
            if cfg.DEV_MODE:
                self.log.emit('DEV_MODE: skipping roslaunch', 'INFO')
            else:
                # Make sure a master is up BEFORE roslaunch starts, so it
                # joins our persistent master rather than spawning its own.
                # Otherwise stopping the scan kills roslaunch + its child
                # master and the next scan has nothing to connect to.
                self._ensure_roscore()

                # Atomic vs stop(): either we spawn roslaunch before
                # stop() bumps the gen (so its teardown sees and kills
                # the proc), or the check fails and nothing is spawned.
                with self._llock:
                    if gen != self._launch_gen:
                        return   # stop() was called while roscore came up

                    self.log.emit(
                        f'roslaunch {os.path.basename(launch_file)} '
                        f'{arg_str}',
                        'INFO')
                    # IMPORTANT: stdout/stderr go to /dev/null, NOT PIPE.
                    # PIPE+line-read in a Python thread can't drain fast
                    # enough when the drivers log heavily — once the 64 KB
                    # pipe buffer fills, any child trying to write blocks,
                    # which back-pressures into the xsens UART loop and
                    # drops it from 200 → ~190 Hz. roslaunch's own logger
                    # still writes per-session logs to ~/.ros/log/, so
                    # nothing is lost for post-mortem analysis.
                    self._proc = subprocess.Popen(
                        ['bash', '-c', cmd],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self._monitor_stop = False
                    self._monitor_thread = threading.Thread(
                        target=self._monitor_proc, daemon=True)
                    self._monitor_thread.start()
        finally:
            # roslaunch is spawned (or launch was aborted) — from here
            # is_running() reflects the real process state.
            self._launching = False

        # Begin polling `rosnode list` for per-driver liveness right
        # away — the LIVE/DEAD pills shouldn't wait out the spin-thread
        # settle below. Gen-guarded so a stop() that already tore the
        # monitor down can't be followed by a stale start().
        with self._llock:
            if gen != self._launch_gen:
                return
            self._nodes.start()

        # Spin thread is used ONLY for image display (view topics).
        # No QObject parent: this worker is not the thread the
        # controller lives in, and Qt forbids cross-thread parenting.
        # Built on a LOCAL so a concurrent stop() never sees a created-
        # but-unstarted thread; published to self._thread only under
        # the gen lock below.
        spin = _ROSSpinThread()
        spin.frame_received.connect(self.frame_received)
        spin.log.connect(self.log)
        if not cfg.DEV_MODE:
            QThread.sleep(3)   # settle — deliberately OUTSIDE the lock

        with self._llock:
            if gen != self._launch_gen:
                return   # stopped during the settle; discard unstarted spin
            self._thread = spin
            spin.start()

            # Mark each driver PID as OOM-protected so the kernel's
            # OOM-killer will never pick them under memory pressure.
            # Driver PIDs aren't all up the instant roslaunch returns,
            # so this polls in its own thread.
            if not cfg.DEV_MODE:
                threading.Thread(
                    target=self._oom_protect_drivers, daemon=True).start()

    # ── OOM protection ─────────────────────────────────────────────────
    def _oom_protect_drivers(self):
        """Background worker that does two things at once:

        1) Tails the roslaunch log for `process[X-N]: started with
           pid [Y]` lines and forwards them verbatim to the on-screen
           console. This is the canonical 'node started' signal —
           roslaunch's own statement that it forked and exec'd a
           child process successfully.

        2) For each data-critical process we recognise (drivers +
           rosbag), runs the lithic-oom-protect helper silently
           against its PID so the kernel's OOM-killer can't pick it
           under memory pressure.

        Both share one poll loop because both depend on the same
        source: roslaunch's per-session log file under
        ~/.ros/log/latest/. The file appears within ~1 s of launch
        and grows as nodes come up.
        """
        import re

        # Process-name prefixes (matched against roslaunch's
        # `process[<name>-<N>]` convention) that we want OOM-protected.
        # Auxiliary processes (pc2_to_laserscan / rosbridge / rosapi)
        # are deliberately omitted — they don't carry scan data.
        protect_prefixes = {
            'hesai_ros_driver_node',
            'xsens_mti_node',
            'rosbag_record',
        }
        if 'seek' in cfg.DRIVERS:
            protect_prefixes.add('seek_driver_node')

        re_started = re.compile(
            r'process\[([^\]]+)\]:\s*started with pid \[(\d+)\]')
        re_suffix  = re.compile(r'-\d+$')   # strip trailing "-N"

        start_time = time.time()
        deadline   = start_time + 60
        log_file   = None
        last_pos   = 0
        seen       = set()        # (name, pid) tuples already forwarded
        latest_dir = os.path.expanduser('~/.ros/log/latest')

        while time.time() < deadline:
            # Locate (or re-locate) the roslaunch log for this scan.
            # It appears within ~1 s of `roslaunch` being exec'd; we
            # pick the newest `roslaunch-*.log` whose mtime is after
            # we started watching.
            if log_file is None or not os.path.exists(log_file):
                try:
                    candidates = []
                    for f in os.listdir(latest_dir):
                        if not (f.startswith('roslaunch-')
                                and f.endswith('.log')):
                            continue
                        full = os.path.join(latest_dir, f)
                        try:
                            if os.path.getmtime(full) >= start_time - 3:
                                candidates.append(
                                    (os.path.getmtime(full), full))
                        except OSError:
                            pass
                    if candidates:
                        candidates.sort()
                        log_file = candidates[-1][1]
                        last_pos = 0
                except OSError:
                    pass

            # Tail any new content + match started-process lines.
            if log_file:
                try:
                    with open(log_file) as f:
                        f.seek(last_pos)
                        chunk = f.read()
                        last_pos = f.tell()
                except OSError:
                    chunk = ''
                new_pids_to_protect = []
                for line in chunk.splitlines():
                    m = re_started.search(line)
                    if not m:
                        continue
                    name, pid = m.group(1), m.group(2)
                    key = (name, pid)
                    if key in seen:
                        continue
                    seen.add(key)
                    # Forward to the console as a plain log line.
                    self.log.emit(
                        f'process[{name}]: started with pid [{pid}]',
                        'INFO')
                    # OOM-protect data-critical procs (silently).
                    base = re_suffix.sub('', name)
                    if base in protect_prefixes:
                        new_pids_to_protect.append(pid)
                if new_pids_to_protect:
                    try:
                        subprocess.run(
                            ['sudo', '-n',
                             '/usr/local/sbin/lithic-oom-protect',
                             *new_pids_to_protect],
                            timeout=3, check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
                    except Exception as e:
                        self.log.emit(
                            f'OOM protect failed: {e}', 'WARN')

            time.sleep(0.5)

    # Legacy alias — older code paths still call .start()
    def start(self, data_path: str):
        self.launch(cfg.LAUNCH_FILE, {'data_path': data_path})

    # ----------------------------------------------------------------- subscribe
    def subscribe(self, topic: str, _msg_type: str = 'Image'):
        """No-op shim — _ROSSpinThread subscribes to everything in DRIVERS
        automatically. We only track the requested set so future logic
        can ignore unrequested topics if needed."""
        self._requested_topics.add(topic)

    # ----------------------------------------------------------------- stop
    def stop(self):
        # Cancel any launch still in flight (operator stopped within
        # seconds of starting, while roscore was coming up). Under the
        # lock so the worker's check-gen-then-act blocks are atomic
        # against this bump: every worker side effect either completed
        # before the bump (and the teardown below cleans it up) or its
        # gen check fails and it never happens.
        with self._llock:
            self._launch_gen += 1
            self._launching = False

        # Stop polling rosnode list FIRST so a probe doesn't try to talk
        # to a master that's about to go down.
        self._nodes.stop()

        if self._thread:
            self._thread.stop()
            self._thread.wait(3000)
            self._thread = None

        self._monitor_stop = True

        if self._proc and self._proc.poll() is None:
            self.log.emit('Stopping ROS…', 'INFO')
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

        self._cleanup_ros()
        self._scan_folder = None
        self._app_log_path = None

    def stop_async(self):
        self._stop_gen += 1
        gen = self._stop_gen

        def _worker():
            try:
                self.stop()
            finally:
                if gen == self._stop_gen:
                    self.stop_done.emit()

        threading.Thread(target=_worker, daemon=True).start()

    # ----------------------------------------------------------------- helpers
    def is_running(self) -> bool:
        if cfg.DEV_MODE:
            return True
        # Launch worker hasn't spawned roslaunch yet (roscore still
        # coming up) — report alive so the QA watchdog doesn't treat
        # the bring-up window as a dead roslaunch.
        if self._launching:
            return True
        return self._proc is not None and self._proc.poll() is None

    def node_live(self, node_name: str) -> bool:
        """Is a ROS node currently registered with the master?"""
        return self._nodes.is_live(node_name)

    def driver_live(self, driver_key: str) -> bool:
        """Convenience: look up cfg.DRIVER_NODES and check liveness."""
        node = cfg.DRIVER_NODES.get(driver_key)
        if not node:
            return False
        return self._nodes.is_live(node)

    # ── back-compat shims (no longer power the status card) ────────────
    # Older callers may still reference these; keep them returning
    # empty so they don't crash, but don't compute anything live.
    def last_seen(self) -> dict:
        return {}

    def rates(self) -> dict:
        return {}

    def msg_counts(self) -> dict:
        return {}

    def app_log(self, msg: str):
        """Append a timestamped line to <scan_folder>/app.log if a scan is
        active. Best-effort — never raises."""
        path = self._app_log_path
        if not path:
            return
        try:
            with open(path, 'a') as f:
                f.write(f'{datetime.now().isoformat(timespec="seconds")}  {msg}\n')
        except Exception:
            pass

    def _write_log_header(self, launch_file, args, metadata):
        if not self._app_log_path:
            return
        try:
            with open(self._app_log_path, 'w') as f:
                f.write('=' * 70 + '\n')
                f.write(f' LITHIC PRO V2 — scan log\n')
                f.write(f' Started: {datetime.now().isoformat(timespec="seconds")}\n')
                f.write('=' * 70 + '\n\n')
                if metadata:
                    f.write('[Scan]\n')
                    for k, v in metadata.items():
                        f.write(f'  {k:14s}: {v}\n')
                    f.write('\n')
                f.write('[Launch]\n')
                f.write(f'  file : {launch_file}\n')
                for k, v in args.items():
                    f.write(f'  {k:10s}: {v}\n')
                f.write('\n')
        except Exception:
            pass

    # ----------------------------------------------------------------- private
    def _monitor_proc(self):
        """Poll-only watchdog. We no longer drain roslaunch stdout (it's
        nailed to /dev/null), so there's no firehose to consume — we
        just poll the process handle to detect an unexpected exit."""
        # Capture locally — stop() sets self._proc = None from another
        # thread, and `self._proc.poll()` mid-loop would AttributeError.
        proc = self._proc
        if not proc:
            return
        while not self._monitor_stop:
            if proc.poll() is not None:
                break
            time.sleep(0.5)

        if not self._monitor_stop:
            self.log.emit('roslaunch process exited unexpectedly', 'ERROR')
            self.launch_died.emit()

    def _cleanup_ros(self):
        """Per-scan cleanup — kills the driver / recorder nodes
        but deliberately leaves rosmaster, /rosout, and *our own* rospy
        node alive so the next scan can join the same master.

        Dynamically discovers nodes via `rosnode list` rather than using
        a hard-coded name list, so a future launch-file change (extra
        driver, renamed node) doesn't silently leave processes behind.
        """
        setup = os.path.expanduser(cfg.SETUP_BASH)

        # Build the "keep alive" set.
        keep = {'/rosout'}
        if _rospy is not None:
            try:
                # Skip the leading '/' in the comparison so we match
                # whatever rospy made up for our anonymous node.
                ours = _rospy.get_name()
                if ours:
                    keep.add(ours)
            except Exception:
                pass

        # 1) Discover live nodes and kill everything not on the keep list.
        try:
            r = subprocess.run(
                ['bash', '-c',
                 f'source {setup} && rosnode list 2>/dev/null'],
                capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                nodes = [n.strip() for n in r.stdout.splitlines() if n.strip()]
                to_kill = [n for n in nodes if n not in keep]
                if to_kill:
                    subprocess.run(
                        ['bash', '-c',
                         f'source {setup} && '
                         f'rosnode kill {" ".join(to_kill)} 2>/dev/null'],
                        capture_output=True, timeout=6)
        except Exception:
            pass

        # 2) Belt-and-braces: SIGTERM any straggler driver binaries that
        #    rosnode couldn't reach (e.g. a driver that ignored the
        #    kill RPC). roslaunch itself was already terminated in stop().
        #    'record' is the rosbag recorder binary (pkg="rosbag"
        #    type="record" runs as `record`, not `rosbag`) — a straggler
        #    here keeps the .bag.active open and corrupts the tail.
        try:
            subprocess.run(
                ['killall', '-q', 'roslaunch', 'hesai_ros_driver_node',
                 'seek_driver', 'xsens_mti_node', 'record'],
                capture_output=True, timeout=4)
        except Exception:
            pass

    # ----------------------------------------------------------------- roscore lifecycle
    @classmethod
    def _test_master_alive(cls, timeout: float = 2.0) -> bool:
        """True if a rosmaster is currently reachable."""
        setup = os.path.expanduser(cfg.SETUP_BASH)
        try:
            r = subprocess.run(
                ['bash', '-c',
                 f'source {setup} && rostopic list >/dev/null 2>&1'],
                capture_output=True, timeout=timeout)
            return r.returncode == 0
        except Exception:
            return False

    def _ensure_roscore(self):
        """Start a detached roscore if no master is reachable.

        Run in its own process group (setsid) so terminating roslaunch
        later doesn't take this master down with it.
        """
        cls = type(self)
        if cls._test_master_alive():
            return True
        # If we have a Popen but it's exited, clear it so we restart.
        if cls._roscore_proc is not None and cls._roscore_proc.poll() is not None:
            cls._roscore_proc = None

        if cls._roscore_proc is None:
            setup = os.path.expanduser(cfg.SETUP_BASH)
            self.log.emit('Starting roscore…', 'INFO')
            cls._roscore_proc = subprocess.Popen(
                ['bash', '-c', f'source {setup} && exec roscore'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )

        # Wait up to ~9 s for it to come up
        for _ in range(30):
            if cls._test_master_alive():
                self.log.emit('roscore ready', 'INFO')
                return True
            time.sleep(0.3)
        self.log.emit('roscore did not become ready within 9 s', 'WARN')
        return False

    # ----------------------------------------------------------------- class methods
    @classmethod
    def shutdown_roscore(cls):
        """Final teardown on app exit. Kills our detached roscore last
        so anything depending on the master gets a chance to clean up."""
        setup = os.path.expanduser(cfg.SETUP_BASH)
        for cmd in [
            f'source {setup} && rosnode kill -a',
            'killall -q roslaunch hesai_ros_driver_node seek_driver '
            'xsens_mti_node record',
        ]:
            try:
                subprocess.run(['bash', '-c', cmd],
                               capture_output=True, timeout=4)
            except Exception:
                pass

        # Now bring down our detached roscore
        if cls._roscore_proc is not None and cls._roscore_proc.poll() is None:
            try:
                # killpg because we started it with setsid (own process group)
                os.killpg(os.getpgid(cls._roscore_proc.pid),
                          __import__('signal').SIGTERM)
                cls._roscore_proc.wait(timeout=3)
            except Exception:
                try:
                    cls._roscore_proc.kill()
                except Exception:
                    pass
        cls._roscore_proc = None

        # Belt-and-braces: any stragglers
        try:
            subprocess.run(
                ['killall', '-q', 'rosmaster', 'rosout', 'roscore'],
                capture_output=True, timeout=3)
        except Exception:
            pass


# Back-compat aliases for any old callers
ROSController = RosController
