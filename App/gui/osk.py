"""On-screen keyboard integration.

RPi OS Bookworm ships `squeekboard` (started at session login by the
LXDE/Wayfire desktop). It exposes a tiny DBus interface:

    bus       : session
    dest      : sm.puri.OSK0
    object    : /sm/puri/OSK0
    method    : SetVisible(in b visible)

We don't try to be the keyboard ourselves (qt6-virtualkeyboard is
client-side and Wayfire refuses to render it). Instead this module
listens for QApplication focus changes and pokes squeekboard to
show / hide as the operator taps in and out of editable widgets.

Key delivery from squeekboard back into Qt widgets is handled by
Wayfire's `wlr-virtual-keyboard-v1` Wayland protocol — nothing for
us to wire on that side.

Diagnostic logging goes to stderr (and therefore the run.sh launch log
at /tmp/lithic-app-launch.log). Look for `[osk]` lines.
"""

import subprocess
import sys

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import (
    QApplication, QLineEdit, QTextEdit, QPlainTextEdit,
    QSpinBox, QDoubleSpinBox, QComboBox,
)


# Widget classes that the operator can type into. QComboBox is only
# editable if isEditable() returns True; handled inline below.
_EDITABLE_TYPES = (
    QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox,
)


class OnScreenKeyboard(QObject):
    """App-wide focus listener that shows / hides squeekboard.

    Hide is debounced (~200 ms) so quickly tabbing between two
    text fields doesn't make the keyboard flicker off and back on.
    """

    _HIDE_DEBOUNCE_MS = 200

    def __init__(self, app: QApplication):
        super().__init__(app)
        self._currently_visible = False
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(self._HIDE_DEBOUNCE_MS)
        self._hide_timer.timeout.connect(lambda: self._set_visible(False))
        app.focusChanged.connect(self._on_focus_changed)
        print('[osk] helper installed; squeekboard will be poked on focus changes',
              file=sys.stderr, flush=True)

    # ── public API ──────────────────────────────────────────────────────
    def toggle(self):
        """Manually flip keyboard visibility (used by the top-bar
        OSK button). Cancels any pending hide-debounce so the user's
        intent isn't undone by a stale timer."""
        self._hide_timer.stop()
        self._set_visible(not self._currently_visible)

    def show(self):
        self._hide_timer.stop()
        self._set_visible(True)

    def hide(self):
        self._hide_timer.stop()
        self._set_visible(False)

    # ── focus tracking ──────────────────────────────────────────────────
    def _on_focus_changed(self, _old, new):
        editable = self._is_editable(new)
        cls = type(new).__name__ if new is not None else 'None'
        print(f'[osk] focusChanged → {cls}  editable={editable}',
              file=sys.stderr, flush=True)
        if editable:
            self._hide_timer.stop()
            self._set_visible(True)
        else:
            # Defer the hide so taps that move focus between two
            # editable widgets don't bounce the keyboard.
            self._hide_timer.start()

    @staticmethod
    def _is_editable(widget) -> bool:
        if widget is None:
            return False
        if isinstance(widget, _EDITABLE_TYPES):
            # QLineEdit and friends respect readOnly / enabled; if the
            # widget is disabled, focus shouldn't have landed on it,
            # but guard anyway.
            try:
                if widget.isReadOnly():
                    return False
            except AttributeError:
                pass
            return widget.isEnabled()
        if isinstance(widget, QComboBox) and widget.isEditable():
            return widget.isEnabled()
        return False

    # ── DBus poke ───────────────────────────────────────────────────────
    def _set_visible(self, visible: bool):
        # Skip if state hasn't changed — avoids stacking DBus calls
        # on every focus tick.
        if visible == self._currently_visible:
            return
        self._currently_visible = visible
        print(f'[osk] SetVisible({visible})',
              file=sys.stderr, flush=True)
        try:
            r = subprocess.run(
                ['gdbus', 'call', '--session',
                 '--dest',        'sm.puri.OSK0',
                 '--object-path', '/sm/puri/OSK0',
                 '--method',      'sm.puri.OSK0.SetVisible',
                 'true' if visible else 'false'],
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode != 0:
                print(f'[osk]   gdbus failed (rc={r.returncode}): '
                      f'{r.stderr.strip()}',
                      file=sys.stderr, flush=True)
        except Exception as e:
            # If gdbus isn't present, or squeekboard isn't running,
            # silently degrade. The operator just loses the OSK —
            # the app keeps working.
            print(f'[osk]   gdbus call raised: {e}',
                  file=sys.stderr, flush=True)
