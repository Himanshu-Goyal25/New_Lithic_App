"""Append-only operator action log.

One JSON object per line so a corrupt write never invalidates earlier
or later entries.
"""
import json
import os
from datetime import datetime
from pathlib import Path

_LOG_FILE = Path(__file__).resolve().parent.parent / 'data' / 'action_log.jsonl'


def log_path() -> str:
    return str(_LOG_FILE)


def log_action(action: str, **details) -> None:
    """Append a single action record. Failures are swallowed —
    the audit log is best-effort, never block a scan if disk is full."""
    entry = {
        'ts':      datetime.now().isoformat(timespec='seconds'),
        'action':  action,
        'details': dict(details),
    }
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_FILE, 'a') as f:
            f.write(json.dumps(entry, default=str) + '\n')
    except Exception:
        pass


# Back-compat alias for old callers
def log(action: str, details: dict | None = None) -> None:
    log_action(action, **(details or {}))


def read_actions(n: int = 200) -> list:
    """Read the last N entries (newest last on disk, newest first returned).
    Skips malformed lines."""
    if not _LOG_FILE.exists():
        return []
    out = []
    try:
        with open(_LOG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return list(reversed(out[-n:]))


def read_recent(n: int = 100) -> list:
    """Back-compat: returns entries newest-last (chronological)."""
    if not _LOG_FILE.exists():
        return []
    out = []
    try:
        with open(_LOG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out[-n:]
