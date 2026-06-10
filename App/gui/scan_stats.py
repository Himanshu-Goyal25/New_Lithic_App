"""Shared helpers — scan discovery, size/duration computation, external-mount detection."""

import os
import re
import json
import datetime

# Observed scan rate — used as a fallback when there is no historical
# data to derive a rate from. Measured ~60 GB/h on this device from
# real bag-folder size vs time-span (Hesai LiDAR pointcloud dominates,
# IMU contribution is ~36 MB/h).
DEFAULT_GB_PER_HOUR = 60.0

# Sanity bounds on a per-scan rate when averaging history. A scan that
# computes outside this range is almost certainly bogus (recovery wrote
# a wrong started_at/stopped_at, partial bag set, etc.) — including
# it would poison the average for everyone.
_MIN_PLAUSIBLE_GB_PER_HOUR = 10.0
_MAX_PLAUSIBLE_GB_PER_HOUR = 200.0

# Canonical scan-folder name produced by scan_player._make_scan_folder:
#   "{site}_{floor_type}{floor_num}_{scan_part}_{YYYYMMDD}_{HHMMSS}"
# Ground Floor has no number: "{site}_Ground_Floor_{scan_part}_..."
_SCAN_FOLDER_RE = re.compile(
    r'^(?P<site>.+?)'
    r'_(?P<floor_type>Ground_Floor|Floor|Basement)(?P<floor_num>\d*)'
    r'_(?P<scan_part>.+)'
    r'_(?P<date>\d{8})_(?P<time>\d{6})$'
)


def list_scans(root: str) -> list:
    """Walk `root` for scan_info.json files. Returns list of dicts (newest first)."""
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, files in os.walk(root):
        if 'scan_info.json' not in files:
            continue
        info_path = os.path.join(dirpath, 'scan_info.json')
        try:
            with open(info_path) as f:
                info = json.load(f)
        except Exception:
            continue
        info['scan_folder']      = dirpath
        info['size_bytes']       = _folder_size(dirpath)
        info['duration_seconds'] = _scan_duration(info, dirpath)
        info.setdefault(
            'mtime',
            datetime.datetime.fromtimestamp(os.path.getmtime(info_path)).isoformat(),
        )
        out.append(info)
    out.sort(key=lambda i: i.get('stopped_at') or i.get('mtime') or '', reverse=True)
    return out


def _folder_size(path: str) -> int:
    total = 0
    for dp, _dirs, fnames in os.walk(path):
        for f in fnames:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def _scan_duration(info: dict, folder: str):
    started = info.get('started_at')
    stopped = info.get('stopped_at')
    try:
        if started and stopped:
            s = datetime.datetime.fromisoformat(started)
            e = datetime.datetime.fromisoformat(stopped)
            return max((e - s).total_seconds(), 0)
    except Exception:
        pass

    # Fallback — earliest bag ctime → stopped_at
    if not stopped:
        return None
    try:
        e = datetime.datetime.fromisoformat(stopped)
        bags = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith('.bag')]
        if not bags:
            return None
        earliest = min(os.path.getctime(b) for b in bags)
        s = datetime.datetime.fromtimestamp(earliest)
        return max((e - s).total_seconds(), 0)
    except Exception:
        return None


def format_size(n) -> str:
    if n is None:
        return '—'
    if n >= 1024 ** 3:
        return f'{n / 1024 ** 3:.1f} GB'
    if n >= 1024 ** 2:
        return f'{n / 1024 ** 2:.1f} MB'
    if n >= 1024:
        return f'{n / 1024:.1f} KB'
    return f'{n} B'


def format_duration(seconds) -> str:
    if seconds is None:
        return '—'
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f'{h}h {m:02d}m'
    if m:
        return f'{m}m {sec:02d}s'
    return f'{sec}s'


