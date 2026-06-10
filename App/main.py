"""Entry point — single-instance guard, splash, MainWindow."""
import sys
import os

# Allow running as: python3 App/main.py from project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QGraphicsOpacityEffect,
)
from PySide6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QEasingCurve, QCoreApplication, QObject,
)
from PySide6.QtGui import QIcon, QPixmap, QPalette, QColor
from PySide6.QtNetwork import QLocalServer, QLocalSocket

import config

APP_ID = 'LithicProV2DataCollector'
_APP_DIR  = os.path.dirname(os.path.abspath(__file__))
_ICON_PNG = os.path.join(_APP_DIR, 'Images', '2739025.png')


def _apply_palette(app: QApplication) -> None:
    """Pin every QPalette role to a theme token so Qt's default palette
    doesn't paint a contrasting `Window` rectangle behind QLabels."""
    from gui import theme
    p = app.palette()
    p.setColor(QPalette.ColorRole.Window,          QColor(theme.P['BG']))
    p.setColor(QPalette.ColorRole.WindowText,      QColor(theme.P['TEXT']))
    p.setColor(QPalette.ColorRole.Base,            QColor(theme.P['INPUT_BG']))
    p.setColor(QPalette.ColorRole.AlternateBase,   QColor(theme.P['PANEL_BG']))
    p.setColor(QPalette.ColorRole.Text,            QColor(theme.P['TEXT']))
    p.setColor(QPalette.ColorRole.Button,          QColor(theme.P['PANEL_BG']))
    p.setColor(QPalette.ColorRole.ButtonText,      QColor(theme.P['TEXT']))
    p.setColor(QPalette.ColorRole.Highlight,       QColor(theme.P['PRIMARY']))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor('#ffffff'))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(theme.P['SUBTLE']))
    p.setColor(QPalette.ColorRole.ToolTipBase,     QColor(theme.P['PANEL_BG']))
    p.setColor(QPalette.ColorRole.ToolTipText,     QColor(theme.P['TEXT']))
    app.setPalette(p)


