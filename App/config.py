import os
import sys as _sys

# ── Development mode ────────────────────────────────────────────────────────
# True  → run without ROS/hardware, uses mock data and dummy video
# False → production, requires ROS + all hardware connected
DEV_MODE = False

# ── Application version ─────────────────────────────────────────────────────
# Single source of truth for the app version. Surfaced via
# QCoreApplication.setApplicationVersion() in main.py.
VERSION = '1.1.2'

# ── Device identity ─────────────────────────────────────────────────────────
DEVICE = 'LITHIC_PRO_V2'

# ── ROS workspace + launch ──────────────────────────────────────────────────
SETUP_BASH  = os.path.expanduser('~/catkin_hesai_ros2/devel/setup.bash')
LAUNCH_FILE = os.path.expanduser('~/catkin_hesai_ros2/lidar_imu_record.launch')

# ── Drivers: key → {ros_topic: msg_count_threshold per 30s bag} ────────────
# Thermal (seek) is temporarily out of the QA + UI loop — the camera is
# not connected in this configuration. To re-enable, uncomment the
# 'seek' block here AND the matching BUFFER entry below. The launch
# file still spawns seek_driver; rosbag silently records nothing for
# the missing topics, which is harmless.
DRIVERS = {
    'hesai': {
        '/hesai/pandar':                          595,   # 20 Hz × 30 s
    },
    # 'seek': {
    #     '/seek_camera/displayImage':              270,   # 9 Hz
    #     '/seek_camera/temperatureImageCelcius':   270,
    # },
    'xsens': {
        '/imu/data':                             5990,   # 200 Hz × 30 s
    },
    # Watchdog-only entry: rosbag has no source topic of its own but
    # its node MUST be alive — its silence means recording isn't
    # happening. No per-topic counts (empty dict skips the bag check
    # but the node-liveness check still runs).
    'rosbag': {},
}

# ── Topics to display as live video in the scan screen ─────────────────────
# Empty — no thermal camera right now means no live preview anywhere.
VIEW_TOPIC = {}

# ── Driver key → expected ROS node name ────────────────────────────────────
# Used by the status card to show LIVE / DEAD per driver via
# `rosnode list`. If the node name in your launch file is different,
# change it here. Listing a node that the launch never starts will
# simply show that driver as DEAD throughout the scan.
DRIVER_NODES = {
    'hesai':  '/hesai_ros_driver_node',
    'xsens':  '/xsens_mti_node',
    # 'seek': '/seek_driver_node',    # re-enable when thermal returns
    'rosbag': '/rosbag_record',
}

# ── QA: per-driver buffer before auto-terminate ────────────────────────────
BUFFER = {
    'lidar': 20,
    'imu':   50,
    # 'seek':  30,
}

# ── Storage root ────────────────────────────────────────────────────────────
DUMP_PATH = '/media/cm5-v1/DATA'

# ── Cogence API ─────────────────────────────────────────────────────────────
COGENCE_API_URL = ''
COGENCE_API_KEY = ''

# ── Minimum free disk space (GB) before auto-terminate ─────────────────────
MIN_DISK_GB = 5

# ── Device readiness probes used by gui.device_status ──────────────────────
LIDAR_IP      = '192.168.1.201'      # Hesai (from catkin_hesai_ros2/.../config.yaml)
LIDAR_HOST_IP = '192.168.1.23'       # expected host NIC IP — leave '' to skip the check

# Seek Thermal (USB)
SEEK_USB_VID  = '289d'

# Xsens IMU — connected via an FTDI USB-UART adapter. Checking just the
# FTDI VID is not enough (the cable stays enumerated even when the IMU
# is unpowered), so the readiness probe also opens the serial port and
# looks for the Xsens MTi packet preamble 0xFA 0xFF.
XSENS_FTDI_VID    = '0403'           # FTDI Limited
XSENS_FTDI_PID    = '6001'           # FT232R
XSENS_SERIAL_PORT = '/dev/ttyUSB0'   # set to '/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_<serial>-if00-port0' to lock to a specific cable
XSENS_SERIAL_BAUD = 2000000


# Legacy Xsens direct-USB VID — kept for older check paths that may
# still reference it. The FTDI fields above are the authoritative ones.
XSENS_USB_VID = '2639'

# Alias kept for compatibility with reference code paths.
SEEK_USB_VENDOR = SEEK_USB_VID

# Optional kiosk display resolution (None = use the screen's native size).
DISPLAY_RESOLUTION = None

# ── Local data files ────────────────────────────────────────────────────────
DATA_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
SITES_CSV    = os.path.join(DATA_DIR, 'sites.csv')
INCHARGE_CSV = os.path.join(DATA_DIR, 'incharge.csv')


# ── Config patcher ──────────────────────────────────────────────────────────
import re as _re_cfg


def patch_config_py(key: str, value) -> bool:
    """Edit config.py in-place and update the live module variable immediately.
    Atomic write (tmp + fsync + replace). Returns True on success."""
    global DEVICE, DEV_MODE

    cfg_path = os.path.splitext(os.path.abspath(__file__))[0] + '.py'
    try:
        with open(cfg_path) as _f:
            src = _f.read()
    except OSError:
        return False

    if key == 'device_id':
        new_src = _re_cfg.sub(
            r"^DEVICE\s*=\s*'[^']*'",
            f"DEVICE = '{value}'",
            src, count=1, flags=_re_cfg.MULTILINE)
    elif key == 'dev_mode':
        new_src = _re_cfg.sub(
            r'^DEV_MODE\s*=\s*\S+',
            f'DEV_MODE = {value!r}',
            src, count=1, flags=_re_cfg.MULTILINE)
    else:
        return False

    if new_src == src:
        return True

    try:
        tmp = cfg_path + '.tmp'
        with open(tmp, 'w') as _f:
            _f.write(new_src)
            _f.flush()
            os.fsync(_f.fileno())
        os.replace(tmp, cfg_path)
    except OSError:
        return False

    if key == 'device_id':
        DEVICE = value
    else:
        DEV_MODE = value
    return True
