"""Centralized theme palette + persistence.

Palette constants used across the GUI are populated from LIGHT or DARK
at import time, according to the persisted preference. The palette
covers base surfaces (BG/PANEL_BG/SIDEBAR_BG), semantic colors
(PRIMARY/ACCENT/DANGER/SUCCESS), text hierarchy (TEXT/LABEL/SUBTLE),
AND the interaction variants (hover, pressed, disabled) so widgets
don't have to pick brightness tweaks that only look right in one mode.

Theme switching happens at next app launch — the Settings page writes
the new preference and prompts the user to restart. Hot-swapping would
require rebuilding every already-constructed widget, which is a lot of
code for a kiosk that rarely changes themes.
"""

import json
import os

import config


_THEME_FILE = os.path.join(config.DATA_DIR, 'theme.json')


LIGHT = {
    # Base surfaces
    'BG':           '#ebebeb',
    'PANEL_BG':     '#f4f7fc',
    'SIDEBAR_BG':   '#eef3fa',
    'BORDER':       '#d0dcea',

    # Text hierarchy
    'TEXT':         '#1a1a1a',
    'LABEL':        '#444444',
    'SUBTLE':       '#888888',

    # Brand / semantic
    'PRIMARY':      '#0159C4',
    'PRIMARY_DARK': '#013a8a',
    'PRIMARY_PRESSED': '#012060',
    'ACCENT':       '#227cc5',
    'ACCENT_DARK':  '#1aa5a8',
    'DANGER':       '#c0392b',
    'DANGER_DARK':  '#a93226',
    'SUCCESS':      '#1a7f37',
    'SUCCESS_DARK': '#145c28',
    'WARNING':      '#d97706',

    # Interaction variants
    'DISABLED_BG':   '#cccccc',
    'DISABLED_TEXT': '#888888',
    'INPUT_BG':      '#ffffff',
    'COMBO_HOVER':   '#eeeeee',
    'BTN_HOVER_WASH':'#e8f0fc',
    'CHIP_BG':       '#dddddd',

    # Scan-player specific
    'VIDEO_BG':     '#dce5f0',
    'CONSOLE_BG':   '#f0f2f5',

    # Scrollbar
    'SCROLL_TRACK': '#f0f0f0',
    'SCROLL_THUMB': '#cccccc',
}


DARK = {
    'BG':           '#292929',
    'PANEL_BG':     '#242424',
    'SIDEBAR_BG':   '#1f1f1f',
    'BORDER':       '#3a3a3a',

    'TEXT':         '#e5e5e5',
    'LABEL':        '#c0c0c0',
    'SUBTLE':       '#808080',

    'PRIMARY':      '#3b82f6',
    'PRIMARY_DARK': '#2563eb',
    'PRIMARY_PRESSED': '#1d4ed8',
    'ACCENT':       '#22c1c3',
    'ACCENT_DARK':  '#1aa5a8',
    'DANGER':       '#ef4444',
    'DANGER_DARK':  '#dc2626',
    'SUCCESS':      '#22c55e',
    'SUCCESS_DARK': '#16a34a',
    'WARNING':      '#f59e0b',

    'DISABLED_BG':   '#2a2a2a',
    'DISABLED_TEXT': '#5a5a5a',
    'INPUT_BG':      '#2a2a2a',
    'COMBO_HOVER':   '#353535',
    'BTN_HOVER_WASH':'rgba(59, 130, 246, 0.18)',
    'CHIP_BG':       '#2f2f2f',

    'VIDEO_BG':     '#1e1e1e',
    'CONSOLE_BG':   '#141414',

    'SCROLL_TRACK': '#2a2a2a',
    'SCROLL_THUMB': '#555555',
}


def load_theme_name() -> str:
    try:
        with open(_THEME_FILE) as f:
            name = json.load(f).get('theme')
    except (OSError, ValueError):
        return 'light'
    return 'dark' if name == 'dark' else 'light'


def save_theme_name(name: str) -> bool:
    if name not in ('light', 'dark'):
        return False
    try:
        os.makedirs(os.path.dirname(_THEME_FILE), exist_ok=True)
        tmp = _THEME_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'theme': name}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _THEME_FILE)
        return True
    except OSError:
        return False


THEME_NAME = load_theme_name()
P = DARK if THEME_NAME == 'dark' else LIGHT