class _SplashScreen(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet('background-color: #0159C4;')

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(10)

        if os.path.exists(_ICON_PNG):
            icon_lbl = QLabel()
            icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pm = QPixmap(_ICON_PNG).scaled(
                180, 180,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            icon_lbl.setPixmap(pm)
            icon_lbl.setStyleSheet('background: transparent;')
            layout.addWidget(icon_lbl)
            layout.addSpacing(10)

        name_lbl = QLabel('LITHIC PRO V2')
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_lbl.setStyleSheet(
            'color: white;'
            'font-size: 52pt;'
            'font-weight: bold;'
            "font-family: 'Ubuntu', 'Segoe UI', sans-serif;"
            'background: transparent;'
        )
        layout.addWidget(name_lbl)

        sub_lbl = QLabel(f'INKERS DATA COLLECTOR  ·  {config.DEVICE}')
        sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub_lbl.setStyleSheet(
            'color: rgba(255, 255, 255, 180);'
            'font-size: 12pt;'
            "font-family: 'Ubuntu', 'Segoe UI', sans-serif;"
            'background: transparent;'
        )
        layout.addWidget(sub_lbl)

        # Accent line under the text
        accent = QLabel()
        accent.setFixedHeight(3)
        accent.setStyleSheet('background-color: rgba(255,255,255,100); border-radius: 2px;')
        accent_wrap = QVBoxLayout()
        accent_wrap.setContentsMargins(200, 12, 200, 0)
        accent_wrap.addWidget(accent)
        layout.addLayout(accent_wrap)

        # Fade-in
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._fade_in = QPropertyAnimation(self._effect, b'opacity', self)
        self._fade_in.setDuration(450)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_in.start()

        QTimer.singleShot(1800, self._begin_close)

    def _begin_close(self):
        self._fade_out = QPropertyAnimation(self._effect, b'opacity', self)
        self._fade_out.setDuration(380)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade_out.finished.connect(self._launch)
        self._fade_out.start()

    def _launch(self):
        from gui.main_window import MainWindow
        self._shell = MainWindow()
        scr = QApplication.primaryScreen().geometry()
        if config.DISPLAY_RESOLUTION:
            self._shell.setFixedSize(*config.DISPLAY_RESOLUTION)
            self._shell.move(scr.x(), scr.y())
            self._shell.show()
        else:
            # Kiosk full-screen look WITHOUT the Wayland fullscreen
            # toplevel state. We need to stay as a regular xdg-toplevel
            # so squeekboard's wlr-layer-shell surface still renders
            # above us (Wayfire draws a fullscreen toplevel above
            # layer-shell, which hides the OSK).
            # run.sh kills wf-panel-pi before launch and respawns it
            # after exit, so the operator gets a real fullscreen
            # appearance during the scan but a working desktop
            # afterwards.
            self._shell.setFixedSize(scr.width(), scr.height())
            self._shell.move(scr.x(), scr.y())
            self._shell.show()
        self.close()


class _SingleInstanceGuard(QObject):
    """Enforce one-running-instance via a named QLocalSocket."""

    KEY = f'lithic-pro-v2-collector-v1-{os.getuid()}'

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._server = None

    def claim(self) -> bool:
        probe = QLocalSocket()
        probe.connectToServer(self.KEY)
        if probe.waitForConnected(400):
            probe.write(b'raise\n')
            probe.flush()
            probe.waitForBytesWritten(400)
            probe.disconnectFromServer()
            return False

        QLocalServer.removeServer(self.KEY)
        self._server = QLocalServer(self)
        if not self._server.listen(self.KEY):
            print(f'[singleton] failed to listen on {self.KEY}: '
                  f'{self._server.errorString()}', file=sys.stderr)
            return True
        self._server.newConnection.connect(self._on_new_connection)
        return True

    def _on_new_connection(self):
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        conn.readyRead.connect(lambda c=conn: self._handle(c))

    def _handle(self, conn):
        msg = bytes(conn.readAll()).decode('utf-8', 'replace').strip()
        conn.disconnectFromServer()
        if 'raise' in msg:
            self._raise_primary_window()

    def _raise_primary_window(self):
        for w in self._app.topLevelWidgets():
            if w.isVisible():
                w.setWindowState(
                    (w.windowState() & ~Qt.WindowState.WindowMinimized)
                    | Qt.WindowState.WindowActive)
                w.show()
                w.raise_()
                w.activateWindow()
                break


def main():
    QCoreApplication.setOrganizationName('Inkers')
    QCoreApplication.setApplicationName(APP_ID)
    QCoreApplication.setApplicationVersion(config.VERSION)

    # Touch-screen support. Must be set BEFORE the QApplication is
    # constructed (Qt loads these attributes during platform-plugin
    # init). Default is True in Qt 6, but some Wayland builds on RPi
    # OS only honour it when set explicitly. Without this, frameless
    # child-widget overlays (e.g. the QA-failure alert) can receive
    # touch events that never get synthesized into clicks — buttons
    # appear unresponsive to tap.
    QCoreApplication.setAttribute(
        Qt.ApplicationAttribute.AA_SynthesizeMouseForUnhandledTouchEvents,
        True)
    QCoreApplication.setAttribute(
        Qt.ApplicationAttribute.AA_SynthesizeTouchForUnhandledMouseEvents,
        False)

    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    _apply_palette(app)

    app.setDesktopFileName(APP_ID)
    if os.path.exists(_ICON_PNG):
        app.setWindowIcon(QIcon(_ICON_PNG))

    # Single-instance guard
    guard = _SingleInstanceGuard(app)
    if not guard.claim():
        print('[singleton] another instance is already running; '
              'bringing it to front and exiting.', file=sys.stderr)
        return 0
    app._singleton_guard = guard

    # Shut down ROS on exit
    from core.ros_controller import RosController
    app.aboutToQuit.connect(RosController.shutdown_roscore)

    # On-screen keyboard for the touch-screen kiosk. Listens for focus
    # changes and pokes squeekboard (RPi OS's default OSK, started by
    # the desktop session) to show/hide via its sm.puri.OSK0 DBus
    # interface. The OSK widget itself lives in the system, not the
    # app — Wayfire delivers its keystrokes back to whichever widget
    # currently holds focus.
    from gui.osk import OnScreenKeyboard
    app._osk = OnScreenKeyboard(app)

    # Render the theme-aware global stylesheet
    gui_dir  = os.path.join(_APP_DIR, 'gui')
    qss_path = os.path.join(gui_dir, 'style.qss')
    if os.path.exists(qss_path):
        from gui.theme import P as _palette
        try:
            with open(qss_path) as f:
                qss_tpl = f.read()
            app.setStyleSheet(qss_tpl.format(**_palette))
        except (OSError, KeyError, ValueError) as e:
            print(f'[theme] global stylesheet could not be applied: {e}',
                  file=sys.stderr)

    splash = _SplashScreen()
    scr = QApplication.primaryScreen().geometry()
    splash.setFixedSize(scr.width(), scr.height())
    splash.move(scr.x(), scr.y())
    splash.showFullScreen()

    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
