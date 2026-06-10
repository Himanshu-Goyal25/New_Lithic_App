#!/bin/bash
# ─── Lithic Pro V2 — one-time system setup ───────────────────────────────────
#
# Run ONCE per device, with sudo:
#     sudo bash setup-system.sh
#
# Configures kernel + systemd + udev settings the app needs to run reliably:
#
#   1. UDP rmem 32 MB           (Hesai LiDAR can't drop packets in burst)
#   2. FTDI latency_timer = 1ms (Xsens IMU stays at clean 200 Hz)
#   3. rtprio 99 for the user   (drivers can run SCHED_FIFO via chrt -f)
#   4. CPU governor=performance (no wakeup latency between sensor bursts)
#   5. Persistent journald      (logs survive a crash for postmortem)
#   6. Daily ROS log prune      (prevents SD card from filling up)
#   7. OOM-protect helper       (OOM killer can never pick the drivers)
#   (RPi OS's bundled `squeekboard` is the on-screen keyboard — the
#    app drives it via DBus from gui/osk.py, no extra packages needed.)
#
# Idempotent — safe to re-run after upgrades. The .desktop launcher,
# Python deps and autostart entry are handled by the no-sudo
# install.sh script — run that as the regular user afterwards.
#
set -e

# ─── must be root ───────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    echo "[setup-system] ✗ This script must run with sudo:" >&2
    echo "                 sudo bash $(basename "$0")" >&2
    exit 1
fi

# ─── identify the target (non-root) user that will run the GUI ─────────────
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null)}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    echo "[setup-system] ✗ Could not determine the non-root user." >&2
    echo "                 Re-run as:  sudo bash $(basename "$0")" >&2
    exit 1
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
if [ ! -d "$TARGET_HOME" ]; then
    echo "[setup-system] ✗ Home dir for $TARGET_USER not found." >&2
    exit 1
fi
echo "[setup-system] Target user: $TARGET_USER  ($TARGET_HOME)"
echo

# ─── 1. UDP receive buffer ──────────────────────────────────────────────────
echo "[1/7] sysctl: raise UDP rmem so Hesai burst packets don't drop"
cat >/etc/sysctl.d/99-lithic.conf <<'EOF'
# Lithic Pro V2 — sensor read-path buffers
# Hesai LiDAR pushes ~5 MB/s in burst; default 212 KB silently drops packets.
net.core.rmem_max     = 33554432
net.core.rmem_default = 33554432
net.core.wmem_max     = 33554432
EOF
sysctl --system 2>&1 | grep -E "rmem|wmem|99-lithic" | head -5 || true

# ─── 2. FTDI USB-UART latency_timer ─────────────────────────────────────────
echo
echo "[2/7] udev: drop FTDI latency_timer from 16 ms → 1 ms (Xsens IMU)"
cat >/etc/udev/rules.d/99-lithic-ftdi.rules <<'EOF'
# Xsens MTi via FTDI USB-UART — chip-side latency timer 1 ms instead
# of the 16 ms default. Without this, the Xsens 200 Hz stream arrives
# in clumps and overflows the TTY buffer at 2 Mbaud.
ACTION=="add", SUBSYSTEM=="usb-serial", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", ATTR{latency_timer}="1"
EOF
udevadm control --reload-rules
udevadm trigger --action=add --subsystem-match=usb-serial 2>/dev/null || true
# Apply to anything currently enumerated.
for f in /sys/bus/usb-serial/devices/ttyUSB*/latency_timer; do
    [ -f "$f" ] && echo 1 > "$f"
done

# ─── 3. rtprio limit for the target user ────────────────────────────────────
echo
echo "[3/7] limits.d: allow $TARGET_USER to use SCHED_FIFO up to prio 99"
cat >/etc/security/limits.d/99-lithic-rtprio.conf <<EOF
# Lithic Pro V2 — let driver processes run at SCHED_FIFO via 'chrt -f'.
# The Debian default '* hard rtprio 2' is too low to outrun random
# kernel work on the same core, so the drivers can be preempted
# mid-readout and drop sensor samples.
$TARGET_USER  -  rtprio  99
EOF
echo "      ← takes effect on next login for $TARGET_USER"

