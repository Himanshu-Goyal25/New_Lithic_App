# INKERS Data Collector — LITHIC PRO V2

Multi-sensor scanning app for Raspberry Pi 5 (kiosk-style PySide6 GUI):
Hesai LiDAR + Xsens MTi IMU + (optional) Seek Thermal, recorded into
ROS Noetic bags via `rosbag record --split --duration=30`.

This document covers **installing on a fresh device**. The app code
itself is in `App/`; this README is about getting the device ready
for it to actually run reliably.

---

## TL;DR

```bash
# One time, with sudo — sets up kernel / udev / systemd / sudoers.
sudo bash setup-system.sh

# Log out and log back in so PAM re-reads the rtprio limit.

# Once more, WITHOUT sudo — Python deps + desktop launcher + autostart.
bash install.sh
```

After that the app auto-launches on every boot. Verify with `ulimit -r`
(should print `99`) before running a scan — that confirms the rtprio
change took effect.

---

## Prerequisites the scripts assume

| Item | Why |
|---|---|
| Raspberry Pi OS Bookworm (Debian 12) on Pi 5 (BCM2712) | Kernel ≥ 6.12, systemd, `ntfs3` available |
| ROS Noetic installed at `/opt/ros/noetic` | Drivers + rosbag + rospy |
| Catkin workspace at `~/catkin_hesai_ros2` with `lidar_imu_record.launch` | Driver launch file — see `catkin_hesai_ros2/lidar_imu_record.launch` |
| Python 3 with `pip` / `apt` access | The installer auto-picks venv > apt > pip --user |
| Passwordless `sudo` for the GUI user | Needed by `setup-system.sh` and by the OOM helper at runtime |
| Hesai LiDAR on `192.168.1.201`, host NIC on `192.168.1.23` | Configured in `App/config.py` — change there if your subnet differs |
| Xsens MTi on `/dev/ttyUSB0` (FTDI USB-UART, VID `0403`, PID `6001`) at 2 Mbaud | Configured in `App/config.py` |
| Data drive mounted at `/media/<user>/DATA` (`DUMP_PATH` in config.py) | NTFS works but ext4 is strongly recommended |

---

## Step 1 — `sudo bash setup-system.sh`

Run **once per device**. This is the only sudo step. It writes seven
pieces of system configuration; every file goes into a well-known
location with a `99-lithic` prefix so you can find/audit them later.
Idempotent — re-running just overwrites the same files with the
same content.

### 1.1 UDP receive buffer → 32 MB
- **File**: `/etc/sysctl.d/99-lithic.conf`
- **What**: `net.core.rmem_max` and `rmem_default` raised from the
  Debian default of 212 KB to 32 MB.
- **Why**: Hesai LiDAR pushes ~5 MB/s in bursts. With the default
  buffer, if the driver is briefly preempted the kernel silently
  drops UDP packets — no error, just missing scan rows in your bag.

### 1.2 FTDI USB-UART latency_timer → 1 ms
- **File**: `/etc/udev/rules.d/99-lithic-ftdi.rules`
- **What**: udev rule that sets `latency_timer=1` on any FTDI FT232R
  (`0403:6001`) at plug-in. Also applies immediately to whatever is
  currently enumerated.
- **Why**: The FTDI chip's internal latency timer defaults to 16 ms.
  That batches ~12.5 KB of IMU bytes at 2 Mbaud before forwarding
  them — which overflows the TTY buffer and shows up as Xsens
  message drops (~50 / 30-s bag was the symptom we tracked down).

### 1.3 rtprio limit → 99 for the target user
- **File**: `/etc/security/limits.d/99-lithic-rtprio.conf`
- **What**: Raises the SCHED_FIFO priority cap from Debian's default
  of 2 to 99 for the user running the GUI.
- **Why**: The launch file wraps each driver with `chrt -f 50`. With
  the default cap of 2, chrt can't elevate the priority and silently
  falls back to CFS — drivers can be preempted by random kernel work.
- **Note**: PAM only reads `limits.d` on login. You **must log out
  and back in** (or reboot) for this to take effect. Verify with
  `ulimit -r` in a fresh shell — it should print 99.

### 1.4 CPU governor → performance
- **File**: `/etc/systemd/system/lithic-cpu-performance.service`
- **What**: Oneshot systemd unit, enabled at boot, that writes
  `performance` to every CPU core's `scaling_governor`. `ExecStop`
  restores `ondemand` if the unit is ever disabled.
