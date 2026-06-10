#!/bin/bash
# One-time installer for the INKERS Data Collector app.
# Registers the desktop launcher and installs any missing Python deps.
#
# Run WITHOUT sudo — every target is user-level (~/.local, ~/Desktop)
# and sudo drops your conda / venv activation (so pip would run against
# the system Python and hit Debian's externally-managed-environment block).
set -e
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
DESKTOP_FILE="${APP_DIR}/INKERS-Data-Collector.desktop"

# -------- guard against sudo ------------------------------------------------
if [ "$(id -u)" = "0" ]; then
    cat >&2 <<'EOF'
[install] ✗ Do not run this script with sudo.
          All targets are user-level (~/.local/share/applications and
          ~/Desktop). sudo also drops your ROS / conda / venv activation
          which would force pip into the system Python and trigger the
          PEP 668 "externally-managed-environment" error.

          Re-run as your normal user:    bash install.sh
EOF
    exit 1
fi

# -------- pick the right python / pip --------------------------------------
PY="$(command -v python3)"
echo "[install] Using python: ${PY}"
${PY} -c 'import sys; print(f"           {sys.executable}\n           {sys.version.splitlines()[0]}")'

# -------- check which packages are already importable ---------------------
need_install=0
missing=()
for mod in PySide6 rospkg numpy; do
    if ! ${PY} -c "import ${mod}" >/dev/null 2>&1; then
        missing+=("${mod}")
        need_install=1
    fi
done

if [ ${need_install} -eq 0 ]; then
    echo "[install] All Python deps already importable — skipping pip."
else
    echo "[install] Missing modules: ${missing[*]}"

    # Detect if we're in a venv / conda — pip can install freely there.
    in_env=$(${PY} -c \
        'import sys, os; print(1 if (sys.prefix != sys.base_prefix) or os.environ.get("CONDA_PREFIX") else 0)')

    if [ "${in_env}" = "1" ]; then
        echo "[install] In an active Python environment — installing with pip..."
        ${PY} -m pip install -r App/requirements.txt
    else
        # System Python on RPi OS / Debian — PEP 668 blocks plain pip.
        # Prefer apt for what's packaged; fall back to pip --user
        # --break-system-packages for the rest.
        echo "[install] System Python (PEP 668). Trying apt packages first..."
        apt_pkgs=()
        case " ${missing[*]} " in *" numpy "*)   apt_pkgs+=("python3-numpy") ;; esac
        case " ${missing[*]} " in *" rospkg "*)  apt_pkgs+=("python3-rospkg") ;; esac
        case " ${missing[*]} " in *" PySide6 "*) apt_pkgs+=("python3-pyside6.qtwidgets" "python3-pyside6.qtcore" "python3-pyside6.qtgui" "python3-pyside6.qtnetwork") ;; esac

        if [ ${#apt_pkgs[@]} -gt 0 ]; then
            echo "[install] Attempting:  sudo apt install ${apt_pkgs[*]}"
            if sudo apt-get install -y "${apt_pkgs[@]}" 2>/dev/null; then
                echo "[install] apt install succeeded."
            else
                echo "[install] apt install failed (packages may not exist on this distro)."
            fi
        fi

        # Re-check what's still missing after apt.
        still_missing=()
        for mod in PySide6 rospkg numpy; do
            ${PY} -c "import ${mod}" >/dev/null 2>&1 || still_missing+=("${mod}")
        done

        if [ ${#still_missing[@]} -gt 0 ]; then
            echo "[install] Still missing: ${still_missing[*]}"
            echo "[install] Falling back to:  pip install --user --break-system-packages"
            ${PY} -m pip install --user --break-system-packages \
                -r App/requirements.txt
        fi
    fi
fi

# -------- Render the .desktop template with this device's APP_DIR ---------
# The repo's INKERS-Data-Collector.desktop uses the placeholder __APP_DIR__
# instead of a hardcoded path. .desktop files don't honour $HOME or shell
# expansion in Exec=/Path=/Icon=, so we substitute the absolute path here
# at install time. The rendered file is what gets copied to the three
# install destinations (apps menu, Desktop, autostart).
RENDERED_DESKTOP="$(mktemp --suffix=.desktop)"
trap 'rm -f "${RENDERED_DESKTOP}"' EXIT
# Use '|' as the sed delimiter so '/' inside APP_DIR doesn't need escaping.
sed "s|__APP_DIR__|${APP_DIR}|g" "${DESKTOP_FILE}" > "${RENDERED_DESKTOP}"
echo "[install] Rendered .desktop for APP_DIR=${APP_DIR}"

# -------- Register the .desktop entry under the user's app menu -----------
APPS_DIR="${HOME}/.local/share/applications"
mkdir -p "${APPS_DIR}"
cp "${RENDERED_DESKTOP}" "${APPS_DIR}/$(basename "${DESKTOP_FILE}")"
echo "[install] Registered ${APPS_DIR}/$(basename "${DESKTOP_FILE}")"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${APPS_DIR}" >/dev/null 2>&1 || true
fi

# -------- Put a copy on the user's Desktop --------------------------------
DESKTOP_DIR="${HOME}/Desktop"
if [ -d "${DESKTOP_DIR}" ]; then
    cp "${RENDERED_DESKTOP}" "${DESKTOP_DIR}/$(basename "${DESKTOP_FILE}")"
    chmod +x "${DESKTOP_DIR}/$(basename "${DESKTOP_FILE}")"
    if command -v gio >/dev/null 2>&1; then
        gio set "${DESKTOP_DIR}/$(basename "${DESKTOP_FILE}")" \
            metadata::trusted true 2>/dev/null || true
    fi
    echo "[install] Placed launcher on ~/Desktop"
fi

# -------- Auto-start on every login (XDG autostart) ------------------------
# Drop the .desktop into ~/.config/autostart so the app launches as soon
# as the desktop session starts. Disable boot-launch later by deleting
# or renaming the file there.
AUTOSTART_DIR="${HOME}/.config/autostart"
mkdir -p "${AUTOSTART_DIR}"
cp "${RENDERED_DESKTOP}" "${AUTOSTART_DIR}/$(basename "${DESKTOP_FILE}")"
chmod +x "${AUTOSTART_DIR}/$(basename "${DESKTOP_FILE}")"
echo "[install] Auto-start enabled (${AUTOSTART_DIR}/$(basename "${DESKTOP_FILE}"))"

# -------- Remind about the root-side system setup -------------------------
# install.sh is intentionally no-sudo (so it doesn't drop the venv).
# All the kernel / udev / systemd / sudoers settings the app needs go
# through setup-system.sh, which the user runs ONCE with sudo.
SYSTEM_SETUP="${APP_DIR}/setup-system.sh"
SENTINEL="/etc/sysctl.d/99-lithic.conf"   # produced by setup-system.sh
echo
if [ -f "${SENTINEL}" ]; then
    echo "[install] System-level setup already present (${SENTINEL} exists)."
else
    cat <<EOF
[install] ⚠ System-level setup has NOT been run on this device.
          Run it once with sudo to apply the kernel/udev/systemd
          settings the scanner relies on (UDP buffer, FTDI latency,
          SCHED_FIFO limit, CPU governor, persistent journald, ROS
          log prune, OOM protection):

              sudo bash ${SYSTEM_SETUP}

          Then log out and back in (so the rtprio limit takes effect).

EOF
fi

echo "[install] ✓ Done."
echo "          Launch from the menu, desktop icon, or:  bash ${APP_DIR}/run.sh"
