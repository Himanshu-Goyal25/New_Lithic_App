"""Scan tab — wraps the setup form and the live player in a sub-stack."""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QStackedWidget
from PySide6.QtCore import Signal

from gui.scan_setup  import ScanSetupPage
from gui.scan_player import ScanPlayerPage


class ScanPage(QWidget):
    """Scan tab content: SETUP → PLAYER, controlled by an internal stack."""

    scan_state_changed = Signal(bool)

    SETUP_IDX  = 0
    PLAYER_IDX = 1

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.stack = QStackedWidget()

        self.setup_page  = ScanSetupPage()
        self.player_page = ScanPlayerPage()

        self.stack.addWidget(self.setup_page)
        self.stack.addWidget(self.player_page)
        layout.addWidget(self.stack)

        self.setup_page.scan_requested.connect(self._on_scan_requested)
        self.player_page.next_scan_requested.connect(self._on_next_scan)
        self.player_page.scan_state_changed.connect(self.scan_state_changed)

    def _on_scan_requested(self, metadata: dict):
        self.player_page.begin(metadata)
        self.stack.setCurrentIndex(self.PLAYER_IDX)

    def _on_next_scan(self):
        self.setup_page.reset_form()
        self.stack.setCurrentIndex(self.SETUP_IDX)

    def request_close_with_confirm(self):
        if self.stack.currentIndex() == self.PLAYER_IDX:
            self.player_page.request_close_with_confirm(
                on_confirm=lambda: self.window().close())