- **Why**: The `ondemand` governor drops core frequency between
  sensor bursts, adding ~10 ms wake-up latency per burst. Over a
  20-minute scan that accumulates into ragged Hz on LiDAR/IMU and
  visible jitter in the recorded data.

### 1.5 Persistent journald (200 MB cap)
- **File**: `/etc/systemd/journald.conf.d/99-lithic.conf`,
  `/var/log/journal/` created
- **What**: Switches journald from `Storage=volatile` (logs live
  only in `/run`, wiped on reboot) to `Storage=persistent` with
  `SystemMaxUse=200M` and `SystemKeepFree=500M`.
- **Why**: A system hang or crash leaves you no postmortem evidence
  unless the journal survives the reboot. The 200 MB cap stops the
  journal itself from filling the SD card. After this is in place,
  `journalctl -k -b -1` works after the next event.

### 1.6 Daily ROS log prune
- **Files**: `/etc/systemd/system/lithic-ros-log-prune.service` and
  `.timer`
- **What**: systemd timer that fires 10 minutes after boot and then
  every 24 hours, removing any directory under `<user>/.ros/log/`
  older than 7 days.
- **Why**: Every `roslaunch` invocation creates a new
  `~/.ros/log/<uuid>/` directory with per-node logs. These never get
  cleaned automatically and can grow to GBs on a long-lived kiosk —
  one of the contributing causes of the 99%-full root partition
  that caused a previous full-OS hang.

### 1.7 OOM-protection helper + sudoers rule
- **Files**: `/usr/local/sbin/lithic-oom-protect` (executable),
  `/etc/sudoers.d/99-lithic-oom` (passwordless sudo for that one
  binary only, validated with `visudo -c` before installation)
- **What**: A tiny shell helper that writes `-1000` to a list of
  `/proc/<pid>/oom_score_adj` files. The sudoers rule lets the
  target user invoke it without a password. The app calls this
  helper after roslaunch with the PIDs of the driver and rosbag
  processes.
- **Why**: Decreasing `oom_score_adj` (more protection) requires
  `CAP_SYS_RESOURCE`. With `-1000`, the kernel's OOM-killer will
  literally never select those PIDs — it would kill Chromium, the
  desktop, even `systemd-resolved` before touching a driver.

---

## Step 2 — log out / log back in

Skipping this is the #1 way to think the setup didn't work. The
rtprio change in 1.3 is PAM-managed; PAM only re-reads
`/etc/security/limits.d/` on login.

```bash
# Quickest path that keeps you on the device:
exec sudo systemctl restart display-manager
# Or just reboot the Pi.
```

Verify in a fresh terminal:
```bash
ulimit -r        # should print 99
chrt -f 10 sleep 1   # should NOT print "Operation not permitted"
```

---

## Step 3 — `bash install.sh`

Run **as the regular user** (NOT sudo). The script refuses to run
as root because sudo would drop your Python venv / conda activation
and force `pip` into the system Python, where Debian's PEP 668
guard blocks it.

What it does:

### 3.1 Detect the Python environment
Prints the interpreter path and version. Tests whether each
required module (`PySide6`, `rospkg`, `numpy`) is importable.

### 3.2 Install missing Python deps
Three-tier strategy, picked automatically:
- If a venv or conda env is active → `pip install -r App/requirements.txt`
- Else, if the system Python is in use → try `apt install` for
  whichever of `python3-pyside6.*`, `python3-rospkg`,
  `python3-numpy` are missing.
- Whatever apt can't provide → `pip install --user
  --break-system-packages -r App/requirements.txt` as a last resort.

### 3.3 Register the launcher in the app menu
Copies `INKERS-Data-Collector.desktop` into
`~/.local/share/applications/` and runs `update-desktop-database`
so the desktop environment picks it up.

### 3.4 Place a launcher on the Desktop
If `~/Desktop` exists, drops a copy there too and marks it
trusted via `gio set ... metadata::trusted true` so
double-click works without the "Untrusted launcher" prompt.

### 3.5 Enable auto-start on every login
Copies the same `.desktop` into `~/.config/autostart/`. The XDG
autostart spec picks this up at session start (after auto-login on
a kiosk Pi), so the app comes up automatically on every boot. To
disable later, just delete or rename that file.

### 3.6 Check the sudo-side setup was already run
Looks for `/etc/sysctl.d/99-lithic.conf` (created by
`setup-system.sh`). If missing, prints a reminder to run
`setup-system.sh` first.

---

## Verifying the install

After Step 3, before running a real scan:

```bash
# 1. rtprio limit took effect
ulimit -r                               # → 99

# 2. UDP buffer is large
sysctl net.core.rmem_max                # → 33554432