# ─── 4. CPU governor = performance ──────────────────────────────────────────
echo
echo "[4/7] systemd unit: pin all CPU cores to the performance governor"
cat >/etc/systemd/system/lithic-cpu-performance.service <<'EOF'
[Unit]
Description=Pin all CPU cores to the performance governor for Lithic scans
After=multi-user.target
DefaultDependencies=no

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$g"; done'
ExecStop=/bin/bash -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo ondemand > "$g"; done'

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now lithic-cpu-performance.service >/dev/null 2>&1

# ─── 5. Persistent journald ─────────────────────────────────────────────────
echo
echo "[5/7] journald: persistent with 200 MB cap (post-crash diagnostics)"
mkdir -p /etc/systemd/journald.conf.d
cat >/etc/systemd/journald.conf.d/99-lithic.conf <<'EOF'
[Journal]
# Lithic Pro V2 — keep journals across reboots so a system crash can
# be diagnosed afterwards (instead of evaporating from /run on reboot).
Storage=persistent
SystemMaxUse=200M
SystemKeepFree=500M
SystemMaxFiles=10
EOF
mkdir -p /var/log/journal
chown root:systemd-journal /var/log/journal
chmod 2755 /var/log/journal
systemctl restart systemd-journald
# Force a flush so the in-memory journal lands on disk immediately.
systemctl kill --signal=SIGUSR1 systemd-journald 2>/dev/null || true

# ─── 6. Daily ROS log prune ─────────────────────────────────────────────────
echo
echo "[6/7] timer: daily prune of $TARGET_HOME/.ros/log/* older than 7 days"
cat >/etc/systemd/system/lithic-ros-log-prune.service <<EOF
[Unit]
Description=Prune ROS session logs older than 7 days under ~/.ros/log

[Service]
Type=oneshot
User=$TARGET_USER
Group=$TARGET_USER
ExecStart=/usr/bin/find $TARGET_HOME/.ros/log -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +
EOF
cat >/etc/systemd/system/lithic-ros-log-prune.timer <<'EOF'
[Unit]
Description=Daily ROS session-log prune for Lithic Pro V2

[Timer]
OnBootSec=10min
OnUnitActiveSec=1d
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now lithic-ros-log-prune.timer >/dev/null 2>&1

# ─── 7. OOM-protect helper + sudoers rule ───────────────────────────────────
echo
echo "[7/7] OOM protection: helper binary + passwordless sudoers entry"
cat >/usr/local/sbin/lithic-oom-protect <<'EOF'
#!/bin/bash
# Lower oom_score_adj to -1000 (OOM-killer will never pick these PIDs).
# Required because non-root cannot *decrease* oom_score_adj.
set -e
for pid in "$@"; do
    case "$pid" in
        [1-9]*[0-9]) ;;
        *) continue ;;
    esac
    [ -w "/proc/$pid/oom_score_adj" ] || continue
    echo -1000 > "/proc/$pid/oom_score_adj" 2>/dev/null || true
done
EOF
chmod 755 /usr/local/sbin/lithic-oom-protect

cat >/etc/sudoers.d/99-lithic-oom <<EOF
# Allow the Lithic app to mark its driver PIDs as OOM-protected.
$TARGET_USER ALL=(root) NOPASSWD: /usr/local/sbin/lithic-oom-protect
EOF
chmod 440 /etc/sudoers.d/99-lithic-oom
visudo -c -f /etc/sudoers.d/99-lithic-oom >/dev/null

# (On-screen keyboard: the app uses RPi OS's bundled `squeekboard`,
#  driven directly via DBus from `gui/osk.py`. squeekboard is part of
#  the default RPi OS Bookworm image and starts with the session —
#  no apt step required here.)

# ─── summary ────────────────────────────────────────────────────────────────
echo
echo "[setup-system] ✓ All system settings installed."
echo
echo "Next steps:"
echo "  1. Log out and back in (or reboot) so $TARGET_USER picks up the new"
echo "     rtprio limit. Verify with:  ulimit -r   (should print 99)"
echo "  2. As $TARGET_USER, run:        bash install.sh"
echo "     to install Python deps + the desktop launcher + autostart entry."
