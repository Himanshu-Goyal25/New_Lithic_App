# INKERS Data Collector — Complete Documentation

LITHIC PRO V2 multi-sensor scanner. PySide6 kiosk GUI driving Hesai LiDAR
+ Xsens IMU + (optional) Seek Thermal through ROS Noetic, recording to
`rosbag` files. Targets Raspberry Pi 5 / Bookworm.

> This document is the reference. `README.md` covers installation,
> `setup-system.sh` is the root-side configuration, `install.sh` is the
> user-side setup. Start there if you're bringing up a fresh device.

---

## Table of contents

- [1. What the app does](#1-what-the-app-does)
- [2. System architecture](#2-system-architecture)
- [3. Process model](#3-process-model)
- [4. Repository layout](#4-repository-layout)
- [5. Per-file reference](#5-per-file-reference)
- [6. Configuration reference (`App/config.py`)](#6-configuration-reference-appconfigpy)
- [7. UI walkthrough — page by page](#7-ui-walkthrough--page-by-page)
- [8. Scan lifecycle (the core flow)](#8-scan-lifecycle-the-core-flow)
- [9. QA system](#9-qa-system)
- [10. Device readiness monitor](#10-device-readiness-monitor)
- [11. Data integrity — delete / copy / recovery](#11-data-integrity--delete--copy--recovery)
- [12. Audit log and supervisor PIN](#12-audit-log-and-supervisor-pin)
- [13. Theming](#13-theming)
- [14. ROS launch file](#14-ros-launch-file)
- [15. Logging — what gets recorded where](#15-logging--what-gets-recorded-where)
- [16. Operational guide](#16-operational-guide)
- [17. Troubleshooting](#17-troubleshooting)
- [18. Development guide](#18-development-guide)
- [19. Known design decisions](#19-known-design-decisions)
- [20. Glossary](#20-glossary)

---

## 1. What the app does

The operator walks a building with the LITHIC PRO V2 device, recording
synchronized LiDAR point clouds + IMU samples (+ optionally thermal
images) into rosbag files. Each "scan" produces a folder of 30-second
bag chunks plus a `scan_info.json` metadata file. The collected data is
later copied to a USB drive for processing on a workstation.

The GUI is a kiosk: it auto-launches on boot, fills the screen, and only
exposes the operations a non-technical operator needs:

- **Home** — at-a-glance device + storage + last-scan summary.
- **Scan** — set up a new scan (site / floor / part / in-charge) and run it.
- **Runs** — read-only browser of previous scans.
- **Data Transfer** — copy scans to USB, delete old ones.
- **Menu** — storage, theme, supervisor-locked admin actions.

Everything that could damage data (delete a scan, change device ID,
toggle DEV mode, edit CSVs) is locked behind a supervisor PIN.

---

## 2. System architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│  Raspberry Pi 5  (BCM2712, kernel 6.12)                                │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │              GUI process  (Python / PySide6)                     │  │
│  │                                                                  │  │
│  │   ┌─────────────────┐    ┌───────────────────┐                   │  │
│  │   │  MainWindow     │    │  DeviceMonitor    │                   │  │
│  │   │  + pages stack  │    │  (singleton)      │                   │  │
│  │   └────────┬────────┘    └────────┬──────────┘                   │  │
│  │            │                      │                              │  │
│  │   ┌────────▼─────────┐   ┌────────▼──────────┐                   │  │
│  │   │  ScanPlayerPage  │   │  QAWorker         │                   │  │
│  │   │  (live screen)   │◄──┤  (watchdog +      │                   │  │
│  │   └────────┬─────────┘   │   disk + bag QA)  │                   │  │
│  │            │             └───────────────────┘                   │  │
│  │   ┌────────▼─────────┐                                           │  │
│  │   │  RosController   │   spawns / monitors / kills roslaunch     │  │
│  │   └──────┬───────────┘                                           │  │
│  └──────────┼───────────────────────────────────────────────────────┘  │
│             │                                                          │
│             │ subprocess.Popen("roslaunch lidar_imu_record.launch ...") │
│             ▼                                                          │
│   ┌────────────────────────────────────────────────────────────────┐   │
│   │  roslaunch + child ROS nodes (one process tree)                │   │
│   │                                                                │   │
│   │   chrt -f 50 taskset -c 1-3  hesai_ros_driver_node             │   │
│   │   chrt -f 50 taskset -c 0    xsens_mti_node                    │   │
│   │   chrt -f 50 taskset -c 1-3  seek_driver_node    (optional)    │   │
│   │   rosbag record --split --duration=30 -b 1024 ...              │   │
│   └────────┬─────────────────┬──────────────────┬────────────────┘     │
│            │                 │                  │                      │
│      UDP (network)     UART (FTDI)         USB (bulk)                  │
│            ▼                 ▼                  ▼                      │
│       Hesai LiDAR       Xsens MTi IMU      Seek Thermal                │
│        192.168.1.201     /dev/ttyUSB0       (USB VID 289d)             │
│                                                                        │
│   Plus: persistent roscore  (started by GUI on first launch,           │
│         survives roslaunch death so the next scan reconnects           │
│         to the same master without re-init quirks).                    │
└────────────────────────────────────────────────────────────────────────┘
```

Key architectural choices:

- **One Python GUI process**, spawning ROS as a subprocess via the system
  shell. The GUI does not link to roscpp or hold any ROS state itself —
  if a driver crashes, only that subprocess dies.
- **Persistent `roscore`** owned by the GUI (via `setsid`) so killing
  `roslaunch` between scans doesn't take the master down with it.
- **All driver / rosbag processes** are pinned to specific cores
  (`taskset`) and run at SCHED_FIFO priority 50 (`chrt -f`). The Python
  GUI itself runs at default priority on whatever core the kernel picks.
- **Recording is rosbag's responsibility, not Python's.** We never read
  driver messages in Python and write them to disk — that would couple
  Python's GIL into the sensor read path. Python only observes that
  bags are appearing on disk and that the driver nodes are still in
  `rosnode list`.

---

## 3. Process model

| Process | Spawned by | Purpose | Survives what |
|---|---|---|---|
| `python3 App/main.py` | `run.sh` (from `.desktop`) | The GUI itself | Reboot via autostart |
| `roscore` (`rosmaster` + `rosout`) | `RosController._ensure_roscore` via `setsid` | ROS master | `roslaunch` death, individual scans |
| `roslaunch` + ROS nodes | `RosController.launch()` | Drivers + recorder | Tied to a single scan; killed on Stop |
| `setup-system.sh`-installed systemd units | systemd | One-shot config (CPU governor) and timer (log prune) | Not relevant per-scan |

Inside the GUI process, the relevant threads:

- **Qt main thread** — event loop, all widget updates.
- **`_ROSSpinThread`** (`core/ros_controller.py`) — only active when the
  app needs to subscribe to image topics (currently no-op because
  `config.VIEW_TOPIC` is empty).
- **`_NodeMonitor._loop`** (`core/ros_controller.py`) — polls
  `rosnode list` every 5 s for driver liveness.
- **`_monitor_proc`** (`core/ros_controller.py`) — watches the
  `roslaunch` subprocess; fires `launch_died` if it exits unexpectedly.
- **`_run_ros_stop`** (`gui/scan_player.py`) — spawned for each Stop
  click so `ros.stop()` doesn't block the GUI thread.
- **`_oom_protect_drivers`** (`core/ros_controller.py`) — short-lived,
  finds driver PIDs after launch and sudo-invokes the OOM helper.
- **`DeviceMonitor`** worker thread — runs the readiness probes
  (lsusb, ping, open `/dev/ttyUSB0`); paused during scans.
- **`QAWorker`** uses `QTimer` callbacks on the main thread plus a
  `QFileSystemWatcher` for bag-rotation events; no extra thread.
- **`_CopyWorker`** (`gui/scan_list.py`) — `QThread` doing file copy
  while the modal progress dialog keeps the UI responsive.

---

## 4. Repository layout

```
New_Lithic_App/
├── README.md                              installation walkthrough
├── DOCS.md                                THIS FILE
├── INKERS-Data-Collector.desktop          launcher (menu + Desktop + autostart)
├── run.sh                                 invoked by the .desktop file
├── install.sh                             user-side install (deps + launchers)
├── setup-system.sh                        root-side install (sysctl / udev / systemd / sudoers)
└── App/
    ├── main.py                            Qt entry point + splash + single-instance guard
    ├── config.py                          device-specific knobs (DEVICE / driver topics / IPs)
    ├── core/
    │   ├── audit.py                       append-only operator action log (JSONL)
    │   ├── qa_worker.py                   real-time watchdog: drivers + disk + per-bag QA
    │   └── ros_controller.py              roslaunch lifecycle + rosnode poll + OOM protect
    ├── data/
    │   ├── sites.csv                      operator pick-list: building names
    │   ├── incharge.csv                   operator pick-list: in-charge persons
    │   ├── theme.json                     persisted "light" / "dark" preference
    │   ├── supervisor.json                PIN hash + salt (auto-created)
    │   └── action_log.jsonl               append-only audit log (auto-created)
    └── gui/
        ├── main_window.py                 chrome, sidebar, footer, palette re-exports
        ├── theme.py                       LIGHT / DARK palettes + persistence
        ├── home.py                        Home page (device + last scan + storage + status)
        ├── scan_page.py                   Setup → Player stack wrapper
        ├── scan_setup.py                  form for site / floor / part / in-charge
        ├── scan_player.py                 the live scan screen (biggest file)
        ├── scan_list.py                   ScanListWidget + copy/delete + orphan recovery
        ├── scan_stats.py                  size / duration / GB-per-hour math + dump helpers
        ├── runs.py                        Runs page (read-only scan browser)
        ├── data_transfer.py               Data Transfer page (copy to USB + free space)
        ├── device_status.py               DeviceMonitor singleton + DeviceStatusPanel
        ├── settings_page.py               Menu page (storage / appearance / supervisor)
        ├── supervisor.py                  PIN storage + verify + time-limited unlock state
        ├── supervisor_dialog.py           PIN entry dialog
        ├── supervisor_tools.py            admin dialogs (change PIN / device id / dev mode / CSVs / log)
        └── make_icon.py                   utility: generates an icon PNG from the brand glyph
```

Total: ~8000 lines of Python.

---

## 5. Per-file reference

### `App/main.py` (236 lines)

- Inserts the `App/` directory onto `sys.path` so `import config` works.
- Single-instance guard via `QLocalServer` / `QLocalSocket` so
  double-clicking the launcher twice doesn't spawn two GUIs.
- `_SplashScreen` — frameless full-screen splash with fade-in /
  fade-out via `QGraphicsOpacityEffect` + `QPropertyAnimation`.
- `_apply_palette()` — pins every `QPalette` role to a value from
  `gui.theme.P` so the OS palette doesn't leak through transparent
  widgets.
- Imports the heavy modules (`MainWindow`, ROS controller) AFTER the
  splash is shown, so the user sees the brand within ~100 ms.

### `App/config.py`

See [§6 Configuration reference](#6-configuration-reference-appconfigpy).

### `App/core/audit.py` (77 lines)

Append-only JSONL action log at `App/data/action_log.jsonl`. Public API:

```python
log_action('scan_started', site='Site A', folder='/media/.../dumps/...')
log_action('scan_stopped', folder='...')
log_action('dev_mode_toggled', new_value=True)
read_actions(n=200)   # newest first
read_recent(n=100)    # chronological (back-compat)
log_path()            # absolute path of the log
```

Every write is best-effort — wrapped in `try/except` so a full disk
never blocks a scan. One JSON object per line so a half-written line
doesn't invalidate earlier entries.

Surfaces in the GUI through `Settings → Supervisor → View Action Log`.

### `App/core/qa_worker.py` (309 lines)

Real-time quality monitoring during a scan. Three independent checks:

1. **Watchdog** (every 10 s): `ros.driver_live(<key>)` for each entry
   in `cfg.DRIVERS` (including the watchdog-only `rosbag` recorder).
   Tracks `_driver_fail_n` per driver; for a driver that was up and then
   disappears, auto-terminates after `DEBOUNCE_TICKS` (2) consecutive
   DEAD ticks. For a driver that never comes up, auto-terminates once
   `NEVER_STARTED_TIMEOUT_S` (30 s) elapses. Seek is the only driver
   excluded from termination; every non-seek driver — sensors and the
   recorder alike — is fatal.
2. **Disk** (every 10 s): checks both `cfg.DUMP_PATH` and `/`.
   Terminates if either drops below its threshold (`MIN_DISK_GB` for
   data drive, hardcoded 1.5 GB for `/`).
3. **Per-bag** (event-driven via `QFileSystemWatcher`): when a `.bag`
   appears in the scan folder, opens it with `rosbag.Bag()`, computes
   per-topic counts, classifies each driver as OK / WARN / LOW / MISSING
   against `cfg.DRIVERS[driver][topic]` thresholds (with slack from
   `cfg.BUFFER`). Terminates with a multi-line reason listing offenders.

See [§9 QA system](#9-qa-system) for the status ladder.

Signals:

- `log(message, level)` — routed through the player's console.
- `terminate(reason)` — handled by the player's `_on_qa_terminate`,
  which auto-stops and pops the alert overlay.
- `bag_checked(results)` — currently unused on the receiver side
  (the player builds its own per-bag display in `_check_bag_rotation`).

### `App/core/ros_controller.py` (714 lines)

Encapsulates everything ROS-related so the GUI never imports rospy
directly. Public surface:

```python
ros = RosController()
ros.frame_received.connect(slot)   # signal(topic: str, image: QImage)
ros.launch_died.connect(slot)
ros.log.connect(slot)              # signal(msg, level)

ros.launch(launch_file, args, metadata=...)
ros.subscribe(topic, msg_type)     # no-op shim
ros.stop()                         # synchronous; caller should wrap in thread
ros.app_log(msg)                   # tee to <scan_folder>/app.log
ros.is_running()
ros.driver_live(driver_key)        # bool: rosnode list contains the driver's node
RosController.shutdown_roscore()   # class method, called at app exit
```

Internal pieces:

- **`_ROSSpinThread`** — QThread that initialises `rospy` and subscribes
  to any `cfg.VIEW_TOPIC`. Currently no-op because thermal preview is
  off. Designed to NEVER call `rospy.signal_shutdown()` between scans
  (rospy is one-way: once shut down, the next `init_node` is permanent
  garbage).
- **`_NodeMonitor`** — background thread that runs
  `bash -c 'source setup.bash && rosnode list'` every 5 s, parses the
  output, and serves `is_live(node_name)` queries.
  Polling interval was tuned to balance "feedback to operator" vs
  "every probe forks a bash + sources a 1 MB setup.bash + opens an
  XML-RPC socket, which leaks into the IMU read loop at 1 Hz". 5 s is
  the sweet spot we measured.
- **`_ensure_roscore`** — starts a persistent `roscore` in its own
  process group (`os.setsid`) so killing `roslaunch` later doesn't
  take it down. Cached at class level so a single instance is shared.
- **`shutdown_roscore`** — class method called at app exit
  (`atexit` in `main.py`) that kills the persistent master.
- **`_oom_protect_drivers`** — background worker that polls
  `pgrep -x <name>` until each entry in `_OOM_PROTECT_PROCS`
  appears, then batches their PIDs through
  `sudo /usr/local/sbin/lithic-oom-protect <pids>`.

### `App/gui/main_window.py` (400 lines)

The shell window: frameless, fullscreen, contains:

- **Title bar** — gradient brand label, fake macOS-style min/max/close
  buttons (`_GradientLabel` paints with `QLinearGradient`).
- **Sidebar** (`_NavButton` × 5: Home, Scan, Runs, Data Transfer, Menu).
- **Content stack** — `QStackedWidget` of the five pages.
- **Footer** — scan indicator (left) + disk free/usage bar (right).
- **Disk poll** — `QTimer` every 5 s refreshes the footer's
  `<scan_state> · Primary X GB free · External Y GB free` row.

Also re-exports every theme constant (`BG`, `PRIMARY`, …) from
`gui.theme.P` so the rest of the codebase can keep
`from gui.main_window import …` imports unchanged after a theme switch.

`_GradientLabel` and `_NavButton` are reused across multiple pages.

### `App/gui/theme.py` (129 lines)

The two palettes (LIGHT, DARK) as plain dicts; the active one is
selected at import time based on `App/data/theme.json`.

`apply()` writes the selected key back to the JSON file. The Settings
page calls this and prompts the user to restart — a hot theme swap is
deliberately not supported (it would require rebuilding every already-
constructed widget for marginal value on a kiosk).

### `App/gui/device_status.py` (446 lines)

Two classes:

- **`DeviceMonitor`** (singleton, `instance()` classmethod) — the only
  code that actually probes the hardware. Owns one `QTimer` ticking at
  5 s and a worker thread that runs the probes (lsusb, ping, open
  `/dev/ttyUSB0` and look for the Xsens preamble `0xFA 0xFF`).
  `pause()` / `resume()` methods are called by `ScanPlayerPage`
  around a scan so the IMU probe doesn't steal UART bytes from
  `xsens_mti_node`.
- **`DeviceStatusPanel`** — the visual chip row. Subscribed to
  `DeviceMonitor.results_changed`. Used twice: as a banner on Home,
  and as the sidebar on Scan Setup.

The XsensIMU probe was the **smoking gun** for an ~50-msgs/30s IMU
drop during scans: Linux lets two processes share a TTY, so each
5-second probe stole a fraction of a second's worth of IMU bytes from
the driver. Pausing during scans fixed it completely.

### `App/gui/scan_setup.py` (543 lines)

The form before a scan starts. Fields:

- **Site** (combo + completer, loaded from `App/data/sites.csv`)
- **Floor type** (Ground Floor / Floor / Basement)
- **Floor number** (`QSpinBox` with custom +/- icons)
- **Scan part** (`QLineEdit`)
- **In-charge** (combo from `App/data/incharge.csv`)

`scan_requested(metadata: dict)` signal feeds `ScanPage`, which
switches to the player.

Includes a `DeviceStatusPanel` in its right sidebar so the operator
sees readiness while filling out the form.

### `App/gui/scan_player.py` (1514 lines — the biggest file)

Live scan view. Owns:

- The ROS controller (`self.ros = RosController()`)
- The QA worker (`self.qa = QAWorker(self)`)
- An inotify watcher for the scan folder (`QFileSystemWatcher`)
- Timers: `_elapsed_timer` (1 s) for the duration label,
  `_pill_timer` (1 s) for LiDAR/IMU LIVE/DEAD pills
- The console widget (rich-text QTextEdit, monospaced)

Lifecycle methods:

- `begin(metadata)` — resets UI, called when the user clicks "Start Scan"
  on Scan Setup.
- `_start_scan()` — pauses DeviceMonitor → resets bag tracking →
  subscribes view topics (no-op here) → `ros.launch(...)` → starts
  inotify watch + elapsed timer + pill timer + QA worker → shows the
  startup-guidance overlay.
- `_stop_scan(after=...)` — sets stopping flag, freezes the recording
  UI immediately (stops `_elapsed_timer`, kills the pulsing dot,
  resets pills), spawns a daemon thread for `ros.stop()`, shows the
  `_StoppingOverlay`, schedules a 30 s deadline.
- `_finish_stop()` — queued-connected to `_ros_stop_done`. Hides
  stopping overlay, sets buttons back, resumes DeviceMonitor,
  writes scan_info.json, fires the after-stop callback.
- `_auto_terminate(reason)` — used by QA and bag-corruption checks.
  Stops the scan + shows `_AlertOverlay` with the reason.

Overlays (all inner classes in this file):

- `_OverlayConfirm` — "Scan in progress, stop and continue?" yes/no.
- `_StoppingOverlay` — "Stopping and saving scan…" animated dots.
- `_AlertOverlay` — title + reason + OK button, monospaced body so QA
  bullets line up.
- `_StartupGuide` — 3-phase guidance (STAY STILL 45 s → ROTATE 15 s →
  START MOVING 5 s) with auto fade-out.

Bag rotation: handled by `_check_bag_rotation` via inotify. On each
`.bag.active → .bag` rename, opens the just-closed bag with
`rosbag.Bag()` for the authoritative per-topic count. If
`rosbag.Bag()` raises (corrupt index), logs ERROR and auto-terminates
immediately — see [§9 QA system](#9-qa-system).

### `App/gui/scan_list.py` (1448 lines)

Reusable list widget used by Runs and Data Transfer pages, plus all the
copy/delete/recovery machinery:

- **`ScanListWidget`** — checkable rows, search, sort, drag-scroll.
- **`copy_scans_with_dialog(scans, dest, parent)`** — public entry for
  copy. Pre-flight free-space check, `_CopyWorker` QThread, modal
  `_CopyDialog`.
- **`_CopyWorker`** — the actual copy. Per-file `.part` sidecar +
  `fsync` + size verify + atomic `os.replace`; per-scan post-walk
  source/destination diff; global `os.sync()` at end.
- **`delete_scans_with_confirm(scans, parent)`** — public entry for
  delete. Uses `_force_remove_tree` which tries `shutil.rmtree`,
  falls back to `chmod -R u+w` + `rm -rf` to survive `ntfs3` dirent
  corruption.
- **`_recover_orphan(orphan)`** — called from the orphan-recovery
  dialog. First runs `_release_stale_rosbag_locks` to kill any straggler
  ROS processes and rename leftover `.bag.active` files to `.bag`, then
  writes a `scan_info.json` marking the scan as `recovered: true`.
- **`offer_orphan_recovery(parent, dumps_root)`** — opens the recovery
  dialog if `find_orphan_scans` finds anything. Called once at startup
  from `MainWindow`.

### `App/gui/scan_stats.py` (266 lines)

Pure functions, no Qt:

- `list_scans(root)` — walk for `scan_info.json` files, attach
  computed size/duration, return newest-first.
- `find_orphan_scans(root)` — folders with `.bag` files but no valid
  `scan_info.json`.
- `parse_scan_folder_name(name)` — reverse the canonical folder name
  produced by `_make_scan_folder` (site + floor + part + timestamp).
- `format_size(n)` / `format_duration(seconds)` — human strings.
- `estimate_gb_per_hour(scans)` — average of non-recovered scans,
  filtered to plausible per-scan rates `[10, 200] GB/h`.
- `estimate_hours_remaining(free_bytes, gb_per_hour)`.
- `find_external_mount()` / `find_all_external_mounts()` — USB
  detection across `/media`, `/run/media`, `/mnt`.

### `App/gui/home.py` (165 lines)

Three top cards (Device / Last Scan / Storage Free) + `DeviceStatusPanel`
banner + a big "Start a New Scan" CTA that jumps to the Scan page.
Auto-refreshes every 10 s.

### `App/gui/runs.py` (71 lines)

Thin wrapper: a `_GradientLabel` title, a refresh button, a total
size readout, and a non-selectable `ScanListWidget`.

### `App/gui/data_transfer.py` (317 lines)

Two columns: a "External Drive" status card on top (detects USB mounts
via `find_external_mount`, polls every 5 s), then a selectable
`ScanListWidget` of scans below, with Copy / Delete / Refresh / Eject
buttons. Eject is run on a daemon thread (so the GUI doesn't freeze
during sync) and reports success/failure via the queued
`_eject_done` signal.

### `App/gui/settings_page.py` (375 lines)

Three cards in a scroll area:

- **Storage** — recording location (read-only, mirrors `cfg.DUMP_PATH`).
- **Appearance** — Light / Dark toggle (writes via `gui.theme.apply`).
- **Supervisor** — locked actions: Change PIN, Edit Device ID, Toggle
  DEV mode, Manage Sites/In-charge CSVs, View Action Log. Each tile
  calls `supervisor.ensure_unlocked(self)` before opening its dialog.

### `App/gui/supervisor.py` (103 lines)

- Stores PIN as `(salt, pbkdf2_hmac('sha256', pin, salt, 100_000))` in
  `App/data/supervisor.json`. Default PIN is `0000`.
- `verify(pin)` — constant-time `hmac.compare_digest`; updates
  `_unlock_until` to `now + 10 min` on success.
- `is_unlocked()` / `lock()` / `ensure_unlocked(parent)` — gate every
  admin action behind a 10-minute window so a single PIN entry covers a
  short admin session.

### `App/gui/supervisor_dialog.py` (134 lines)

Frameless modal PIN entry dialog. Numeric keypad + dotted display.

### `App/gui/supervisor_tools.py` (423 lines)

The admin dialogs themselves:

- **`_ChangePinDialog`** — old pin / new / confirm.
- **`_DeviceIdDialog`** — edits `cfg.DEVICE` and writes back to
  `config.py` (atomic write via `patch_config_py`).
- **`_DevModeDialog`** — toggles `cfg.DEV_MODE`, same atomic writer.
- **`_CsvManagerDialog`** — edit Sites / In-charge CSVs in-place.
- **`_ActionLogDialog`** — paginated read of `action_log.jsonl`.

### `App/gui/make_icon.py` (95 lines)

Tool, not part of the runtime. Generates `Images/2739025.png` from the
brand glyph. Run manually if the launcher icon needs to be rebuilt.

---

## 6. Configuration reference (`App/config.py`)

Every operator-or-device-specific value lives here. Other modules
`import config as cfg` and read attributes off it. Changes can be made
two ways:

1. **Edit the file directly** (requires app restart).
2. **Through the GUI's supervisor tools**, which use
   `config.patch_config_py(key, value)` to do an atomic rewrite (tmp +
   fsync + rename) — only supports `device_id` and `dev_mode`.

### Top-level switches

| Key | Default | Meaning |
|---|---|---|
| `DEV_MODE` | `False` | If True, app runs with mock data and no real hardware. UI is identical; QA worker auto-passes; DeviceMonitor reports all green. |
| `DEVICE` | `'LITHIC_PRO_V2'` | Device identifier, embedded in bag filenames and scan_info.json. |

### ROS workspace

| Key | Default | Meaning |
|---|---|---|
| `SETUP_BASH` | `~/catkin_hesai_ros2/devel/setup.bash` | Sourced before any subprocess that talks to ROS. |
| `LAUNCH_FILE` | `~/catkin_hesai_ros2/lidar_imu_record.launch` | Passed to `roslaunch`. |

### Drivers

```python
DRIVERS = {
    'hesai':  {'/hesai/pandar': 595},     # 20 Hz × 30 s
    'xsens':  {'/imu/data':    5990},     # 200 Hz × 30 s
    'rosbag': {},                         # watchdog-only (no source topic)
}
```

Each top-level key is a driver. Each inner dict maps topic → expected
message count per 30-s bag. `QAWorker._check_bag` compares actual counts
against these to produce OK/WARN/LOW/MISSING status.

The `rosbag` entry is a **watchdog-only** driver: its empty dict means
the per-bag count check skips it, but the node-liveness watchdog still
runs against `DRIVER_NODES['rosbag']` (`/rosbag_record`). rosbag has no
source topic of its own — its silence means recording isn't happening,
so if its node dies (or never comes up within 30 s) the scan is
auto-terminated with a "Recorder died / never started" alert. It is not
a sensor, so it is deliberately excluded from the LiDAR/IMU readiness
pills and the per-bag console block in the player.

To re-enable Seek, uncomment the `'seek'` entry here AND the
corresponding `BUFFER['seek']` AND the `DRIVER_NODES['seek']` entry.

| Key | Purpose |
|---|---|
| `DRIVERS` | Topic counts QA expects per 30-s bag. Empty dict = watchdog-only (node liveness, no count check). |
| `BUFFER` | Per-driver slack subtracted from threshold for WARN classification. `lidar=20, imu=50`. |
| `DRIVER_NODES` | Driver key → expected ROS node name shown in `rosnode list`. |
| `VIEW_TOPIC` | Live-video subscriptions for the player. Empty (no live preview without thermal). |

### Hardware-readiness probes

| Key | Default | Used by |
|---|---|---|
| `LIDAR_IP` | `192.168.1.201` | `_probe_lidar` (ICMP ping) |
| `LIDAR_HOST_IP` | `192.168.1.23` | (optional) — set `''` to skip the NIC-IP sanity check |
| `SEEK_USB_VID` | `289d` | `_probe_seek` (lsusb match) |
| `XSENS_FTDI_VID` | `0403` | `_probe_xsens_imu` (lsusb match) |
| `XSENS_FTDI_PID` | `6001` | same |
| `XSENS_SERIAL_PORT` | `/dev/ttyUSB0` | open() + read 1 KB looking for `0xFA 0xFF` |
| `XSENS_SERIAL_BAUD` | `2000000` | |

### Storage + QA

| Key | Default | Meaning |
|---|---|---|
| `DUMP_PATH` | `/media/cm5-v1/DATA` | Root of all scan data. The `dumps/<month>/<DD_MMM>/<scan>/` tree lives under this. |
| `MIN_DISK_GB` | `5` | QA terminates scans below this on the data drive. |
| `DISPLAY_RESOLUTION` | `None` | Force a kiosk resolution, e.g. `(1920, 1080)`. `None` = native. |

### Pick-list CSVs

| Key | Path | Editor |
|---|---|---|
| `DATA_DIR` | `App/data/` | — |
| `SITES_CSV` | `App/data/sites.csv` | Supervisor → Manage Sites |
| `INCHARGE_CSV` | `App/data/incharge.csv` | Supervisor → Manage In-charge |

### Atomic config patcher

```python
config.patch_config_py('device_id', 'LITHIC_PRO_V3')   # rewrites config.py
config.patch_config_py('dev_mode',  True)
```

Uses regex on the source file with `count=1` so it only edits the
canonical assignment, then writes via tmp + fsync + os.replace.

---

## 7. UI walkthrough — page by page

### Home

The default landing page. Three top cards:

- **Device** — `cfg.DEVICE` + a brief description.
- **Last Scan** — site / when / size of the most recent `scan_info.json`.
- **Storage Free** — free bytes on `cfg.DUMP_PATH`, plus an estimated
  "≈ N hours of scan time" computed from `estimate_gb_per_hour` of
  past scans (fallback `DEFAULT_GB_PER_HOUR = 60`).

Below the cards: a horizontal `DeviceStatusPanel` showing live LiDAR /
IMU readiness.

A big primary "Start a New Scan" button jumps to the Scan page.

Refreshes every 10 s.

### Scan (sub-stack: Setup → Player)

`ScanPage` is just a `QStackedWidget` of `ScanSetupPage` and
`ScanPlayerPage`. The setup form emits `scan_requested(metadata)` →
the player calls `begin(metadata)` and the stack flips.

When the operator clicks "Next Scan" on the player after a scan ends,
the page flips back to a freshly-reset setup form.

#### Setup
Form fields (Site / Floor type / Floor number / Scan part / In-charge)
with custom widgets:

- Site + In-charge are autocomplete combos backed by CSVs.
- Floor number is a `QSpinBox` with custom +/- icons painted as lines
  in `_step_glyph` (so the symbols are pixel-aligned regardless of
  font metrics).
- The right column is a `DeviceStatusPanel` so the operator sees
  hardware readiness while filling out the form.

The Start button is disabled until all required fields are valid.

#### Player
Documented in detail in [§8](#8-scan-lifecycle-the-core-flow).

### Runs

Read-only list of past scans (anything under
`<DUMP_PATH>/dumps/**/scan_info.json`). Per-row: site / when / duration
/ size / driver list. Click → details popup with bag count and per-topic
totals. No mutation buttons — the only actions on this page are
refresh and view.

### Data Transfer

- **External drive card** at top: shows the currently-mounted USB drive
  (path, label, free space) or "Plug in a USB drive to copy scans."
  Polled every 5 s via `find_external_mount`.
- **Eject button** runs `udisksctl unmount/power-off` on a daemon
  thread; result reported back via `_eject_done` signal.
- **Scan list** below: selectable rows.
- **Copy / Delete / Refresh** buttons. Copy is enabled only when both
  ≥ 1 scan is selected AND a USB drive is mounted with enough free
  space.

### Menu

Three cards:

- **Storage** — read-only display of `cfg.DUMP_PATH` and `MIN_DISK_GB`.
- **Appearance** — Light/Dark toggle. Writes via `gui.theme.apply` and
  shows a "Restart the app for the new theme" prompt.
- **Supervisor** — five locked actions. Clicking any of them runs
  `supervisor.ensure_unlocked(self)`; if not unlocked, opens
  `_PinDialog`. Successful unlock keeps the session admin for 10 min.

---

## 8. Scan lifecycle (the core flow)

```
operator on Scan Setup
  │
  ├─[Start Scan clicked]
  │     ScanSetupPage.scan_requested.emit(metadata)
  ▼
ScanPlayerPage.begin(metadata)
  │ - reset console + info panel
  │ - clear video panel (no thermal feed in this build)
  │
  ├─[user clicks ▶ Start Scan on player]
  ▼
_start_scan()
  │ 1. _make_scan_folder() → /media/.../DATA/dumps/<month>/<DD_MMM>/<name>_<ts>/
  │ 2. DeviceMonitor.pause()              ← releases /dev/ttyUSB0
  │ 3. reset _bag_active_name, _bag_index
  │ 4. QFileSystemWatcher.addPath(scan_folder)
  │ 5. ros.subscribe(... cfg.VIEW_TOPIC)  ← no-op currently
  │ 6. ros.launch(cfg.LAUNCH_FILE, {'data_path': scan_folder})
  │       └─ subprocess.Popen(['bash','-c','source setup.bash && roslaunch ...'])
  │       └─ _NodeMonitor.start()
  │       └─ _oom_protect_drivers thread starts
  │ 7. _elapsed_timer.start(1000)
  │ 8. _pill_timer.start()
  │ 9. qa.start(scan_folder, ros)
  │ 10. _set_scanning(True)               ← RECORDING pulse on, scan_state_changed.emit(True)
  │ 11. _StartupGuide overlay shown (45 s STAY STILL → 15 s ROTATE → 5 s MOVING)
  │ 12. audit.log_action('scan_started', site=..., folder=...)
  ▼
… recording …
  │ - rosbag rolls a new file every 30 s
  │ - on .bag.active → .bag rename, QFileSystemWatcher fires
  │     _check_bag_rotation():
  │       open closed bag → if rosbag.Bag() raises → log ERROR + _auto_terminate
  │       else → render BAG block in console with per-driver count/expect/STATUS
  │ - QAWorker checks watchdog + disk every 10 s; on terminate, plumbs to player
  │
  ├─[user clicks ■ Stop Scan]
  ▼
_stop_scan(after=None)
  │ 1. _is_stopping = True, stop_btn disabled, text "Stopping…"
  │ 2. qa.stop()  ← stops watchdog/disk timers
  │ 3. _elapsed_timer.stop() + _pill_timer.stop() + _freeze_recording_ui()
  │      ← UI immediately stops showing "RECORDING" and a climbing duration
  │ 4. _StoppingOverlay shown
  │ 5. threading.Thread(target=_run_ros_stop).start()
  │      └─ ros.stop() — _nodes.stop, _thread.stop, _proc.terminate(5s), _cleanup_ros
  │      └─ emits _ros_stop_done (queued connection → _finish_stop on Qt thread)
  │ 6. QTimer.singleShot(30 s, _stop_deadline_expired)  ← failsafe
  ▼
_finish_stop()  (or _stop_deadline_expired after 30 s)
  │ 1. hide _StoppingOverlay; cancel _StartupGuide if still running
  │ 2. _set_scanning(False) ← STOPPED card, scan_state_changed.emit(False)
  │ 3. _write_metadata() ← scan_info.json with site/floor/part/incharge/started_at/stopped_at
  │ 4. remove inotify watch
  │ 5. DeviceMonitor.resume()
  │ 6. fire `_after_stop` callback (used by Next Scan + QA-driven alert popup)
  ▼
operator sees STOPPED, can click "Next Scan" to return to setup,
or close the window (confirm overlay if scan was in progress).
```

---

## 9. QA system

Lives in `core/qa_worker.py`. Started by the player in `_start_scan`,
stopped in `_stop_scan`. Three concurrent checks:

### 9.1 Watchdog (10 s tick)

For each driver in `cfg.DRIVERS` (sensors plus the watchdog-only
`rosbag` recorder):

```python
alive = ros.driver_live(driver_key)
if alive:
    _driver_ever_up[driver] = True
    _driver_fail_n[driver]  = 0
elif not _driver_ever_up[driver]:
    # never came up yet — give it NEVER_STARTED_TIMEOUT_S to appear
    if elapsed > NEVER_STARTED_TIMEOUT_S:          # 30 s
        terminate.emit(f'{label} never started …')  # unless seek
else:
    # was up, now gone — debounce a transient rosnode hiccup
    _driver_fail_n[driver] += 1
    if _driver_fail_n[driver] >= DEBOUNCE_TICKS:   # 2 ticks ≈ 20 s
        terminate.emit(f'{label} died …')           # unless seek
```

Two consecutive DEAD ticks (~20 s) terminate a driver that had been
alive; a driver that never appears within 30 s of `start()` terminates
immediately. The recorder gets distinct phrasing — "Recorder never
started — no bags will be written" / "Recorder died — recording has
stopped" — so the alert overlay is unambiguous. Seek is the only driver
excluded from termination.

### 9.2 Disk (10 s tick)

```python
free_data = shutil.disk_usage(cfg.DUMP_PATH).free / 1e9
if free_data < cfg.MIN_DISK_GB:
    terminate.emit(f'Disk space critical on data drive: {free_data:.1f} GB free')

free_root = shutil.disk_usage('/').free / 1e9
if free_root < 1.5:
    terminate.emit(f'Root filesystem critical: ... Stopping before OS becomes unresponsive.')
```

Root-FS check was added after a 20-min scan filled the SD card to 99%
and locked up the whole OS. The kernel can't even allocate memory or
fork bash when `/` is full.

### 9.3 Per-bag (event-driven via QFileSystemWatcher)

On every `.bag` created in the scan folder:

```python
counts = {topic: rosbag.Bag(path).get_type_and_topic_info()...}
for driver, topics in cfg.DRIVERS.items():
    for topic, threshold in topics.items():
        buf = BUFFER[driver_key]
        count = counts.get(topic, 0)
        if count >= threshold:           → OK
        elif count >= threshold - buf:   → WARN     (close but acceptable)
        elif count > 0:                  → LOW      (terminate from bag 1+)
        else:                            → MISSING  (terminate always)
```

Termination rules:
- **Seek topics never terminate.**
- **MISSING** (count = 0) is fatal on any non-seek topic, even on bag 0
  (the first 30-s bag).
- **LOW** is fatal only from bag 1 onward — bag 0 is partial because
  recording starts mid-second so a small shortfall is expected.

The terminate reason now lists each offender with `count/threshold
[status]`, so the alert overlay reads e.g.:

```
Bag 3 QA failed — data below threshold:
  • LiDAR 543/600 [LOW]
  • IMU 4,820/6,000 [LOW]
```

### 9.4 Per-bag corruption check (in `scan_player.py`)

Independent of QAWorker. `_read_bag_topic_counts` is called from
`_check_bag_rotation` on each `.bag.active → .bag` rename. If
`rosbag.Bag()` raises (corrupt index, truncated file), the player
immediately calls `_auto_terminate(...)`. No streak required — one
bad bag terminates the scan, because by the time you see one,
recording integrity is already gone.

---

## 10. Device readiness monitor

`gui/device_status.py`. Singleton `DeviceMonitor.instance()`. Probes:

| Device | Probe |
|---|---|
| LiDAR | `subprocess.run(['ping', '-c', '1', '-W', '1', LIDAR_IP])` |
| IMU | `lsusb` looking for `XSENS_FTDI_VID:XSENS_FTDI_PID` AND open `XSENS_SERIAL_PORT` at `XSENS_SERIAL_BAUD`, read up to 1 KB, look for `0xFA 0xFF` preamble in the buffer |
| Thermal | `lsusb` looking for `SEEK_USB_VID:*` (when enabled) |

Why both lsusb AND open the port for IMU: the FTDI cable stays
enumerated even when the IMU is unpowered, so lsusb alone gives a
false positive. Reading actual bytes from the port confirms the
sensor is on.

### Pause / resume

The IMU probe opens `/dev/ttyUSB0` for ~1 second every 5 seconds.
Linux allows two processes to share a TTY, splitting the byte stream
between them. With `xsens_mti_node` already reading the port, our
probe steals ~9 IMU messages per invocation. Six probes per 30-s bag
= ~54 lost samples — exactly the symptom that took us a day to diagnose.

`pause()` is called by the player at `_start_scan`, blocks briefly
(up to 2 s) for any in-flight probe to finish, then stops the timer.
`resume()` is called from `_finish_stop` once the driver has fully
released the port.

---

## 11. Data integrity — delete / copy / recovery

### 11.1 Delete

`delete_scans_with_confirm` (and the single-folder variant in
`scan_list.py`) routes through **`_force_remove_tree(path)`**:

```
1. shutil.rmtree(path)        ← fast path for clean trees
2. if dir still exists:
       chmod -R u+w path       ← reset any read-only NTFS files
       rm -rf -- path          ← uses unlinkat(), survives broken NTFS dirents
3. return os.path.exists(path) == False
```

The fallback exists because `ntfs3` can leave directory entries whose
`stat()` returns garbage (`ls -la` shows `-?????????`). Python's
`shutil.rmtree` uses `os.scandir + stat + unlink` and aborts halfway
when it hits one. `rm -rf` ignores stat errors and uses lower-level
syscalls.

If the helper returns False, the operator gets a clear error pointing
to `sudo ntfsfix /dev/nvme0n1p1`.

### 11.2 Copy

`_CopyWorker._copy_file(src, dst)`:

```
part = dst + '.part'
remove any leftover part      ← from a prior aborted run

open(src, 'rb') + open(part, 'wb')
while chunk = src.read(1 MB):
    part.write(chunk)
    progress.emit(bytes_done, total, scan_name)
part.flush()
os.fsync(part.fileno())       ← push pages to device BEFORE close

if getsize(src) != getsize(part):    ← truncation check
    os.remove(part); _skipped += 1; return

copystat(src, part)           ← preserve mtime/perms
os.replace(part, dst)         ← atomic promote
```

After all scans in the batch finish copying:

```
self._current_name = 'flushing to disk…'
os.sync()                     ← global flush — "Copied" only when really on the device
```

`_copytree_bytes` walks with `onerror=self._on_walk_error` so broken
NTFS dirents in subdirectories don't abort the whole scan — they're
just counted.

After each per-scan copy, `_verify_against_source` walks both src and
dst, diffs the relative-path sets, and adds anything in `src - dst`
to `_skipped` and `_missing_names`. Result is reported as
`(N unreadable, M missing in destination)` per scan in the final
dialog.

### 11.3 Orphan recovery

Triggered once at startup by `MainWindow._offer_orphan_recovery`.
`find_orphan_scans` returns folders with `.bag` files but no valid
`scan_info.json`. The dialog shows them as cards; the operator can
mark each one as Recover / Delete / Skip.

**Recover** flows through `_recover_orphan(orphan)`:

```
1. _release_stale_rosbag_locks(folder):
     - killall -TERM rosbag roslaunch hesai_ros_driver_node \
                            xsens_mti_node seek_driver
     - sleep 0.4 s
     - rename any *.bag.active → *.bag (if .bag doesn't already exist)
2. atomic write of scan_info.json with:
     - parsed site/floor/part from folder name
     - incharge: '(recovered)'
     - started_at: parsed_started_guess
     - stopped_at: orphan['mtime']
     - recovered: True
```

The `recovered: True` flag is later honoured by
`estimate_gb_per_hour` to keep the bogus `stopped_at` from poisoning
the rate average.

---

## 12. Audit log and supervisor PIN

### Audit log

`App/data/action_log.jsonl`. One JSON object per line:

```
{"ts":"2026-05-14T16:17:32","action":"scan_started","details":{"site":"...","folder":"..."}}
{"ts":"2026-05-14T16:32:00","action":"scan_stopped","details":{"folder":"..."}}
{"ts":"2026-05-14T16:48:10","action":"dev_mode_toggled","details":{"new_value":true}}
```

Actions currently logged: `scan_started`, `scan_stopped`, `scans_deleted`,
`scans_copied`, `dev_mode_toggled`, `device_id_changed`, plus generic
fallbacks from supervisor tools.

Read back via Settings → Supervisor → View Action Log (paginated, 200
entries per page).

### Supervisor PIN

`App/data/supervisor.json`:

```json
{"salt":"<32 hex>", "hash":"<64 hex (pbkdf2_hmac sha256, 100k iters)>"}
```

Default PIN: `0000`. The Settings → Supervisor section nags the user
on first launch to change it.

`is_unlocked()` is true for 10 minutes after a successful PIN entry.
`ensure_unlocked(parent)` is called by every admin tile.

No PIN recovery flow exists. If forgotten, delete `supervisor.json`
on disk and the app falls back to the default PIN.

---

## 13. Theming

`gui/theme.py` defines LIGHT and DARK palettes. The active palette is
selected at import time from `App/data/theme.json` (`{"theme": "dark"}`
or `"light"`).

`gui/main_window.py` re-exports every key from the active palette as a
module constant (`BG`, `PRIMARY`, `TEXT`, …). Everything else imports
from `gui.main_window`, so a theme switch is just "edit theme.json,
restart app."

To add a new theme key:
1. Add it to both LIGHT and DARK in `gui/theme.py`.
2. Add a `KEY = _P['KEY']` line in `gui/main_window.py`.
3. Import and use it wherever needed.

Hot-swap is not supported by design — would require rebuilding every
already-constructed widget for what is effectively a settings-page
preference on a kiosk.

---

## 14. ROS launch file

`~/catkin_hesai_ros2/lidar_imu_record.launch`. Owned by the ROS
workspace, not by this repo, but referenced via `cfg.LAUNCH_FILE`.
Minimal — only the three nodes recording needs:

```xml
<launch>
  <arg name="data_path" default="$(env HOME)/testruns/..."/>

  <node pkg="hesai_ros_driver"  name="hesai_ros_driver_node"
        type="hesai_ros_driver_node"
        launch-prefix="chrt -f 50 taskset -c 1-3"/>

  <node pkg="xsens_mti_driver"  name="xsens_mti_node"
        type="xsens_mti_node"
        launch-prefix="chrt -f 50 taskset -c 0">
      <rosparam command="load" file="$(find xsens_mti_driver)/param/xsens_mti_node.yaml"/>
  </node>

  <node pkg="rosbag" type="record" name="rosbag_record"
        args="--split --duration=30 -b 1024
              -o $(arg data_path)/LITHIC_PRO_V2
              /hesai/pandar /imu/data"/>
</launch>
```

Key things to understand:

- **Real-time priority + core pinning** is on every sensor driver.
  The `chrt -f 50` only takes effect because of the `rtprio 99` limit
  added by `setup-system.sh`.
- **`rosbag record -b 1024`** gives rosbag a 1 GB in-memory buffer so a
  multi-second disk stall (NTFS hiccup) doesn't drop sensor messages.
- **No compression** (`--lz4` / `--bz2` absent) — a truncated compressed
  bag tail is unreadable, an uncompressed one is recoverable via
  `rosbag reindex`.

---

## 15. Logging — what gets recorded where

| Where | What | Lifetime |
|---|---|---|
| `/tmp/lithic-app-launch.log` | `run.sh` tee'd output (stdout + stderr) of the entire app launch | Reset on each launch |
| `<scan_folder>/app.log` | `ros.app_log(...)` calls, one per console message + timestamp | Per scan |
| `App/data/action_log.jsonl` | Operator actions (start/stop/delete/copy/admin) | Forever, ~150 bytes/action |
| `~/.ros/log/<uuid>/` | Per-node ROS session logs (roslaunch managed) | Pruned > 7 days via systemd timer |
| `journalctl` | Kernel + systemd-managed services | 200 MB rolling (`SystemMaxUse` from `setup-system.sh`) |
| Bag files | Raw sensor data, named `LITHIC_PRO_V2_YYYY-MM-DD-HH-MM-SS_<N>.bag` | Forever, until copied/deleted |
| `<scan_folder>/scan_info.json` | Scan metadata (site, floor, part, in-charge, started_at, stopped_at, device) | Forever |

The on-screen console (the QTextEdit at the bottom of the scan player)
shows a filtered subset: only `INFO`, `WARN`, `ERROR`, `BAG` levels. The
raw `ROS` firehose is dropped from the console but still tee'd to
`<scan_folder>/app.log`.

---

## 16. Operational guide

### Starting the app

- **At boot** (autostart): no action required, just power on. The
  XDG-autostart `.desktop` runs `run.sh`.
- **Manually**: double-click the desktop launcher, or
  `bash run.sh` from a terminal in the app directory.

### Running a scan

1. Home → Start a New Scan (or use the Scan tab in the sidebar).
2. Pick site / floor / part / in-charge. The DeviceStatusPanel on the
   right must show LiDAR + IMU as ready before Start enables.
3. Click Start. The startup-guidance overlay walks through:
   - **45 s STAY STILL** — IMU static-init.
   - **15 s ROTATE — STAY IN PLACE** — rotate device only, don't walk.
   - **5 s START MOVING** — fades out, begin walking.
4. Watch the console for per-bag summary lines and any WARN/ERROR.
5. Click Stop when done. The Stopping… overlay shows for ~5–10 s while
   ROS shuts down, then the player flips to a STOPPED state with
   "Next Scan" available.

### Force-stop everything

If the GUI is unresponsive but you don't want to kill the OS:

```bash
pkill -KILL -f "python3 App/main.py"
pkill -f roslaunch
pkill -f roscore
pkill -f rosmaster
pkill -f xsens_mti_node
pkill -f hesai_ros_driver_node
```

If the entire OS is frozen: the hardware reset button is the only path
(no hardware watchdog is enabled in this build).

### Disable autostart

```bash
mv ~/.config/autostart/INKERS-Data-Collector.desktop{,.disabled}
```

### Copy data off

1. Plug in a USB drive (NTFS or exFAT, ideally ext4-formatted on Linux).
2. Go to Data Transfer. The external drive card should show up within
   ~5 s.
3. Select one or more scans, click Copy to External.
4. Wait for the modal progress dialog to finish. Final dialog reports
   any unreadable / missing files (only happens with corrupted source
   data — see [§11.2](#112-copy)).
5. Click Eject when done; the dialog waits for `udisksctl power-off`
   to return before declaring the drive safe to unplug.

---

## 17. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| **App doesn't auto-launch on boot** | Missing/renamed `.desktop` in `~/.config/autostart/` | Re-run `bash install.sh` |
| **Splash screen flashes then disappears** | Python import error before MainWindow | `tail /tmp/lithic-app-launch.log` |
| **LiDAR pill is DEAD even though driver is up** | `rosnode list` slow → 12 s LIVE_TIMEOUT in `_NodeMonitor` | Check `rosnode list` manually; if it hangs, master is stuck |
| **IMU dropping ~50 msgs / 30-s bag** | A second process is reading `/dev/ttyUSB0` | Already fixed via DeviceMonitor.pause(). If recurring, check if `minicom`/`screen` is open |
| **Bag stays at `.bag.active` after Stop** | `roslaunch` was force-killed; rosbag never finalized | App's orphan recovery on next launch will rename + write scan_info.json |
| **`Delete` fails leaving bytes behind** | NTFS metadata corruption on data drive (`ls -la` shows `-?????????`) | App now falls back to `rm -rf`; for the underlying issue, `sudo ntfsfix /dev/nvme0n1p1` |
| **20-min scan crashes the OS** | SD card filled (look at `df /`) | Already prevented: QA terminates below 1.5 GB free. Run cleanup: `sudo apt clean; rm -rf ~/.cache/* ~/.local/share/Trash/*` |
| **`ulimit -r` prints 2, drivers can't get realtime** | rtprio limit not applied | Log out and back in (PAM only reads limits on login). If still 2, check `/etc/security/limits.d/99-lithic-rtprio.conf` |
| **`vcgencmd get_throttled` returns non-zero** | Undervolt / thermal | Better PSU; better cooling. Currently no in-app watchdog for this |
| **Two scans launched in parallel** | Single-instance guard via `QLocalServer` failed (stale socket) | Restart, or `rm /tmp/LithicProV2DataCollector` |

For anything not covered: enable persistent journald (already done by
`setup-system.sh`) and after the next failure:

```bash
# Most recent boot
journalctl -k -b 0 --no-pager | tail -100

# Previous boot (post-crash forensics)
journalctl -k -b -1 --no-pager | tail -100

# What happened to the app process
journalctl _COMM=python3 -b 0 --no-pager
```

---

## 18. Development guide

### Running the app for development

```bash
cd New_Lithic_App
# Optional: enter your venv
source ~/.venvs/inkers/bin/activate
# Source ROS
source ~/catkin_hesai_ros2/devel/setup.bash
# Run directly (skips run.sh's bash plumbing)
python3 App/main.py
```

If you're hacking on the UI and don't have ROS / hardware connected,
set `DEV_MODE = True` in `config.py`. The DeviceMonitor will report all
green, QAWorker will auto-pass, and `_ROSSpinThread` emits mock colored
frames for any `cfg.VIEW_TOPIC`.

### Adding a new ROS driver

1. Add it to `cfg.DRIVERS` with its topic → expected msg-count per 30-s
   bag.
2. Add it to `cfg.DRIVER_NODES` so the LIVE/DEAD pill works.
3. Add a buffer entry to `cfg.BUFFER`.
4. Add a probe to `gui/device_status.py:DeviceMonitor._actually_check`.
5. Add `_DRIVER_LABEL[<key>] = '<display>'` in `core/qa_worker.py`.
6. Update the launch file to include the driver.
7. Make sure the topics are listed in the `rosbag record` args of the
   launch file.

### Adding a new GUI page

1. Create `gui/<your_page>.py` with a `QWidget` subclass.
2. Import + instantiate in `MainWindow._build_content_stack`.
3. Add it to the `items` list in `_build_sidebar` with a glyph + label.
4. Wire any navigation signals via `self._shell.jump_to('<tab>')`.

### Touching `config.py` from the GUI

Use `config.patch_config_py(key, value)`. Adding a new key requires
editing the function to add a regex branch. The patcher is intentionally
keyed to specific names so a typo on the calling side can't blow away
unrelated config.

### Coding conventions

- All imports of theme colors come from `gui.main_window`, NOT from
  `gui.theme` directly, so a theme change is a single import path.
- Long-running blocking work goes in QThread workers or daemon threads,
  never on the Qt event loop.
- Qt cross-thread signal emissions use the default `AutoConnection`,
  which is queued for non-Qt-thread emitters — that's the right
  default in this app. Explicit `QueuedConnection` only where reentrancy
  would matter (e.g. `_ros_stop_done → _finish_stop`).
- Subprocess timeouts everywhere. No `subprocess.run` without a
  `timeout=` keyword. We've been bitten by hung child processes
  blocking the GUI thread.
- No new file should be a `*.md` unless someone explicitly asks for
  documentation. (Self-imposed; reduces drift between code and docs.)

### What NOT to do

- **Don't call `rospy.signal_shutdown()`** between scans. Once tripped,
  rospy is permanently dead in this process — the next `init_node` is
  garbage. `_ROSSpinThread.stop()` only sets a flag; cleanup happens
  through topic unsubscription, not master disconnect.
- **Don't poll the scan folder on a timer.** Every disk read competes
  with rosbag's writes; we observed ~50 IMU messages dropped per 30-s
  bag from a 1-Hz `os.listdir` loop. The inotify path
  (`QFileSystemWatcher`) is mandatory.
- **Don't restrict the GUI's cpuset via `taskset`.** The drivers'
  `launch-prefix=taskset -c 0` inherits the cpuset from the calling
  shell. We learned this the hard way: a `taskset -c 2-3` on the
  Python process made it impossible for xsens to ever land on core 0.
- **Don't write to `App/data/` without an atomic temp + fsync + rename**
  pattern. Power-loss in the middle of writing `supervisor.json` or
  `scan_info.json` would corrupt the file otherwise.

---

## 19. Known design decisions

A few decisions that look weird but are deliberate, in case you're
tempted to "improve" them:

- **The GUI doesn't link rospy/roscpp.** All ROS state lives in
  subprocesses. Easier to reason about, easier to recover when something
  dies. The image-display path is the only place we use rospy at all.
- **roscore is persistent across scans.** Previously we let roslaunch
  spawn its own master and killing roslaunch took it down — the next
  scan would fail because rospy still thought it was talking to the old
  master. Owning roscore ourselves makes "Next Scan" actually work.
- **The bag-rotation log builds its own per-driver block** instead of
  using the `bag_checked` signal from QAWorker. Two readers means two
  rosbag.Bag() opens per close, but it lets us render BAG-level white
  text immediately, while QA still drives terminate decisions
  independently. Single source of truth for the screen, separate one
  for the policy decision.
- **The 5-second `_NodeMonitor` poll interval** is the result of
  measurement, not guessing. At 1-second polling we observed ~50 IMU
  msgs/bag dropped from the rosnode-list subprocess load; at 5 s the
  problem disappears and the operator's "is the driver alive?" feedback
  is still under 6 seconds.
- **`fsync` is mandatory in the copy worker.** We learned the hard way:
  pulling the USB the moment "Copied" was displayed truncated the tail
  of the last bag because the data was still in the kernel's page cache.
- **Recovery rewrites `.bag.active → .bag` rather than running
  `rosbag reindex`.** Reindex requires `rospkg` paths and ROS env
  sourced; renaming is a single syscall. The user can still reindex
  off-device when needed.
- **The supervisor PIN store is intentionally simple.** PBKDF2-SHA256
  with 100k iterations and a per-PIN salt is sufficient for a 4-digit
  PIN on a physical kiosk — the attack model is "a curious operator,"
  not "a remote attacker." Brute-forcing 10⁴ pins still takes a few
  seconds per attempt with the iteration count.

---

## 20. Glossary

| Term | Meaning |
|---|---|
| **Bag** | A rosbag-formatted file (`*.bag`) containing one or more topic recordings. We use `--split --duration=30` so a long scan becomes a sequence of 30-second bags. |
| **`.bag.active`** | An rosbag in flight. Renamed to `.bag` when its 30-second window ends or recording stops. |
| **Driver** | One of `hesai_ros_driver_node`, `xsens_mti_node`, `seek_driver_node`. A ROS node that publishes a topic with raw sensor data. |
| **Drift** | What the IMU shows during the STAY STILL phase. A short static init period gives the IMU a stable bias estimate. |
| **DUMP_PATH** | Root of all scan data. Configurable. `/media/<user>/DATA` on this build. |
| **Orphan scan** | A folder with `.bag` files but no valid `scan_info.json`. Usually the result of a force-killed app. The recovery flow promotes them. |
| **Recovered scan** | A scan whose `scan_info.json` was written by the recovery flow, not by a normal Stop. Flagged with `"recovered": true`. |
| **Scan folder** | The per-scan directory under `<DUMP_PATH>/dumps/<month>/<DD_MMM>/`. Named with site, floor, part, and timestamp. |
| **scan_info.json** | The per-scan metadata file. Written at successful Stop or by orphan recovery. Required for `list_scans` to consider a folder a "scan." |
| **DEV_MODE** | Master switch in `config.py`. When True, hardware is mocked end-to-end so the GUI can run on any machine. Auto-disables QA terminations and replaces sensor probes with mocks. |
