#!/bin/bash
# Launcher for the INKERS Data Collector.
#
# When invoked from the desktop launcher this is a non-interactive shell
# (~/.bashrc is NOT sourced), so we must activate the user's Python venv
# and source the ROS workspace explicitly here.
#
# All output is tee'd to /tmp/lithic-app-launch.log so silent failures
# from a desktop double-click are still recoverable.

LOG="/tmp/lithic-app-launch.log"
{
echo
echo "==================================================================="
echo " $(date -Iseconds)  —  INKERS Data Collector launch"
echo "==================================================================="
echo " USER=${USER}  PWD=${PWD}"
echo " DISPLAY=${DISPLAY:-?}  WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-?}"
echo " XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-?}"

set -e
APP_DIR="$(dirname "$(readlink -f "$0")")"
cd "${APP_DIR}"
echo " App dir: ${APP_DIR}"

# ---------- 1. Activate the Python venv if one exists ----------
# Look for the most common locations on this device. First hit wins.
VENV_CANDIDATES=(
    "${HOME}/.venvs/ros-noetic"
    "${HOME}/.venvs/inkers"
    "${HOME}/venv"
)
VENV=""
for v in "${VENV_CANDIDATES[@]}"; do
    if [ -f "${v}/bin/activate" ]; then VENV="${v}"; break; fi
done

if [ -n "${VENV}" ]; then
    echo " Activating venv: ${VENV}"
    # shellcheck disable=SC1091
    source "${VENV}/bin/activate"
else
    echo " No venv detected — using system python."
fi

# ---------- 2. Source the ROS workspace ----------
if [ -f "${HOME}/catkin_hesai_ros2/devel/setup.bash" ]; then
    echo " Sourcing ROS workspace"
    # shellcheck disable=SC1091
    source "${HOME}/catkin_hesai_ros2/devel/setup.bash"
else
    echo " ⚠ ROS workspace not found at ~/catkin_hesai_ros2 — app will run in non-ROS mode."
fi

export ROS_MASTER_URI=${ROS_MASTER_URI:-http://localhost:11311}

# Hide wf-panel-pi (the RPi top panel) so the app gets a true
# full-screen look without using Wayland xdg-toplevel fullscreen
# state (which would draw above the OSK's wlr-layer-shell surface).
# Wayfire's wfrespawn watcher would normally bring the panel right
# back — kill the watcher FIRST, then the panel.
# The respawn at the bottom of this script restores it after exit
# so the operator can use the Pi menu / desktop to relaunch.
pkill -f "wfrespawn wf-panel-pi" 2>/dev/null || true
pkill -x wf-panel-pi             2>/dev/null || true

# Silence harmless Wayland text-input warnings.
export QT_LOGGING_RULES="qt.qpa.wayland.textinput=false;qt.qpa.wayland.input=false"

echo " python:  $(command -v python3)"
python3 --version || true
echo " checking imports..."
python3 -c "import PySide6; print('   PySide6', PySide6.__version__)" \
    || { echo ' ✗ PySide6 import failed'; exit 2; }

echo " launching App/main.py"
echo "-------------------------------------------------------------------"
# IMPORTANT: do NOT taskset Python to a restricted core list here.
#
# The launch file already pins each driver to its own core
# (taskset -c 0 for xsens, 1-3 for hesai/seek). When the Python
# process is restricted to cores 2-3, the entire subprocess tree
# (bash → roslaunch → driver processes) inherits that restricted
# cpuset, and the driver's launch-prefix=taskset can no longer
# expand back out to core 0. Result: xsens ends up sharing cores
# with everything else and drops ~50 IMU samples per 30-s bag.
#
# Letting Python use any core costs us at most a tiny bit of
# scheduler interference; the kernel does a fine job of keeping
# our (mostly-idle) Qt event loop off cores that are CPU-bound.
#
# NOT exec — we need to return to this shell after Python exits so
# the cleanup below (respawn wf-panel-pi) actually runs.
set +e
python3 App/main.py
PY_RC=$?
set -e

echo " app exited (rc=${PY_RC}) — restoring wf-panel-pi"
# Re-launch the RPi top panel under its respawn watcher, detached
# from this shell so it survives our exit. Without this, the
# operator would log back into a panel-less desktop until next
# session login.
setsid /bin/sh -c "wfrespawn wf-panel-pi" >/dev/null 2>&1 < /dev/null &
disown 2>/dev/null || true

exit "${PY_RC}"
} 2>&1 | tee -a "${LOG}"