def estimate_gb_per_hour(scans: list) -> float:
    valid = []
    for s in scans:
        # Recovered scans have unreliable started_at/stopped_at —
        # `_recover_orphan` writes the recovery time as stopped_at,
        # which is often hours after the actual recording ended.
        # Including them produces wildly wrong rates.
        if s.get('recovered'):
            continue
        dur = s.get('duration_seconds')
        size = s.get('size_bytes')
        if not dur or not size or dur <= 10:
            continue
        gb_per_h = (size / 1024 ** 3) / (dur / 3600)
        if not (_MIN_PLAUSIBLE_GB_PER_HOUR <= gb_per_h
                <= _MAX_PLAUSIBLE_GB_PER_HOUR):
            # Outlier (e.g. half-recovered scan with a partial bag
            # count, or a folder that's had extra junk written to it).
            continue
        valid.append(s)

    if not valid:
        return DEFAULT_GB_PER_HOUR
    total_gb    = sum(s['size_bytes'] for s in valid) / 1024 ** 3
    total_hours = sum(s['duration_seconds'] for s in valid) / 3600
    if total_hours < 0.01:
        return DEFAULT_GB_PER_HOUR
    return total_gb / total_hours


def estimate_hours_remaining(free_bytes: int, gb_per_hour: float):
    if gb_per_hour <= 0:
        return None
    return (free_bytes / 1024 ** 3) / gb_per_hour


def parse_scan_folder_name(name: str) -> dict:
    """Reverse the folder-name convention from scan_player._make_scan_folder."""
    m = _SCAN_FOLDER_RE.match(name)
    if not m:
        return {}
    floor_type = m.group('floor_type').replace('_', ' ')
    floor_num_raw = m.group('floor_num')
    result = {
        'site':       m.group('site').replace('_', ' '),
        'floor_type': floor_type,
        'floor_num':  int(floor_num_raw) if floor_num_raw else 0,
        'scan_part':  m.group('scan_part').replace('_', ' '),
    }
    try:
        dt = datetime.datetime.strptime(
            f"{m.group('date')}_{m.group('time')}", '%Y%m%d_%H%M%S')
        result['started_guess'] = dt.isoformat()
    except ValueError:
        pass
    return result


def find_orphan_scans(root: str) -> list:
    """Folders under `root` that contain .bag files but no valid scan_info.json."""
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, files in os.walk(root):
        bag_files = [f for f in files if f.endswith('.bag')]
        if not bag_files:
            continue
        info_path = os.path.join(dirpath, 'scan_info.json')
        if os.path.exists(info_path):
            try:
                with open(info_path) as f:
                    json.load(f)
                continue
            except (OSError, ValueError):
                pass

        size = 0
        for b in bag_files:
            try:
                size += os.path.getsize(os.path.join(dirpath, b))
            except OSError:
                pass
        try:
            mtime = datetime.datetime.fromtimestamp(
                os.path.getmtime(dirpath)).isoformat()
        except OSError:
            mtime = ''
        out.append({
            'scan_folder': dirpath,
            'bag_count':   len(bag_files),
            'size_bytes':  size,
            'mtime':       mtime,
            'parsed':      parse_scan_folder_name(os.path.basename(dirpath)),
        })
    out.sort(key=lambda i: i.get('mtime', ''), reverse=True)
    return out


