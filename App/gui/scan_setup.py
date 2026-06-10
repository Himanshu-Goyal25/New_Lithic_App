"""Scan-setup form — embeddable page."""

import csv
import os
import sys

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QComboBox, QLineEdit, QPushButton, QSpinBox, QCompleter, QSizePolicy,
    QFrame, QGraphicsDropShadowEffect,
)
from PySide6.QtCore import Qt, QStringListModel, Signal, QSize
from PySide6.QtGui import (
    QPainter, QColor, QBrush, QPen, QPixmap, QIcon, QAction,
)

import config
from gui.main_window import (
    BG, PRIMARY, PRIMARY_DARK, PRIMARY_PRESSED, TEXT, LABEL, SUBTLE, DANGER,
    BORDER, PANEL_BG, BTN_HOVER_WASH, _GradientLabel,
)
from gui.device_status import DeviceStatusPanel


def _step_glyph(symbol: str, size: int = 22, color: str = 'white') -> QIcon:
    """Draw + or - as plain lines, geometrically centered in `size`x`size`.

    Beats text-based +/- because the symbol's position doesn't depend on
    the font's baseline / x-height — the lines are placed by pixel math.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidth(max(2, size // 10))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    mid   = size // 2
    inset = size // 4
    p.drawLine(inset, mid, size - inset, mid)         # horizontal bar (always)
    if symbol == '+':
        p.drawLine(mid, inset, mid, size - inset)     # vertical bar (plus only)
    p.end()
    return QIcon(pm)


def _field_icon(color_hex: str, size: int = 14) -> QAction:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(QColor(color_hex)))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    action = QAction()
    action.setIcon(QIcon(pm))
    return action


class ScanSetupPage(QWidget):
    """Form page — emits scan_requested(metadata) on submit."""

    scan_requested = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._load_data()

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        title = _GradientLabel('SCAN SETUP', PRIMARY, PRIMARY_DARK)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setContentsMargins(0, 14, 0, 0)
        title.setStyleSheet('font-size: 28pt; font-weight: bold;')
        outer.addWidget(title)

        subtitle = QLabel(f'DEVICE: {config.DEVICE}')
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet(
            f'color: {PRIMARY_DARK}; font-size: 12pt; font-weight: bold;')
        outer.addWidget(subtitle)

        h_row = QHBoxLayout()
        h_row.setContentsMargins(20, 14, 20, 8)
        h_row.setSpacing(0)

        form_col = QVBoxLayout()
        form_col.setSpacing(14)
        form_col.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        # 24 px vertical gap + each label pinned to 40 px (matches the
        # field height) so every row is a clean band with no overlap.
        form.setVerticalSpacing(24)
        form.setHorizontalSpacing(14)
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)

        # Absolute path to the bundled dropdown arrow SVG — QSS won't
        # resolve relative paths because the CWD at launch isn't fixed.
        arrow_down = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '..', 'Images', 'Arrow_down.svg')
        arrow_down = os.path.abspath(arrow_down).replace('\\', '/')

        combo_style = (
            f'QComboBox {{ color: {TEXT}; background-color: {BG};'
            f'  border: 1px solid {BORDER}; border-radius: 4px;'
            f'  font-size: 13pt; padding-left: 6px; padding-right: 32px; }} '
            f'QComboBox:focus {{ border: 2px solid {PRIMARY}; }} '
            f'QComboBox::drop-down {{ border: none; width: 32px;'
            f'  subcontrol-origin: padding; subcontrol-position: center right; }} '
            f'QComboBox::down-arrow {{ image: url({arrow_down});'
            f'  width: 14px; height: 10px; }} '
            f'QComboBox QAbstractItemView {{ color: {TEXT}; background-color: {BG};'
            f'  selection-background-color: {PRIMARY}; selection-color: white; }}'
        )
        spin_style = (
            f'QSpinBox {{ color: {TEXT}; background-color: {BG};'
            f'  border: 1px solid {BORDER}; border-radius: 4px;'
            f'  font-size: 13pt; padding-left: 6px; }} '
            f'QSpinBox:focus {{ border: 2px solid {PRIMARY}; }}'
        )
        line_style = (
            f'QLineEdit {{ color: {TEXT}; background-color: {BG};'
            f'  border: 1px solid {BORDER}; border-radius: 4px;'
            f'  font-size: 13pt; padding-left: 6px; }} '
            f'QLineEdit:focus {{ border: 2px solid {PRIMARY}; }}'
        )

        # ── Site ────────────────────────────────────────────────────────
        self._all_sites = []
        self._site_model = QStringListModel()
        self._site_completer = QCompleter(self._site_model, self)
        self._site_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._site_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._site_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._style_completer_popup(self._site_completer)

        self.site_combo = QComboBox()
        self.site_combo.setEditable(True)
        self.site_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.site_combo.setCompleter(self._site_completer)
        self.site_combo.lineEdit().setPlaceholderText('Select or type a site name')
        self.site_combo.lineEdit().addAction(
            _field_icon(PRIMARY), QLineEdit.ActionPosition.LeadingPosition)
        self.site_combo.setMinimumHeight(40)
        self.site_combo.setStyleSheet(combo_style)
        form.addRow(self._lbl('Site:'), self.site_combo)

        # ── Floor type ──────────────────────────────────────────────────
        self.floor_type_combo = QComboBox()
        self.floor_type_combo.setMinimumHeight(40)
        self.floor_type_combo.setStyleSheet(combo_style)
        self.floor_type_combo.addItems(['Floor', 'Basement', 'Ground Floor'])
        form.addRow(self._lbl('Floor Type:'), self.floor_type_combo)

        # ── Floor number with custom +/− buttons ────────────────────────
        # Use fixed height (not minimum) so the spinbox and the step
        # buttons share the same exact height — otherwise the HBox
        # stretches the spin to the row height and the 40-px buttons
        # look slightly shorter and off-centre.
        self.floor_number = QSpinBox()
        self.floor_number.setFixedHeight(40)
        self.floor_number.setRange(0, 1000)
        # Disable the built-in up/down arrows — setButtonSymbols is the
        # only reliable way; QSS `width:0` still leaves a visible chrome
        # rectangle on some Qt themes.
        self.floor_number.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.floor_number.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.floor_number.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.floor_number.setStyleSheet(spin_style)

        # The +/- glyphs are painted as lines via `_step_glyph` (see top
        # of file). Text-based +/- depend on font baselines that drift
        # 2-3 px below center in most fonts — painting the lines avoids
        # the centering issue entirely.
        self._floor_minus_btn = self._step_btn('-')
        self._floor_minus_btn.clicked.connect(self._floor_step_down)
        self._floor_plus_btn = self._step_btn('+', radius_right=True)
        self._floor_plus_btn.clicked.connect(self._floor_step_up)

        floor_row = QHBoxLayout()
        floor_row.setSpacing(0)
        # Pin every child to vcenter so any pixel-level rounding inside
        # the layout doesn't drop the spin or buttons by 1 px.
        floor_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        floor_row.addWidget(self.floor_number, alignment=Qt.AlignmentFlag.AlignVCenter)
        floor_row.addWidget(self._floor_minus_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        floor_row.addWidget(self._floor_plus_btn,  alignment=Qt.AlignmentFlag.AlignVCenter)
        form.addRow(self._lbl('Floor Number:'), floor_row)

        # ── Incharge ────────────────────────────────────────────────────
        self._all_incharge = []
        self._incharge_model = QStringListModel()
        self._incharge_completer = QCompleter(self._incharge_model, self)
        self._incharge_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._incharge_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._incharge_completer.setCompletionMode(
            QCompleter.CompletionMode.PopupCompletion)
        self._style_completer_popup(self._incharge_completer)

        self.incharge_combo = QComboBox()
        self.incharge_combo.setEditable(True)
        self.incharge_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.incharge_combo.setCompleter(self._incharge_completer)
        self.incharge_combo.lineEdit().setPlaceholderText('Select or type a name')
        self.incharge_combo.lineEdit().addAction(
            _field_icon(LABEL), QLineEdit.ActionPosition.LeadingPosition)
        self.incharge_combo.setMinimumHeight(40)
        self.incharge_combo.setStyleSheet(combo_style)
        form.addRow(self._lbl('Scan Incharge:'), self.incharge_combo)

        self._spin_style  = spin_style
        self._line_style  = line_style
        self._combo_style = combo_style

        self.scan_part = QLineEdit()
        self.scan_part.setMinimumHeight(40)
        self.scan_part.setStyleSheet(line_style)
        self.scan_part.setPlaceholderText('e.g. North Wing, Stairwell A')
        self.scan_part.addAction(
            _field_icon(SUBTLE), QLineEdit.ActionPosition.LeadingPosition)
        self.scan_part.textChanged.connect(lambda: self._clear_error(self.scan_part))
        self.site_combo.lineEdit().textChanged.connect(
            lambda: self._clear_error(self.site_combo))
        self.incharge_combo.lineEdit().textChanged.connect(
            lambda: self._clear_error(self.incharge_combo))
        form.addRow(self._lbl('Scan Part:'), self.scan_part)

        self.floor_number.valueChanged.connect(
            lambda: self._clear_error(self.floor_number))
        self.floor_type_combo.currentIndexChanged.connect(
            self._on_floor_type_changed)

        form_col.addLayout(form)
        form_col.addSpacing(8)

        self.submit_btn = QPushButton('Proceed to Scan  →')
        self.submit_btn.setMinimumHeight(50)
        self.submit_btn.setStyleSheet(
            f'QPushButton {{ background-color: {PRIMARY}; color: white;'
            f'  border-radius: 10px; font-size: 14pt; font-weight: bold; }} '
            f'QPushButton:hover  {{ background-color: {PRIMARY_DARK}; }} '
            f'QPushButton:pressed{{ background-color: {PRIMARY_PRESSED}; }}'
        )
        self.submit_btn.clicked.connect(self._on_submit)
        form_col.addWidget(self.submit_btn)

        self.status_label = QLabel('')
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(f'font-size: 11pt; color: {SUBTLE};')
        form_col.addWidget(self.status_label)

        card = QFrame()
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        card.setStyleSheet(
            f'QFrame {{ background-color: {PANEL_BG}; border: 1px solid {BORDER};'
            f'  border-radius: 16px; }} '
            f'QLineEdit, QComboBox, QSpinBox {{ background-color: {BG}; }}'
        )
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(1, 89, 196, 35))
        card.setGraphicsEffect(shadow)

        card_inner = QVBoxLayout(card)
        card_inner.setContentsMargins(28, 24, 28, 24)
        card_inner.addLayout(form_col)
        card_inner.addStretch()
        h_row.addWidget(card, stretch=1)

        outer.addLayout(h_row, stretch=1)

        # Devices banner
        dev_wrap = QVBoxLayout()
        dev_wrap.setContentsMargins(20, 0, 20, 14)
        dev_wrap.addWidget(DeviceStatusPanel(horizontal=True))
        outer.addLayout(dev_wrap)

    def _step_btn(self, symbol: str, radius_right: bool = False) -> QPushButton:
        """`symbol` is '+' or '-' — rendered as a painted icon so it's
        geometrically centred (no font baseline drift)."""
        btn = QPushButton()
        btn.setIcon(_step_glyph(symbol, size=22))
        btn.setIconSize(QSize(22, 22))
        btn.setText('')                       # icon only — no text contributes
        btn.setFixedSize(44, 40)
        radius_css = ''
        if radius_right:
            radius_css = 'border-top-right-radius: 6px; border-bottom-right-radius: 6px;'
        btn.setStyleSheet(
            f'QPushButton {{ background-color: {PRIMARY}; color: white;'
            f'  border: none; border-right: 1px solid rgba(255,255,255,0.35);'
            f'  padding: 0; {radius_css} }} '
            f'QPushButton:hover  {{ background-color: {PRIMARY_DARK}; }} '
            f'QPushButton:pressed{{ background-color: {PRIMARY_PRESSED}; }} '
            f'QPushButton:disabled {{ background-color: {SUBTLE}; }}'
        )
        return btn

    def _style_completer_popup(self, completer):
        completer.popup().setStyleSheet(
            f'QListView {{ color: {TEXT}; background-color: {BG};'
            f'  font-size: 13pt; border: 1px solid {PRIMARY};'
            f'  border-radius: 4px; outline: none; }} '
            f'QListView::item {{ min-height: 36px; padding-left: 8px; }} '
            f'QListView::item:hover {{ background-color: {BTN_HOVER_WASH};'
            f'  color: {PRIMARY}; }} '
            f'QListView::item:selected {{ background-color: {PRIMARY}; color: white; }}'
        )

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _lbl(text: str) -> QLabel:
        lbl = QLabel(text)
        # Fix the label height to the 40-px field height and centre the
        # text inside it. Without these, QFormLayout sized each label to
        # the natural 22-px text height and aligned it to the top of the
        # cell, so consecutive rows looked like they overlapped.
        lbl.setFixedHeight(40)
        lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lbl.setContentsMargins(0, 0, 0, 0)
        lbl.setStyleSheet(
            f'color: {LABEL}; font-size: 13pt; font-weight: bold;'
            f'background: transparent; border: 0; margin: 0; padding: 0;')
        return lbl

    def _floor_step_up(self):
        self.floor_number.stepUp()
        self.floor_number.lineEdit().deselect()
        self.floor_number.clearFocus()

    def _floor_step_down(self):
        self.floor_number.stepDown()
        self.floor_number.lineEdit().deselect()
        self.floor_number.clearFocus()

    def _on_floor_type_changed(self):
        is_ground = self.floor_type_combo.currentText() == 'Ground Floor'
        self.floor_number.setEnabled(not is_ground)
        self._floor_minus_btn.setEnabled(not is_ground)
        self._floor_plus_btn.setEnabled(not is_ground)
        if is_ground:
            self.floor_number.setValue(0)
        self._clear_error(self.floor_number)

    # ── Data loading ────────────────────────────────────────────────────────

    def _load_data(self):
        self._load_sites_from_csv()
        self._load_incharge_from_csv()

    def _load_sites_from_csv(self):
        try:
            with open(config.SITES_CSV, newline='') as f:
                reader = csv.DictReader(f)
                sites = sorted({row['site'] for row in reader if row.get('site')})
            self._set_sites(sites)
        except OSError as e:
            print(f'[scan_setup] cannot read {config.SITES_CSV}: {e}', file=sys.stderr)
            self._set_sites([])

    def _set_sites(self, sites: list):
        self._all_sites = list(sites)
        self._site_model.setStringList(self._all_sites)
        self.site_combo.clear()
        self.site_combo.addItems(self._all_sites)
        self.site_combo.setCurrentIndex(-1)
        self.site_combo.lineEdit().clear()

    def _load_incharge_from_csv(self):
        try:
            with open(config.INCHARGE_CSV, newline='') as f:
                reader = csv.DictReader(f)
                names = sorted({row['name'] for row in reader if row.get('name')})
            self._set_incharge(names)
        except OSError as e:
            print(f'[scan_setup] cannot read {config.INCHARGE_CSV}: {e}', file=sys.stderr)
            self._set_incharge([])

    def _set_incharge(self, names: list):
        self._all_incharge = list(names)
        self._incharge_model.setStringList(self._all_incharge)
        self.incharge_combo.clear()
        self.incharge_combo.addItems(self._all_incharge)
        self.incharge_combo.setCurrentIndex(-1)
        self.incharge_combo.lineEdit().clear()

    # ── Persist new entries so they autocomplete next time ──────────────────

    @staticmethod
    def _append_csv_value(path: str, column: str, value: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        needs_header = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, 'a', newline='') as f:
            writer = csv.writer(f)
            if needs_header:
                writer.writerow([column])
            writer.writerow([value])

    def _persist_new_site(self, site: str):
        if site.lower() in {s.lower() for s in self._all_sites}:
            return
        try:
            self._append_csv_value(config.SITES_CSV, 'site', site)
        except OSError as e:
            print(f'[scan_setup] could not save site: {e}', file=sys.stderr)
            return
        self._all_sites = sorted(self._all_sites + [site])
        self._site_model.setStringList(self._all_sites)
        self.site_combo.addItem(site)

    def _persist_new_incharge(self, name: str):
        if name.lower() in {n.lower() for n in self._all_incharge}:
            return
        try:
            self._append_csv_value(config.INCHARGE_CSV, 'name', name)
        except OSError as e:
            print(f'[scan_setup] could not save incharge: {e}', file=sys.stderr)
            return
        self._all_incharge = sorted(self._all_incharge + [name])
        self._incharge_model.setStringList(self._all_incharge)
        self.incharge_combo.addItem(name)

    # ── Inline error helpers ────────────────────────────────────────────────

    def _show_error(self, msg: str, widget=None):
        self.status_label.setText(f'✕   {msg}')
        self.status_label.setStyleSheet(
            f'font-size: 11pt; font-weight: bold; color: {DANGER};')
        if widget is not None:
            error_border = f'border: 2px solid {DANGER};'
            if isinstance(widget, QComboBox):
                widget.setStyleSheet(self._combo_style + f'QComboBox {{ {error_border} }}')
            elif isinstance(widget, QSpinBox):
                widget.setStyleSheet(self._spin_style + f'QSpinBox {{ {error_border} }}')
            else:
                widget.setStyleSheet(self._line_style + f'QLineEdit {{ {error_border} }}')

    def _clear_error(self, widget=None):
        self.status_label.setText('')
        self.status_label.setStyleSheet(f'font-size: 11pt; color: {SUBTLE};')
        if widget is not None:
            if isinstance(widget, QComboBox):
                widget.setStyleSheet(self._combo_style)
            elif isinstance(widget, QSpinBox):
                widget.setStyleSheet(self._spin_style)
            else:
                widget.setStyleSheet(self._line_style)

    # ── Submit ──────────────────────────────────────────────────────────────

    _FORBIDDEN = set('/\\:*?"<>|\0') | {chr(c) for c in range(32)}

    @classmethod
    def _check_folder_safe(cls, value: str) -> str:
        bad = sorted({c for c in value if c in cls._FORBIDDEN})
        if bad:
            shown = ', '.join(repr(c) for c in bad[:3])
            return f'Remove illegal characters: {shown}'
        return ''

    def _on_submit(self):
        site = self.site_combo.lineEdit().text().strip()
        if not site:
            self._show_error('Please select or enter a Site.', self.site_combo)
            self.site_combo.setFocus()
            return
        err = self._check_folder_safe(site)
        if err:
            self._show_error(err, self.site_combo)
            self.site_combo.setFocus()
            return

        incharge = self.incharge_combo.lineEdit().text().strip()
        if not incharge:
            self._show_error('Please select or enter a Scan Incharge.', self.incharge_combo)
            self.incharge_combo.setFocus()
            return
        err = self._check_folder_safe(incharge)
        if err:
            self._show_error(err, self.incharge_combo)
            self.incharge_combo.setFocus()
            return

        scan_part = self.scan_part.text().strip()
        if not scan_part:
            self._show_error('Please enter a Scan Part.', self.scan_part)
            self.scan_part.setFocus()
            return
        err = self._check_folder_safe(scan_part)
        if err:
            self._show_error(err, self.scan_part)
            self.scan_part.setFocus()
            return

        floor_type = self.floor_type_combo.currentText()
        floor_num  = self.floor_number.value()
        if floor_type != 'Ground Floor' and floor_num <= 0:
            self._show_error('Floor Number must be greater than 0.', self.floor_number)
            self.floor_number.setFocus()
            return

        metadata = {
            'site':       site,
            'floor_type': floor_type,
            'floor_num':  floor_num,
            'incharge':   incharge,
            'scan_part':  scan_part,
            'device':     config.DEVICE,
        }

        self._persist_new_site(site)
        self._persist_new_incharge(incharge)

        self.scan_requested.emit(metadata)

    def reset_form(self):
        self.site_combo.setCurrentIndex(-1)
        self.site_combo.lineEdit().clear()
        self.incharge_combo.setCurrentIndex(-1)
        self.incharge_combo.lineEdit().clear()
        self.scan_part.clear()
        self.floor_number.setValue(0)
        self.floor_type_combo.setCurrentIndex(0)
        self.status_label.setText('')