# 3. FTDI latency low
cat /sys/bus/usb-serial/devices/ttyUSB0/latency_timer   # → 1

# 4. CPU governor is performance
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor   # → performance

# 5. journald is persistent
journalctl --header | head -2           # path should be under /var/log/journal/...

# 6. ROS log prune timer is scheduled
systemctl list-timers lithic-ros-log-prune.timer --no-pager

# 7. OOM helper works
sudo /usr/local/sbin/lithic-oom-protect $$
cat /proc/$$/oom_score_adj              # → -1000
```

All seven should pass. If any fail, re-run the corresponding step
of `setup-system.sh` (the script is idempotent).

---

## What happens at boot, end-to-end

```
power on
 └─ kernel boots (with /etc/sysctl.d/* applied, governor unit started)
     └─ auto-login (configured via raspi-config "Boot to Desktop, Auto-login")
         └─ user session starts
             └─ /etc/security/limits.d/* loaded → rtprio cap = 99
             └─ XDG autostart processes ~/.config/autostart/*.desktop
                 └─ INKERS-Data-Collector.desktop → run.sh
                     └─ run.sh sources venv + ROS workspace
                         └─ python3 App/main.py
                             └─ (operator presses Start)
                                 └─ roslaunch ... lidar_imu_record.launch
                                     ├─ chrt -f 50 taskset -c 1-3 hesai_ros_driver_node
                                     ├─ chrt -f 50 taskset -c 0 xsens_mti_node
                                     └─ rosbag record --split --duration=30 -b 1024 ...
                                 └─ background thread: sudo lithic-oom-protect <PIDs>
                                 └─ DeviceMonitor.pause() (releases /dev/ttyUSB0)
                                 └─ QAWorker.start() — watchdog, disk, per-bag checks
```

---

## Operational notes

### Disable autostart
```bash
mv ~/.config/autostart/INKERS-Data-Collector.desktop{,.disabled}
```

### Where things live afterward
- App code: `App/`
- App config: `App/config.py` (device ID, IPs, driver topics, etc.)
- App launch script: `run.sh`
- ROS launch file: `~/catkin_hesai_ros2/lidar_imu_record.launch`
- Scan data: `/media/<user>/DATA/dumps/...`
- App log per launch: `/tmp/lithic-app-launch.log`
- ROS per-session logs: `~/.ros/log/<uuid>/` (auto-pruned > 7 days)
- System journal: `journalctl -u <unit> -b 0` (current boot) or
  `-b -1` (previous boot)

### Useful one-liners
```bash
# Tail the live app log
tail -f /tmp/lithic-app-launch.log

# Watch what ROS processes are currently up
ps -ef | grep -E "ros|hesai|xsens|record" | grep -v grep

# Inspect a bag without playing it
rosbag info /media/<user>/DATA/dumps/<scan_folder>/<file>.bag

# Verify OOM protection took effect on a running scan
for p in $(pgrep -x hesai_ros_driver_node xsens_mti_node record); do
    echo "$p $(cat /proc/$p/oom_score_adj 2>/dev/null)"
done
```

### Recovering from a force-killed scan
If the app or OS was force-killed mid-scan, `.bag.active` files
will be left behind. The app's orphan-recovery dialog on next
launch handles them, killing any straggler `rosbag`/`roslaunch`/
driver processes and promoting `.bag.active` → `.bag` so they can
be copied/deleted normally.

### NTFS caveat
The data drive on the reference unit is NTFS (via `ntfs3`). This
filesystem has known metadata-corruption issues during heavy
writes; the app already works around them in delete (`rm -rf`
fallback) and copy (size verify + skip-broken). **If you can
afford the migration, reformat the data drive as ext4 — every
NTFS workaround in the app exists because of `ntfs3` quirks.**

---

## File map

```
New_Lithic_App/
├── README.md                              ← you are here
├── setup-system.sh                        ← step 1: sudo, once per device
├── install.sh                             ← step 3: user, once
├── run.sh                                 ← invoked by the .desktop file
├── INKERS-Data-Collector.desktop          ← launcher (menu + Desktop + autostart)
└── App/
    ├── main.py                            ← Qt entry point
    ├── config.py                          ← device-specific knobs
    ├── core/
    │   ├── ros_controller.py              ← roslaunch + rosnode + OOM hook
    │   ├── qa_worker.py                   ← watchdog + disk + per-bag QA
    │   └── audit.py
    ├── gui/                               ← scan player + scan list + dialogs
    └── data/
        ├── sites.csv / incharge.csv       ← operator-facing pick lists
        └── theme.json
```