def ensure_external_drives_mounted() -> int:
    """Best-effort: try to mount any USB block-device partitions that
    lsblk reports as not currently mounted. Returns the number of
    mounts that were attempted (not necessarily successful). Called
    by the Data Transfer page before each refresh so a plugged-in
    drive that pcmanfm's automount missed becomes visible to the app.

    Removes stale, empty mount-point dirs under /media/<user>/ that
    udisks left behind from a previous run — those block the new
    mount because udisks refuses to mount over a non-empty path it
    doesn't own.

    Never raises — all subprocess calls are wrapped.
    """
    import re
    import subprocess
    attempted = 0

    # 1. Find candidate USB partitions. Use lsblk -P (KEY="value")
    # output — robust against empty fields (TRAN/MOUNTPOINTS are
    # blank on partitions / disks respectively, and -r raw mode
    # would silently shift the column index).
    try:
        r = subprocess.run(
            ['lsblk', '-Pno', 'NAME,TRAN,TYPE,FSTYPE,MOUNTPOINTS', '-p'],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return 0
    if r.returncode != 0:
        return 0

    pair_re = re.compile(r'(\w+)="([^"]*)"')
    parents = {}   # disk-device -> TRAN
    rows    = []   # list of dicts
    for line in r.stdout.splitlines():
        row = dict(pair_re.findall(line))
        if not row:
            continue
        rows.append(row)
        if row.get('TYPE') == 'disk':
            parents[row.get('NAME', '')] = row.get('TRAN', '')

    targets = []   # partition paths to mount
    for row in rows:
        if row.get('TYPE') != 'part':
            continue
        if row.get('MOUNTPOINTS'):
            continue
        name = row.get('NAME', '')
        # Strip trailing partition number to find the parent disk.
        parent_path = name.rstrip('0123456789')
        if parents.get(parent_path) != 'usb':
            continue
        if not row.get('FSTYPE'):
            continue
        targets.append(name)

    if not targets:
        return 0

    # 2. Clean stale empty mount dirs under /media/<user>/ that might
    # block automount.
    media_root = '/media'
    if os.path.isdir(media_root):
        for user_dir in os.listdir(media_root):
            udir = os.path.join(media_root, user_dir)
            if not os.path.isdir(udir):
                continue
            try:
                children = os.listdir(udir)
            except (PermissionError, OSError):
                continue
            for name in children:
                p = os.path.join(udir, name)
                if not os.path.isdir(p):
                    continue
                if os.path.ismount(p):
                    continue
                try:
                    if not os.listdir(p):    # truly empty
                        # rmdir requires us to own the parent OR the
                        # dir. Stale udisks dirs are root-owned, so
                        # best-effort sudo with -n (no prompt).
                        subprocess.run(
                            ['sudo', '-n', 'rmdir', p],
                            timeout=3, check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
                except OSError:
                    pass

    # 3. Ask udisksctl to mount each candidate. Capture stderr so a
    # polkit / udisks failure shows up in /tmp/lithic-app-launch.log
    # under a clear prefix — silent failure here is the most common
    # cause of "drive doesn't appear in Data Transfer page".
    import sys
    for dev in targets:
        try:
            r = subprocess.run(
                ['udisksctl', 'mount', '-b', dev],
                timeout=10, check=False,
                capture_output=True, text=True)
            attempted += 1
            if r.returncode != 0:
                print(
                    f'[mount] udisksctl mount {dev} failed (rc={r.returncode}): '
                    f'{(r.stderr or r.stdout).strip()}',
                    file=sys.stderr, flush=True)
            else:
                print(f'[mount] mounted {dev}: {r.stdout.strip()}',
                      file=sys.stderr, flush=True)
        except Exception as e:
            print(f'[mount] subprocess error for {dev}: {e}',
                  file=sys.stderr, flush=True)
    return attempted


def find_external_mount():
    """Return the first detected USB-mount path, or None."""
    for mount in find_all_external_mounts():
        return mount
    return None


def find_all_external_mounts() -> list:
    """Return every detected external/USB mount point (deduplicated, sorted)."""
    import config
    dump = os.path.realpath(config.DUMP_PATH)
    found = set()
    roots = ['/media', '/run/media', '/mnt']
    for base in roots:
        if not os.path.isdir(base):
            continue
        try:
            entries = os.listdir(base)
        except (PermissionError, OSError):
            continue
        for entry in entries:
            path = os.path.join(base, entry)
            if not os.path.isdir(path):
                continue
            if _is_real_mount(path):
                if os.path.realpath(path) != dump:
                    found.add(path)
                continue
            try:
                sub_entries = os.listdir(path)
            except (PermissionError, NotADirectoryError, OSError):
                continue
            for s in sub_entries:
                sp = os.path.join(path, s)
                if _is_real_mount(sp) and os.path.realpath(sp) != dump:
                    found.add(sp)
    return sorted(found)


def _is_real_mount(path: str) -> bool:
    if not os.path.ismount(path):
        return False
    try:
        st_path = os.stat(path).st_dev
        st_root = os.stat('/').st_dev
        return st_path != st_root
    except OSError:
        return False
