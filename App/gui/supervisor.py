"""Supervisor PIN — storage, verification, time-limited unlock state.

Shipped default PIN is '0000'. The operator should change it from
Settings → Supervisor → Change PIN on first use. There is no
recovery mechanism by design: a forgotten PIN requires editing
App/data/supervisor.json directly on the device.
"""

import hashlib
import hmac
import json
import os
import secrets
import time

import config

_STORE = os.path.join(config.DATA_DIR, 'supervisor.json')
_DEFAULT_PIN = '0000'

# How long a successful unlock keeps the app in "admin mode" before
# re-prompting. Tracked in-memory; resets on app restart.
_UNLOCK_SECONDS = 10 * 60


def _load() -> dict:
    try:
        with open(_STORE) as f:
            data = json.load(f)
        if 'salt' in data and 'hash' in data:
            return data
    except (OSError, ValueError):
        pass
    return _make_entry(_DEFAULT_PIN)


def _save(entry: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(_STORE), exist_ok=True)
        tmp = _STORE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(entry, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _STORE)
        return True
    except OSError:
        return False


def _make_entry(pin: str) -> dict:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pin.encode(), bytes.fromhex(salt), 100_000)
    return {'salt': salt, 'hash': h.hex()}


# ── Public API ──────────────────────────────────────────────────────────────

def is_default_pin() -> bool:
    return verify(_DEFAULT_PIN, _silent=True)


def verify(pin: str, _silent: bool = False) -> bool:
    entry = _load()
    h = hashlib.pbkdf2_hmac(
        'sha256', pin.encode(), bytes.fromhex(entry['salt']), 100_000)
    ok = hmac.compare_digest(h.hex(), entry['hash'])
    if ok and not _silent:
        _unlock_until[0] = time.monotonic() + _UNLOCK_SECONDS
    return ok


def set_pin(old_pin: str, new_pin: str) -> tuple:
    if not verify(old_pin, _silent=True):
        return (False, 'Current PIN is incorrect.')
    if not new_pin or len(new_pin) < 4:
        return (False, 'New PIN must be at least 4 digits.')
    if not new_pin.isdigit():
        return (False, 'PIN must contain digits only.')
    if _save(_make_entry(new_pin)):
        return (True, '')
    return (False, 'Could not save PIN (disk error).')


# ── Unlock state ────────────────────────────────────────────────────────────

_unlock_until = [0.0]


def is_unlocked() -> bool:
    return time.monotonic() < _unlock_until[0]


def lock():
    _unlock_until[0] = 0.0


def ensure_unlocked(parent) -> bool:
    if is_unlocked():
        return True
    from gui.supervisor_dialog import _PinDialog
    dlg = _PinDialog(parent)
    return dlg.exec() == dlg.DialogCode.Accepted
