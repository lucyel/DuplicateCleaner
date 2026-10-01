import os
import random
import re
import shlex
from datetime import datetime
from decimal import Decimal
from dataclasses import replace
from pathlib import Path
from threading import Event

from PySide6.QtCore import QFileInfo, QRectF, QSettings, QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QBrush, QCloseEvent, QColor, QDesktopServices, QFont, QIcon, QPainter, QPalette, QPixmap, QTextOption
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTabBar, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QToolButton, QVBoxLayout, QWidget,
)

from .cleanup import recycle_selected
from .duplicate_folders import (FolderRecord, FolderGroup, FolderScanResult,
                                ensure_folder_current, recycle_duplicate_folders)
from .scanner import Reporter
from .scan_control import ScanControl
from .files import ensure_current
from .file_types import FILE_TYPES, file_type
from .scan_workflow import SCAN_MODES, run_selected_scans
from .similarity import PRESETS, SimilarResult
from .models import DuplicateGroup, FileRecord, Progress, RecycleResult, ScanResult, SearchCriteria, format_bytes
from .preview import ComparisonPreview
from .similar_tab import SimilarTab
from .sessions import (LoadResult, SaveResult, SessionData, load_session as load_session_file,
                       save_session as save_session_file)
from .thumbnails import ThumbnailController


def parse_filter_terms(value: str, *, path: bool = False) -> tuple[tuple[str, bool], ...]:
    lexer = shlex.shlex(value, posix=True)
    lexer.whitespace = ","
    lexer.whitespace_split = True
    lexer.quotes = '"'
    # Preserve Windows path separators and literal apostrophes/hash characters.
    lexer.escape = ""
    lexer.commenters = ""
    terms = []
    for term in lexer:
        term = term.strip()
        if not term:
            continue
        excluded = term.startswith("-")
        if term[0] in "+-":
            term = term[1:].strip()
        if not term:
            raise ValueError("Enter text after + or -.")
        if path:
            term = term.replace("\\", "/")
        terms.append((term.casefold(), excluded))
    return tuple(terms)


STYLE = """
QMainWindow, QDialog { background: #f4f6f9; }
QWidget { color: #25334a; font-family: 'Segoe UI'; font-size: 10pt; }
QLabel#hint { color: #64748b; }
QLabel#filterError { color: #b91c1c; }
QLineEdit, QComboBox { background: white; border: 1px solid #ccd5e2; border-radius: 5px; padding: 5px; }
QLabel#section { font-size: 12pt; font-weight: 600; }
QLabel#metadataBadge { background: #dcfce7; color: #166534; border-radius: 6px; padding: 6px 10px; }
QLabel#metadataBadge[warning="true"] { background: #fef3c7; color: #92400e; }
QLabel#summary { background: white; border: 1px solid #e0e6ef; border-radius: 10px;
                  padding: 16px; font-size: 12pt; font-weight: 600; }
QPushButton { background: white; border: 1px solid #ccd5e2; border-radius: 7px;
              padding: 9px 14px; font-weight: 600; }
QPushButton:hover { background: #eaf0f8; border-color: #8aa7cc; }
QPushButton:pressed { background: #dce7f6; }
QToolButton#themeToggle, QToolButton#filterToggle {
    background: transparent; border: none; border-radius: 6px;
}
QToolButton#themeToggle:hover,
QToolButton#filterToggle:hover, QToolButton#filterToggle:checked { background: #e0ebff; }
QToolButton#themeToggle:focus,
QToolButton#filterToggle:focus { border: 1px solid #60a5fa; }
QPushButton#primary { background: #2563eb; border-color: #2563eb; color: white; }
QPushButton#primary:hover { background: #1d4ed8; }
QPushButton:disabled { background: #edf0f4; border-color: #e0e5eb; color: #9aa5b4; }
QPushButton#primary:disabled { background: #edf0f4; border-color: #e0e5eb; color: #9aa5b4; }
QTreeWidget, QListWidget, QPlainTextEdit { background: white; border: 1px solid #dce3ed;
                                        border-radius: 8px; padding: 5px; }
QTreeWidget { alternate-background-color: #f8fafc; }
QTreeWidget::item { padding: 8px 4px; }
QTreeWidget::item:selected { background: #e0ebff; color: #14305c; }
QTreeWidget::indicator, QListWidget::indicator { width: 17px; height: 17px; }
QTabBar::tab { background: #edf2f8; padding: 8px; border-bottom: 2px solid transparent; }
QTabBar::tab:hover { background: #e0ebff; }
QTabBar::tab:selected { background: white; color: #1d4ed8; border-bottom-color: #2563eb; }
QHeaderView::section { background: #edf2f8; color: #4a5f7c; border: none;
                       padding: 12px 8px; font-weight: 600; }
QProgressBar { background: #e3e9f2; border: none; border-radius: 4px; min-height: 8px; }
QProgressBar::chunk { background: #2563eb; border-radius: 4px; }
QCheckBox { spacing: 8px; }
QSplitter::handle { background: transparent; width: 16px; }
QFrame#thumbnailPopup { background: white; border: 1px solid #b9c8dc; border-radius: 8px; }
QToolTip { background: white; color: #25334a; border: 1px solid #b9c8dc; padding: 5px; }
"""

DARK_STYLE = """
QMainWindow, QDialog { background: #111827; }
QWidget { color: #e2e8f0; }
QLabel#hint { color: #a5b4c8; }
QLabel#filterError { color: #fca5a5; }
QLabel#metadataBadge { background: #16372a; color: #86efac; }
QLabel#metadataBadge[warning="true"] { background: #3b3520; color: #fde68a; }
QLineEdit, QComboBox { background: #172235; border-color: #475569; }
QLabel#summary { background: #1e293b; border-color: #334155; }
QPushButton { background: #243247; border-color: #475569; }
QPushButton:hover { background: #334155; border-color: #94a3b8; }
QPushButton:pressed { background: #3c4e68; }
QToolButton#themeToggle:hover,
QToolButton#filterToggle:hover, QToolButton#filterToggle:checked { background: #334155; }
QPushButton:disabled, QPushButton#primary:disabled {
    background: #1a2536; border-color: #334155; color: #7f8da3;
}
QTreeWidget, QListWidget, QPlainTextEdit { background: #172235; border-color: #334155; }
QTreeWidget { alternate-background-color: #1b293e; }
QTreeWidget::item:selected, QListWidget::item:selected { background: #264b78; color: #f1f5f9; }
QTabBar::tab { background: #243247; }
QTabBar::tab:hover { background: #334155; }
QTabBar::tab:selected { background: #172235; color: #93c5fd; border-bottom-color: #60a5fa; }
QTreeWidget::indicator:unchecked, QListWidget::indicator:unchecked, QCheckBox::indicator:unchecked {
    background: #111827; border: 1px solid #7f8da3; border-radius: 2px;
}
QTreeWidget::indicator:unchecked:hover, QListWidget::indicator:unchecked:hover, QCheckBox::indicator:unchecked:hover {
    background: #243247; border-color: #bfdbfe;
}
QHeaderView::section { background: #243247; color: #cbd5e1; }
QProgressBar { background: #243247; }
QFrame#thumbnailPopup { background: #1e293b; border-color: #475569; }
QToolTip { background: #1e293b; color: #e2e8f0; border-color: #475569; }
"""


def apply_theme(dark: bool):
    app = QApplication.instance()
    palette = app.style().standardPalette()
    if dark:
        # The palette also covers controls drawn by Qt, such as scrollbars and checkboxes.
        colors = {
            QPalette.ColorRole.Window: "#111827",
            QPalette.ColorRole.WindowText: "#e2e8f0",
            QPalette.ColorRole.Base: "#172235",
            QPalette.ColorRole.AlternateBase: "#1b293e",
            QPalette.ColorRole.Text: "#e2e8f0",
            QPalette.ColorRole.Button: "#243247",
            QPalette.ColorRole.ButtonText: "#e2e8f0",
            QPalette.ColorRole.Highlight: "#264b78",
            QPalette.ColorRole.HighlightedText: "#f1f5f9",
            QPalette.ColorRole.ToolTipBase: "#1e293b",
            QPalette.ColorRole.ToolTipText: "#e2e8f0",
            QPalette.ColorRole.Link: "#93c5fd",
            QPalette.ColorRole.PlaceholderText: "#a5b4c8",
            QPalette.ColorRole.Light: "#475569",
            QPalette.ColorRole.Midlight: "#334155",
            QPalette.ColorRole.Mid: "#243247",
            QPalette.ColorRole.Dark: "#0b1220",
            QPalette.ColorRole.Shadow: "#070d18",
        }
        for role, color in colors.items():
            palette.setColor(role, QColor(color))
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
            palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#7f8da3"))
    app.setPalette(palette)
    app.setStyleSheet(STYLE + (DARK_STYLE if dark else ""))


def control_icon(kind: str, dark: bool, color: str | None = None) -> QIcon:
    color = color or ("#e2e8f0" if dark else "#25334a")
    shapes = {
        "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2'
               'M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
        "moon": '<path d="M20.5 14A9 9 0 0 1 10 3.5 9 9 0 1 0 20.5 14Z"/>',
        "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/>',
        "checked": '<circle cx="12" cy="12" r="9"/><path d="m7.5 12 3 3 6-6"/>',
        "all_checked": '<path d="m12 3 10 18H2Z M12 9v5 M12 17v.5"/>',
    }
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
           f'fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round" '
           f'stroke-linejoin="round">{shapes[kind]}</svg>')
    pixmap = QPixmap(48, 48)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(svg.encode()).render(painter, QRectF(0, 0, 24, 24))
    painter.end()
    return QIcon(pixmap)


class Worker(QThread):
    progress = Signal(object)
    outcome = Signal(object)
    failed = Signal(str)
    mode_started = Signal(str)
    mode_finished = Signal(str, object, str)
    paused = Signal()

    def __init__(self, job, parent=None, *, scan_run=False):
        super().__init__(parent)
        self.job = job
        self.cancel_event = ScanControl(self.paused.emit) if scan_run else Event()
        self.scan_run = scan_run

    def run(self):
        try:
            callbacks = (dict(on_mode_started=self.mode_started.emit, on_mode_finished=self.mode_finished.emit)
                         if self.scan_run else {})
            result = self.job(cancel=self.cancel_event, progress=self.progress.emit, **callbacks)
            self.outcome.emit(result)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ResultItem(QTreeWidgetItem):
    group_sort_keys = None

    def __lt__(self, other):
        column = self.treeWidget().sortColumn()
        if self.group_sort_keys is not None and other.group_sort_keys is not None:
            return self.group_sort_keys[column] < other.group_sort_keys[column]
        if column == 2:
            return self.data(2, Qt.ItemDataRole.UserRole) < other.data(2, Qt.ItemDataRole.UserRole)
        return self.text(column).casefold() < other.text(column).casefold()


class ResultDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        background = index.data(Qt.ItemDataRole.BackgroundRole)
        if not index.parent().isValid() and background is not None and background.style() != Qt.BrushStyle.NoBrush:
            # Keep checked-group colors visible while the group is the current row.
            option = QStyleOptionViewItem(option)
            self.initStyleOption(option, index)
            option.state &= ~QStyle.StateFlag.State_Selected
            style = option.widget.style() if option.widget else QApplication.style()
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)
        else:
            super().paint(painter, option, index)


class ResultTree(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setItemDelegate(ResultDelegate(self))

    def mouseDoubleClickEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and index.data(Qt.ItemDataRole.CheckStateRole) is not None:
            option = QStyleOptionViewItem()
            option.initFrom(self)
            option.rect = self.visualRect(index)
            option.features |= QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
            checkbox = self.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator,
                                                    option, self)
            if checkbox.contains(event.position().toPoint()):
                return
        super().mouseDoubleClickEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("DuplicateCleaner", "DuplicateCleaner")
        dark = self.settings.value("appearance/dark_mode", False, type=bool)
        apply_theme(dark)
        self.setWindowTitle("Duplicate Cleaner")
        self.resize(1220, 800)
        self.setMinimumSize(940, 720)
        self.worker = None
        self.scan_worker = None
        self.scan_mode_states = {}
        self._scan_modes_running = ()
        self._scan_messages = {}
        self._last_scan_progress = None
        self._scan_paused = False
        self._restart_scan = False
        self._scan_settings = None
        self._scan_baselines = {}
        self._baseline_settings = None
        self._incremental_run = False
        self._incremental_states = {}
        self._scan_recycled = []
        self._scan_cache_valid = True
        self.groups: list[DuplicateGroup] = []
        self.selected: set[Path] = set()
        self.visible_paths: set[Path] = set()
        self.result_filters = {}
        self.summary_notice = "Ready to scan"
        self.summary_context = ""
        self.records: dict[Path, FileRecord] = {}
        self.issues = []
        self._changing_checks = False
        self.session_available = False
        self.session_path: Path | None = None
        self.scan_file_count = 0
        self.scan_total_bytes = 0
        self.folder_groups: list[FolderGroup] = []
        self.duplicate_folders: list[FolderRecord] = []
        self.selected_duplicate_folders: set[Path] = set()
        self.duplicate_folder_issues = []
        self._changing_empty_checks = False

        container = QWidget()
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        corner_controls = QWidget()
        corner_layout = QHBoxLayout(corner_controls)
        corner_layout.setContentsMargins(0, 0, 0, 0)
        corner_layout.setSpacing(4)
        self.filter_toggle = QToolButton()
        self.filter_toggle.setObjectName("filterToggle")
        self.filter_toggle.setFixedSize(36, 36)
        self.filter_toggle.setIconSize(QSize(24, 24))
        self.filter_toggle.setCheckable(True)
        self.filter_toggle.setToolTip("Show filters")
        self.filter_toggle.setAccessibleName("Show filters")
        self.filter_toggle.toggled.connect(self.toggle_filters)
        corner_layout.addWidget(self.filter_toggle)
        self.dark_mode = QToolButton()
        self.dark_mode.setObjectName("themeToggle")
        self.dark_mode.setFixedSize(36, 36)
        self.dark_mode.setIconSize(QSize(24, 24))
        self.dark_mode.setCheckable(True)
        self.dark_mode.setChecked(dark)
        self.dark_mode.toggled.connect(self.change_theme)
        corner_layout.addWidget(self.dark_mode)

        self.location_page = QWidget()
        side = QVBoxLayout(self.location_page)
        side.setContentsMargins(18, 16, 18, 16)
        side.setSpacing(6)
        section = QLabel("Choose folders")
        section.setObjectName("section")
        side.addWidget(section)
        self.folders = QListWidget()
        self.folders.setToolTip("Add one or more folders. Files are compared within and across them.")
        self.folders.setMinimumHeight(60)
        self.folders.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        side.addWidget(self.folders, 1)
        self.add_button = QPushButton("+ Add folder")
        self.remove_button = QPushButton("Remove")
        self.remove_button.setAccessibleName("Remove selected folders")
        self.remove_button.setToolTip("Remove selected folders from the scan list")
        self.add_button.clicked.connect(self.add_folder)
        self.remove_button.clicked.connect(self.remove_folders)
        folder_actions = QHBoxLayout()
        folder_actions.addWidget(self.add_button, 1)
        folder_actions.addWidget(self.remove_button, 1)
        side.addLayout(folder_actions)
        excluded_label = QLabel("Excluded subfolders")
        excluded_label.setObjectName("section")
        side.addWidget(excluded_label)
        self.excluded_folders = QListWidget()
        self.excluded_folders.setAccessibleName("Excluded subfolders")
        self.excluded_folders.setToolTip(
            "These subfolders and everything inside them are skipped by all selected scans. "
            "Changes apply to the next scan.")
        self.excluded_folders.setMinimumHeight(60)
        self.excluded_folders.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.excluded_folders.itemSelectionChanged.connect(self.update_actions)
        side.addWidget(self.excluded_folders, 1)
        exclusion_actions = QHBoxLayout()
        self.exclude_button = QPushButton("+ Exclude…")
        self.exclude_button.setToolTip("Choose a subfolder of an added scan folder to skip.")
        self.exclude_button.clicked.connect(self.choose_excluded_folder)
        self.remove_exclusion_button = QPushButton("Remove")
        self.remove_exclusion_button.setToolTip("Include the selected excluded subfolders in future scans.")
        self.remove_exclusion_button.clicked.connect(self.remove_exclusions)
        exclusion_actions.addWidget(self.exclude_button)
        exclusion_actions.addWidget(self.remove_exclusion_button)
        side.addLayout(exclusion_actions)
        self.recursive = QCheckBox("Include subfolders")
        self.recursive.setChecked(True)
        side.addWidget(self.recursive)
        self.scan_button = QPushButton("Scan")
        self.scan_button.setToolTip("Scanning never changes files. Links and unsafe files are skipped.")
        self.scan_button.setObjectName("primary")
        self.scan_button.clicked.connect(self.start_scan)
        self.scan_added_button = QPushButton("Scan added folders")
        self.scan_added_button.clicked.connect(self.start_added_scan)
        self.pause_button = QPushButton("Pause scan")
        self.pause_button.clicked.connect(self.toggle_scan_pause)
        self.pause_button.setToolTip("Pause to add disks or change criteria. Changed settings restart the scan on Resume.")
        self.save_session_button = QPushButton("Save session…")
        self.save_session_button.setToolTip("Save these results and checked files so you can continue later.")
        self.save_session_button.clicked.connect(self.save_session)
        self.load_session_button = QPushButton("Load session…")
        self.load_session_button.setToolTip("Load saved results without scanning file contents again.")
        self.load_session_button.clicked.connect(self.load_session)
        self.update_control_icons()

        self.workflow_tabs = QTabWidget()
        self.workflow_tabs.setAccessibleName("Cleaner tools")
        self.workflow_tabs.setCornerWidget(corner_controls, Qt.Corner.TopRightCorner)
        layout.addWidget(self.workflow_tabs, 1)
        duplicate_page = QWidget()
        content = QVBoxLayout(duplicate_page)
        content.setContentsMargins(12, 12, 12, 12)
        content.setSpacing(12)
        duplicate_scroll = QScrollArea()
        duplicate_scroll.setWidgetResizable(True)
        duplicate_scroll.setFrameShape(QFrame.Shape.NoFrame)
        duplicate_scroll.setWidget(duplicate_page)
        duplicate_container = QWidget()
        duplicate_layout = QVBoxLayout(duplicate_container)
        duplicate_layout.setContentsMargins(0, 0, 0, 0)
        duplicate_layout.setSpacing(0)
        duplicate_layout.addWidget(duplicate_scroll, 1)
        self.duplicate_page = duplicate_container
        self.criteria_page = QWidget()
        criteria_layout = QVBoxLayout(self.criteria_page)
        criteria_layout.setContentsMargins(12, 12, 12, 12)
        criteria_help = QLabel("Choose one or more scan modes and file types, then click Scan. Folders are configured in Scan location.")
        criteria_help.setWordWrap(True)
        criteria_layout.addWidget(criteria_help)
        modes_box = QGroupBox("Scan modes")
        mode_layout = QHBoxLayout(modes_box)
        self.mode_checks = {}
        for mode, title in SCAN_MODES.items():
            checkbox = QCheckBox(title)
            checkbox.setChecked(mode == "duplicates")
            checkbox.toggled.connect(self.update_actions)
            self.mode_checks[mode] = checkbox
            mode_layout.addWidget(checkbox)
        criteria_layout.addWidget(modes_box)
        self.file_types_box = QGroupBox("File types to scan")
        types_layout = QGridLayout(self.file_types_box)
        self.all_file_types = QCheckBox("All file types")
        self.all_file_types.setChecked(True)
        self.all_file_types.toggled.connect(self.all_types_changed)
        types_layout.addWidget(self.all_file_types, 0, 0, 1, 3)
        self.file_type_checks = {}
        for index, name in enumerate((*FILE_TYPES, "Other")):
            checkbox = QCheckBox(name)
            checkbox.setToolTip(", ".join(sorted(FILE_TYPES[name])) if name in FILE_TYPES
                                else "All remaining extensions, including files with no extension")
            checkbox.toggled.connect(self.file_types_changed)
            self.file_type_checks[name] = checkbox
            types_layout.addWidget(checkbox, 1 + index // 3, index % 3)
        type_help = QLabel("Types are selected by file extension. Duplicate-folder scans always inspect every entry.\n"
                           "Similarity supports decodable images and videos only; other chosen types are skipped.")
        type_help.setWordWrap(True)
        types_layout.addWidget(type_help, 3, 0, 1, 3)
        criteria_layout.addWidget(self.file_types_box)
        self.similarity_options = QGroupBox("Similarity options")
        similarity_layout = QHBoxLayout(self.similarity_options)
        similarity_layout.addWidget(QLabel("Similarity"))
        self.similarity_preset = QComboBox()
        self.similarity_preset.addItems(PRESETS)
        self.similarity_preset.setCurrentText("Balanced")
        self.similarity_preset.setAccessibleName("Image and video similarity preset")
        similarity_layout.addWidget(self.similarity_preset)
        similarity_layout.addStretch()
        criteria_layout.addWidget(self.similarity_options)
        self.criteria_checks = {}
        defaults = SearchCriteria()

        def check(key, label, tip):
            widget = QCheckBox(label)
            widget.setToolTip(tip)
            widget.setChecked(getattr(defaults, key))
            self.criteria_checks[key] = widget
            return widget

        def row(parent, *widgets):
            line = QHBoxLayout()
            for widget in widgets:
                line.addWidget(widget)
            line.addStretch()
            parent.addLayout(line)

        content_box = QGroupBox("Duplicate comparison — file contents")
        content_options = QVBoxLayout(content_box)
        row(content_options,
            check("hashes", "Same file hash (SHA-256)", "Compare full main-file SHA-256 hashes. Hash matching requires equal size."),
            check("contents", "Byte-for-byte comparison", "Read and compare every main-file byte. Requires equal size, even with a size tolerance."),
            check("streams", "Same NTFS extra data", "Require matching extra stream names, sizes, and SHA-256 hashes."))
        criteria_layout.addWidget(content_box)
        options_box = QGroupBox("More duplicate options")
        options = QVBoxLayout(options_box)
        row(options,
            check("filename", "Same file name", "Compare the complete filename including its extension."),
            check("extension", "Same file extension", "Compare the final file extension."),
            check("similar_names", "Similar file names", "Allow up to the chosen number of character insertions, deletions, or substitutions in the complete filename."))
        row(options, check("ignore_copy", 'Ignore "Copy" part of filename',
                           'Ignore common prefixes such as "Copy of " and suffixes such as " - Copy (2)"; keep words such as "copyright" intact.'))
        self.size_tolerance = QSpinBox()
        self.size_tolerance.setRange(0, 2_147_483_647)
        self.size_tolerance.setAccessibleName("File size tolerance in bytes")
        self.size_tolerance.setToolTip("Maximum size difference between any two files in a group. Hash and byte comparisons still require equal size.")
        row(options, check("size", "Same file size", "Compare main-file bytes, excluding NTFS extra streams."),
            self.size_tolerance, QLabel("Bytes tolerance"))
        row(options, check("created", "Same created date/time", "Compare filesystem creation timestamps."),
            check("created_date_only", "Match date only", "Compare the creation calendar date in your local timezone, ignoring time."))
        row(options, check("modified", "Same modified date/time", "Compare exact modification timestamps."),
            check("modified_date_only", "Match date only", "Compare the modification calendar date in your local timezone, ignoring time."))
        row(options, check("same_drive", "Same drive", "Require the same filesystem volume."))
        self.folder_depth = QSpinBox()
        self.folder_depth.setRange(1, 1000)
        self.folder_depth.setAccessibleName("Folder match depth")
        row(options, check("folder", "Same folder name", "Match the immediate parent folder name unless a folder modifier is selected."),
            check("full_folder", "Match full folder name", "Compare the complete parent folder path."))
        row(options, check("folder_depth_enabled", "Match depth from top", "Compare the first N folder components below the drive/share, or below the search root when selected. Overrides full-folder matching."),
            self.folder_depth,
            check("from_search_root", "Match from search root", "Compare relative folder paths below each scan root. For overlapping roots, use the most specific root."))
        row(options, check("ignore_same_folder", "Ignore duplicate groups within the same folder", "Hide groups where all files have the same parent folder. Groups spanning folders retain all copies."))
        criteria_layout.addWidget(options_box)
        text_box = QGroupBox("Duplicate comparison — text options")
        text_options = QVBoxLayout(text_box)
        self.text_tolerance = QSpinBox()
        self.text_tolerance.setRange(0, 255)
        self.text_tolerance.setValue(3)
        self.text_tolerance.setAccessibleName("Similar filename text tolerance")
        row(text_options, check("case_sensitive", "Is case sensitive", "Apply case-sensitive comparisons to filename, extension, folder names, and Copy markers."),
            self.text_tolerance, QLabel("Similar text — tolerance"))
        criteria_layout.addWidget(text_box)
        self.duplicate_criteria_boxes = (content_box, options_box, text_box)
        self.criteria_hint = QLabel()
        self.criteria_hint.setWordWrap(True)
        self.criteria_hint.setObjectName("hint")
        criteria_layout.addWidget(self.criteria_hint)
        criteria_layout.addStretch()
        self.criteria_scroll = QScrollArea()
        self.criteria_scroll.setWidgetResizable(True)
        self.criteria_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.criteria_scroll.setWidget(self.criteria_page)
        for widget in self.criteria_checks.values():
            widget.toggled.connect(self.update_actions)
        self.criteria_checks["created_date_only"].setAccessibleName("Match created date only")
        self.criteria_checks["modified_date_only"].setAccessibleName("Match modified date only")
        for widget in (self.size_tolerance, self.folder_depth, self.text_tolerance):
            widget.valueChanged.connect(self.update_actions)
        self.summary = QLabel("Ready to scan")
        self.summary.setObjectName("summary")
        self.summary.setWordWrap(True)
        content.addWidget(self.summary)
        results_heading = QHBoxLayout()
        section = QLabel("Review matching files")
        section.setObjectName("section")
        results_heading.addWidget(section)
        results_heading.addStretch()
        self.preview_toggle = QPushButton("Hide preview")
        self.preview_toggle.setCheckable(True)
        self.preview_toggle.setChecked(True)
        self.preview_toggle.toggled.connect(self.update_details)
        results_heading.addWidget(self.preview_toggle)
        self.open_button = QPushButton("Open file location")
        self.open_button.clicked.connect(self.open_folder)
        results_heading.addWidget(self.open_button)
        content.addLayout(results_heading)
        self.type_tabs = QTabBar()
        self.type_tabs.setAccessibleName("Filter duplicate results")
        self.type_tabs.setExpanding(False)
        self.type_tabs.setUsesScrollButtons(True)
        self.type_tabs.setDrawBase(False)
        for name in ("All", "Selected", *FILE_TYPES, "Other"):
            index = self.type_tabs.addTab(name)
            if name == "All":
                tooltip = "All duplicate files"
            elif name == "Selected":
                tooltip = "Complete duplicate groups containing at least one checked file"
            else:
                tooltip = f"{name} files, grouped by filename extension"
            self.type_tabs.setTabToolTip(index, tooltip)
        content.addWidget(self.type_tabs)
        self.filter_panel = QWidget()
        filter_layout = QGridLayout(self.filter_panel)
        filter_layout.setContentsMargins(12, 8, 12, 4)
        filter_layout.setSpacing(6)
        self.filename_filter = QLineEdit()
        self.file_path_filter = QLineEdit()
        self.folder_path_filter = QLineEdit()
        for column, (label, field) in enumerate((
                ("Filename", self.filename_filter), ("File path", self.file_path_filter),
                ("Folder path", self.folder_path_filter))):
            caption = QLabel(label)
            caption.setBuddy(field)
            field.setAccessibleName(label + " filter")
            field.setPlaceholderText("text, -exclude")
            field.setClearButtonEnabled(True)
            field.setToolTip(
                'Separate terms with commas; use -term to exclude. Matching ignores capitalization. '
                'Spaces stay inside a term. Quote terms containing commas, such as "photos, 2026". '
                'Use + before a term that literally starts with + or -. All terms apply to the same file.')
            field.returnPressed.connect(self.apply_result_filters)
            filter_layout.addWidget(caption, 0, column)
            filter_layout.addWidget(field, 1, column)
        syntax_hint = QLabel("Separate terms with commas. Use -term to exclude. All terms must match.")
        syntax_hint.setObjectName("hint")
        syntax_hint.setWordWrap(True)
        filter_layout.addWidget(syntax_hint, 2, 0, 1, 3)
        size_row = QHBoxLayout()
        self.min_size_filter = QLineEdit()
        self.max_size_filter = QLineEdit()
        size_label = QLabel("File size")
        size_label.setBuddy(self.min_size_filter)
        size_row.addWidget(size_label)
        for label, field in (("Minimum", self.min_size_filter), ("Maximum", self.max_size_filter)):
            field.setAccessibleName(label + " file size")
            field.setPlaceholderText(label)
            field.setMaxLength(32)
            field.setMinimumWidth(60)
            field.setToolTip(label + " size, inclusive. Leave blank for no limit; decimals are allowed.")
            field.returnPressed.connect(self.apply_result_filters)
            size_row.addWidget(field, 1)
        self.size_unit = QComboBox()
        self.size_unit.addItems(["B", "KiB", "MiB", "GiB"])
        self.size_unit.setCurrentIndex(2)
        self.size_unit.setAccessibleName("File size unit")
        size_row.addWidget(self.size_unit)
        self.apply_filter_button = QPushButton("Apply")
        self.apply_filter_button.setToolTip("Show groups containing a file that matches all filled filters.")
        self.apply_filter_button.clicked.connect(self.apply_result_filters)
        self.clear_filter_button = QPushButton("Clear filters")
        self.clear_filter_button.clicked.connect(self.clear_result_filters)
        size_row.addWidget(self.apply_filter_button)
        size_row.addWidget(self.clear_filter_button)
        filter_layout.addLayout(size_row, 3, 0, 1, 3)
        self.filter_error = QLabel()
        self.filter_error.setObjectName("filterError")
        self.filter_error.setWordWrap(True)
        self.filter_error.hide()
        filter_layout.addWidget(self.filter_error, 4, 0, 1, 3)
        # Keep filters accessible even when the results/preview need horizontal scrolling.
        duplicate_layout.insertWidget(0, self.filter_panel)
        self.filter_panel.hide()
        self.filter_hint = QLabel()
        self.filter_hint.setObjectName("hint")
        self.filter_hint.setWordWrap(True)
        self.filter_hint.hide()
        content.addWidget(self.filter_hint)
        self.empty = QLabel("Click a group to preview all copies, or a file to compare two copies.\nEvery checkbox starts unchecked. You decide what to remove.")
        self.empty.setObjectName("hint")
        self.empty.setWordWrap(True)
        content.addWidget(self.empty)
        self.tree = ResultTree()
        self.tree.setHeaderLabels(["File / duplicate group", "Full location", "Size", "Modified"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 340)
        self.tree.setColumnWidth(2, 115)
        self.tree.setColumnWidth(3, 150)
        self.tree.itemChanged.connect(self.item_changed)
        self.tree.itemSelectionChanged.connect(self.update_actions)
        self.tree.itemSelectionChanged.connect(self.update_details)
        self.tree.currentItemChanged.connect(self.update_details)
        self.tree.itemDoubleClicked.connect(self.open_file)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_result_menu)
        self.thumbnails = ThumbnailController(self.tree, self)
        self.results_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.results_splitter.setChildrenCollapsible(False)
        self.results_splitter.setHandleWidth(8)
        self.tree.setMinimumHeight(160)
        self.tree.setMinimumWidth(290)
        self.results_splitter.addWidget(self.tree)
        self.details_panel = QWidget()
        self.details_panel.setMinimumWidth(380)
        details_layout = QVBoxLayout(self.details_panel)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(4)
        details_title = QLabel("Group preview")
        details_title.setObjectName("section")
        details_layout.addWidget(details_title)
        self.comparison_preview = ComparisonPreview()
        self.comparison_preview.selection_changed.connect(self.preview_selection_changed)
        details_layout.addWidget(self.comparison_preview, 1)
        self.details_toggle = QPushButton("File details")
        self.details_toggle.setCheckable(True)
        details_layout.addWidget(self.details_toggle)
        self.details_text = QPlainTextEdit()
        self.details_text.setReadOnly(True)
        self.details_text.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.details_text.setMinimumHeight(90)
        self.details_text.setMaximumHeight(160)
        self.details_text.setAccessibleName("Selected file details")
        details_layout.addWidget(self.details_text)
        self.details_text.hide()
        self.details_toggle.toggled.connect(self.details_text.setVisible)
        self.details_toggle.toggled.connect(self.update_details)
        self.results_splitter.addWidget(self.details_panel)
        self.results_splitter.setStretchFactor(0, 2)
        self.results_splitter.setStretchFactor(1, 3)
        self.details_panel.hide()
        content.addWidget(self.results_splitter, 1)
        foot = QHBoxLayout()
        self.clear_button = QPushButton("Clear file selection")
        self.clear_button.clicked.connect(self.clear_selection)
        foot.addWidget(self.clear_button)
        self.select_folder_button = QPushButton("Select this folder's duplicates")
        self.select_folder_button.setToolTip(
            "Select matches in this exact folder, including hidden files. Leave one random copy unchecked "
            "if a group would otherwise have every copy selected; repeated clicks keep that copy.")
        self.select_folder_button.clicked.connect(lambda: self.select_folder_duplicates())
        foot.addWidget(self.select_folder_button)
        self.issue_button = QPushButton("Skipped files / errors (0)")
        self.issue_button.clicked.connect(self.show_issues)
        foot.addWidget(self.issue_button)
        foot.addStretch()
        self.recycle_button = QPushButton("Recycle selected files")
        self.recycle_button.setObjectName("primary")
        self.recycle_button.clicked.connect(self.confirm_recycle)
        foot.addWidget(self.recycle_button)
        content.addLayout(foot)

        self.selection_label = QLabel("0 files selected for recycling")
        self.selection_label.setObjectName("section")
        self.selection_label.setWordWrap(True)
        content.addWidget(self.selection_label)

        folder_page = QWidget()
        empty_content = QVBoxLayout(folder_page)
        empty_content.setContentsMargins(12, 12, 12, 12)
        empty_content.setSpacing(12)
        self.duplicate_folder_summary = QLabel("Ready to compare folders")
        self.duplicate_folder_summary.setObjectName("summary")
        self.duplicate_folder_summary.setWordWrap(True)
        empty_content.addWidget(self.duplicate_folder_summary)
        empty_heading = QHBoxLayout()
        empty_section = QLabel("Review duplicate and empty folders")
        empty_section.setObjectName("section")
        empty_heading.addWidget(empty_section)
        empty_heading.addStretch()
        empty_content.addLayout(empty_heading)
        self.duplicate_folder_hint = QLabel(
            "Folders match by file count and contents, including subfolders; names can differ.\n"
            "Every checkbox starts unchecked. Double-click a row to open the folder.")
        self.duplicate_folder_hint.setObjectName("hint")
        self.duplicate_folder_hint.setWordWrap(True)
        empty_content.addWidget(self.duplicate_folder_hint)
        self.duplicate_folder_tree = ResultTree()
        self.duplicate_folder_tree.setHeaderLabels(["Folder / group", "Full path", "Size", "Files"])
        self.duplicate_folder_tree.setRootIsDecorated(True)
        self.duplicate_folder_tree.setAlternatingRowColors(True)
        self.duplicate_folder_tree.setUniformRowHeights(True)
        self.duplicate_folder_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.duplicate_folder_tree.setSortingEnabled(True)
        self.duplicate_folder_tree.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.duplicate_folder_tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.duplicate_folder_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.duplicate_folder_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.duplicate_folder_tree.setColumnWidth(0, 170)
        self.duplicate_folder_tree.itemChanged.connect(self.duplicate_folder_item_changed)
        self.duplicate_folder_tree.itemDoubleClicked.connect(self.open_duplicate_folder)
        empty_content.addWidget(self.duplicate_folder_tree, 1)
        empty_foot = QHBoxLayout()
        self.clear_duplicate_folders_button = QPushButton("Clear folder selection")
        self.clear_duplicate_folders_button.clicked.connect(self.clear_duplicate_folder_selection)
        empty_foot.addWidget(self.clear_duplicate_folders_button)
        self.duplicate_folder_issue_button = QPushButton("Skipped folders / errors (0)")
        self.duplicate_folder_issue_button.clicked.connect(self.show_duplicate_folder_issues)
        empty_foot.addWidget(self.duplicate_folder_issue_button)
        empty_foot.addStretch()
        empty_content.addLayout(empty_foot)
        self.duplicate_folder_selection_label = QLabel("0 folders selected for recycling")
        self.duplicate_folder_selection_label.setObjectName("section")
        self.duplicate_folder_selection_label.setWordWrap(True)
        empty_content.addWidget(self.duplicate_folder_selection_label)
        empty_action = QHBoxLayout()
        empty_recycle_hint = QLabel(
            "Folders are checked again before being sent to the Recycle Bin.\n"
            "Nothing is permanently deleted.")
        empty_recycle_hint.setObjectName("hint")
        empty_recycle_hint.setWordWrap(True)
        empty_action.addWidget(empty_recycle_hint, 1)
        self.recycle_duplicate_folders_button = QPushButton("Recycle selected folders")
        self.recycle_duplicate_folders_button.setObjectName("primary")
        self.recycle_duplicate_folders_button.clicked.connect(self.confirm_duplicate_folder_recycle)
        empty_action.addWidget(self.recycle_duplicate_folders_button)
        empty_content.addLayout(empty_action)
        self.folder_page = folder_page
        self.similar_tab = SimilarTab()
        self.workflow_tabs.addTab(self.location_page, "Scan location")
        self.workflow_tabs.addTab(self.criteria_scroll, "Search criteria")
        self.workflow_tabs.addTab(self.duplicate_page, "Duplicate files")
        self.workflow_tabs.addTab(self.folder_page, "Duplicate folders")
        self.workflow_tabs.addTab(self.similar_tab, "Similar files")

        scan_actions = QHBoxLayout()
        scan_actions.addWidget(self.scan_button)
        scan_actions.addWidget(self.scan_added_button)
        scan_actions.addWidget(self.pause_button)
        scan_actions.addStretch()
        scan_actions.addWidget(self.save_session_button)
        scan_actions.addWidget(self.load_session_button)
        layout.addLayout(scan_actions)

        status_row = QHBoxLayout()
        self.status = QLabel("Add folders to begin.")
        self.status.setWordWrap(True)
        status_row.addWidget(self.status, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_work)
        status_row.addWidget(self.cancel_button)
        layout.addLayout(status_row)
        self.current_path = QLabel("")
        self.current_path.setObjectName("hint")
        self.current_path.setMinimumWidth(0)
        layout.addWidget(self.current_path)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        self.type_tabs.currentChanged.connect(self.filter_results)
        self.workflow_tabs.currentChanged.connect(self.update_details)
        self.workflow_tabs.currentChanged.connect(self.update_similarity_controls)
        self.update_actions()
        self.update_similarity_controls()

    def update_similarity_controls(self):
        separate = self.workflow_tabs.currentWidget() is self.similar_tab
        self.filter_toggle.setVisible(self.workflow_tabs.currentWidget() is self.duplicate_page)
        if not separate:
            self.similar_tab.clear_preview()
        else:
            self.similar_tab.review_selection()

    def selected_modes(self):
        return tuple(mode for mode, checkbox in self.mode_checks.items() if checkbox.isChecked())

    def selected_file_types(self):
        if self.all_file_types.isChecked():
            return None
        return tuple(name for name, checkbox in self.file_type_checks.items() if checkbox.isChecked())

    def all_types_changed(self, checked):
        if checked:
            for checkbox in self.file_type_checks.values():
                previous = checkbox.blockSignals(True)
                checkbox.setChecked(False)
                checkbox.blockSignals(previous)
        self.update_actions()

    def file_types_changed(self, checked):
        if checked:
            previous = self.all_file_types.blockSignals(True)
            self.all_file_types.setChecked(False)
            self.all_file_types.blockSignals(previous)
        self.update_actions()

    def search_criteria(self):
        return SearchCriteria(**{key: check.isChecked() for key, check in self.criteria_checks.items()},
                              size_tolerance=self.size_tolerance.value(), folder_depth=self.folder_depth.value(),
                              text_tolerance=self.text_tolerance.value())

    def change_theme(self, dark):
        apply_theme(dark)
        self.update_control_icons()
        self.update_group_highlights()
        self.settings.setValue("appearance/dark_mode", dark)

    def update_control_icons(self):
        dark = self.dark_mode.isChecked()
        self.dark_mode.setIcon(control_icon("sun" if dark else "moon", dark))
        label = "Switch to light mode" if dark else "Switch to dark mode"
        self.dark_mode.setToolTip(label)
        self.dark_mode.setAccessibleName(label)
        self.filter_toggle.setIcon(control_icon("search", dark))

    def toggle_filters(self, visible):
        self.filter_panel.setVisible(visible)
        label = "Hide filters" if visible else "Show filters"
        self.filter_toggle.setToolTip(label)
        self.filter_toggle.setAccessibleName(label)
        if visible:
            self.workflow_tabs.setCurrentWidget(self.duplicate_page)
            self.filename_filter.setFocus()

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder to scan")
        if folder:
            self.add_folder_path(folder)

    def add_folder_path(self, folder):
        folder = os.path.abspath(folder)
        existing = {os.path.normcase(self.folders.item(i).text()) for i in range(self.folders.count())}
        if os.path.normcase(folder) not in existing:
            self.folders.addItem(folder)
            self.folders.item(self.folders.count() - 1).setToolTip(folder)
        self.update_actions()

    def remove_folders(self):
        for item in self.folders.selectedItems():
            self.folders.takeItem(self.folders.row(item))
        for index in reversed(range(self.excluded_folders.count())):
            if not self.is_scan_subfolder(self.excluded_folders.item(index).text()):
                self.excluded_folders.takeItem(index)
        self.update_actions()

    def is_scan_subfolder(self, folder):
        parents = Path(os.path.abspath(folder)).parents
        return any(Path(self.folders.item(index).text()) in parents
                   for index in range(self.folders.count()))

    def choose_excluded_folder(self):
        if self.worker is not None or (self.scan_worker is not None and not self._scan_paused) or not self.folders.count():
            return
        root = self.folders.currentItem() or self.folders.item(0)
        folder = QFileDialog.getExistingDirectory(self, "Choose a subfolder to exclude", root.text())
        if folder:
            self.add_excluded_folder_path(folder)

    def add_excluded_folder_path(self, folder):
        folder = os.path.abspath(folder)
        if not self.is_scan_subfolder(folder):
            QMessageBox.warning(self, "Choose a subfolder",
                                "Choose a subfolder inside one of the added scan folders.")
            return
        if Path(folder) not in {Path(path) for path in self.excluded_folder_paths()}:
            self.excluded_folders.addItem(folder)
            self.excluded_folders.item(self.excluded_folders.count() - 1).setToolTip(folder)
        self.update_actions()

    def excluded_folder_paths(self):
        return tuple(self.excluded_folders.item(index).text()
                     for index in range(self.excluded_folders.count()))

    def remove_exclusions(self):
        for item in self.excluded_folders.selectedItems():
            self.excluded_folders.takeItem(self.excluded_folders.row(item))
        self.update_actions()

    def mode_busy(self, mode):
        return self.worker is not None or (self.scan_worker is not None
            and (self._incremental_run or self.scan_mode_states.get(mode) in ("Waiting", "Scanning")))

    def update_actions(self):
        busy = self.worker is not None or self.scan_worker is not None
        settings_locked = self.worker is not None or (self.scan_worker is not None and not self._scan_paused)
        duplicates_busy = self.mode_busy("duplicates")
        folders_busy = self.mode_busy("folders")
        criteria = self.search_criteria()
        modes = self.selected_modes()
        file_modes = any(mode != "folders" for mode in modes)
        self.file_types_box.setEnabled(file_modes)
        self.similarity_options.setEnabled("similarity" in modes)
        for box in self.duplicate_criteria_boxes:
            box.setEnabled("duplicates" in modes)
        self.similar_tab.set_busy(self.mode_busy("similarity"))
        self.criteria_page.setEnabled(not settings_locked)
        for key, enabled in (
                ("ignore_copy", criteria.filename or criteria.similar_names),
                ("created_date_only", criteria.created), ("modified_date_only", criteria.modified),
                ("full_folder", criteria.folder and not criteria.folder_depth_enabled),
                ("folder_depth_enabled", criteria.folder), ("from_search_root", criteria.folder)):
            self.criteria_checks[key].setEnabled(enabled)
        self.size_tolerance.setEnabled(criteria.size and not (criteria.hashes or criteria.contents))
        self.folder_depth.setEnabled(criteria.folder and criteria.folder_depth_enabled)
        self.text_tolerance.setEnabled(criteria.similar_names)
        self.criteria_hint.setText(
            "Choose at least one scan mode." if not modes else
            "Choose at least one file type or All file types." if file_modes and self.selected_file_types() == () else
            "Choose at least one duplicate comparison criterion." if "duplicates" in modes and not criteria.enabled else
            "Only the selected modes will run. Scanning never changes files." if "duplicates" not in modes else
            "Contents are verified byte for byte before files appear as duplicates." if criteria.contents else
            "Hashes match, but bytes are not compared during scanning. Recycling still verifies contents." if criteria.hashes else
            "Results are possible matches only. Contents must still match before recycling.")
        for widget in (self.add_button, self.remove_button, self.recursive, self.folders,
                       self.excluded_folders):
            widget.setEnabled(not settings_locked)
        for widget in (self.tree, self.type_tabs, self.filter_panel, self.comparison_preview):
            widget.setEnabled(not duplicates_busy)
        self.duplicate_folder_tree.setEnabled(not folders_busy)
        self.exclude_button.setEnabled(not settings_locked and self.folders.count() > 0)
        self.remove_exclusion_button.setEnabled(not settings_locked and bool(self.excluded_folders.selectedItems()))
        self.pause_button.setEnabled(self.scan_worker is not None and self.worker is None
                                     and not self.scan_worker.cancel_event.is_set())
        self.pause_button.setText("Resume scan" if self._scan_paused else "Pause scan")
        self.scan_button.setEnabled(not busy and self.folders.count() > 0 and bool(modes)
                                    and ("duplicates" not in modes or criteria.enabled)
                                    and (not file_modes or self.selected_file_types() != ()))
        added, reason = self.added_scan_scope()
        self.scan_added_button.setEnabled(not busy and bool(added) and not reason)
        self.scan_added_button.setToolTip(reason or "Scan only added locations and compare them with retained results. Old candidate copies may be read for verification.")
        self.save_session_button.setEnabled(not duplicates_busy and self.session_available)
        self.load_session_button.setEnabled(not busy)
        self.cancel_button.setEnabled(any(worker is not None and not worker.cancel_event.is_set()
                                          for worker in (self.worker, self.scan_worker)))
        self.recycle_button.setEnabled(not duplicates_busy and bool(self.selected))
        self.clear_button.setEnabled(not duplicates_busy and bool(self.selected))
        self.issue_button.setEnabled(bool(self.issues))
        self.issue_button.setText(f"Skipped files / errors ({len(self.issues):,})")
        current = self.tree.currentItem()
        current_is_file = current is not None and current.parent() is not None
        self.open_button.setEnabled(not duplicates_busy and current_is_file)
        self.select_folder_button.setEnabled(not duplicates_busy and current_is_file)
        self.recycle_duplicate_folders_button.setEnabled(
            not folders_busy and bool(self.selected_duplicate_folders))
        self.clear_duplicate_folders_button.setEnabled(
            not folders_busy and bool(self.selected_duplicate_folders))
        self.duplicate_folder_issue_button.setEnabled(bool(self.duplicate_folder_issues))
        self.duplicate_folder_issue_button.setText(
            f"Skipped folders / errors ({len(self.duplicate_folder_issues):,})")
        amount = sum(self.records[path].total_size for path in self.selected)
        noun = "file" if len(self.selected) == 1 else "files"
        hidden = len(self.selected - self.visible_paths)
        suffix = f"  ·  {hidden:,} hidden by this tab or filters" if hidden else ""
        affected = sum(any(record.path in self.selected for record in group.files) for group in self.groups)
        group_noun = "group" if affected == 1 else "groups"
        self.selection_label.setText(
            f"{len(self.selected):,} {noun} selected  ·  {affected:,} {group_noun}  ·  {format_bytes(amount)}{suffix}")
        folder_noun = "folder" if len(self.selected_duplicate_folders) == 1 else "folders"
        self.duplicate_folder_selection_label.setText(
            f"{len(self.selected_duplicate_folders):,} {folder_noun} selected for recycling")

    def apply_result_filters(self):
        sizes = []
        for field in (self.min_size_filter, self.max_size_filter):
            value = field.text().strip()
            if not value:
                sizes.append(None)
            elif re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)", value, flags=re.ASCII):
                sizes.append(Decimal(value) * (1024 ** self.size_unit.currentIndex()))
            else:
                self.filter_error.setText("Enter a nonnegative size, such as 0, 1.5, or 250. Filters were not changed.")
                self.filter_error.show()
                return
        minimum, maximum = sizes
        if minimum is not None and maximum is not None and minimum > maximum:
            self.filter_error.setText("Minimum size must not exceed maximum size. Filters were not changed.")
            self.filter_error.show()
            return
        try:
            text_filters = {
                "filename": parse_filter_terms(self.filename_filter.text()),
                "file_path": parse_filter_terms(self.file_path_filter.text(), path=True),
                "folder_path": parse_filter_terms(self.folder_path_filter.text(), path=True),
            }
        except ValueError as exc:
            self.filter_error.setText(
                f"Invalid text filter: {exc} Use comma-separated terms and -term to exclude. Filters were not changed.")
            self.filter_error.show()
            return
        self.result_filters = {
            **{key: terms for key, terms in text_filters.items() if terms},
            "min_size": minimum, "max_size": maximum,
        }
        self.result_filters = {key: value for key, value in self.result_filters.items()
                               if value is not None}
        self.filter_error.hide()
        self.filter_results()

    def clear_result_filters(self):
        for field in (self.filename_filter, self.file_path_filter, self.folder_path_filter,
                      self.min_size_filter, self.max_size_filter):
            field.clear()
        self.result_filters = {}
        self.filter_error.hide()
        self.filter_results()

    def group_matches_filters(self, group):
        filters = self.result_filters
        if not filters:
            return True

        def text_matches(key, value):
            return all(query not in value if excluded else query in value
                       for query, excluded in filters.get(key, ()))

        return any(
            text_matches("filename", record.path.name.casefold())
            and text_matches("file_path", record.path.as_posix().casefold())
            and text_matches("folder_path", record.path.parent.as_posix().casefold())
            and (filters.get("min_size") is None or record.total_size >= filters["min_size"])
            and (filters.get("max_size") is None or record.total_size <= filters["max_size"])
            for record in group.files)

    def filter_results(self):
        self.thumbnails.dismiss()
        category = self.type_tabs.tabText(self.type_tabs.currentIndex())
        selected_view = category == "Selected"
        self.visible_paths.clear()
        visible_groups = 0
        visible_savings = 0
        possible = False
        self.tree.setUpdatesEnabled(False)
        try:
            for index in range(self.tree.topLevelItemCount()):
                parent = self.tree.topLevelItem(index)
                group = parent.data(0, Qt.ItemDataRole.UserRole)
                group_matches = self.group_matches_filters(group)
                group_selected = selected_view and any(
                    parent.child(child_index).data(0, Qt.ItemDataRole.UserRole).path in self.selected
                    for child_index in range(parent.childCount())
                )
                visible = 0
                for child_index in range(parent.childCount()):
                    child = parent.child(child_index)
                    record = child.data(0, Qt.ItemDataRole.UserRole)
                    matches = group_matches and (category == "All" or group_selected or file_type(record.path) == category)
                    child.setHidden(not matches)
                    if matches:
                        self.visible_paths.add(record.path)
                        visible += 1
                parent.setHidden(visible == 0)
                if visible:
                    visible_groups += 1
                    visible_savings += group.extra_bytes
                    possible = possible or not group.contents_verified
            current = self.tree.currentItem()
            if current is not None and (current.isHidden() or
                                       (current.parent() is not None and current.parent().isHidden())):
                self.tree.setCurrentItem(None)
                self.tree.clearSelection()
        finally:
            self.tree.setUpdatesEnabled(True)
        if self.summary_notice is not None:
            self.summary.setText(self.summary_notice)
        else:
            noun = "group" if visible_groups == 1 else "groups"
            kind = "possible match" if possible else "duplicate"
            heading = f"{visible_groups:,} {kind} {noun}"
            if self.summary_context == "loaded":
                heading = "Loaded " + heading
            elif self.summary_context == "remaining":
                heading += " remaining"
            saving_label = "estimated savings · contents not verified" if possible else "potentially recoverable"
            self.summary.setText(f"{heading}  ·  {format_bytes(visible_savings)} {saving_label}")
        if self.result_filters:
            self.filter_hint.clear()
        elif selected_view and self.selected:
            noun = "group" if visible_groups == 1 else "groups"
            checked = "file" if len(self.selected) == 1 else "files"
            self.filter_hint.setText(
                f"{visible_groups:,} duplicate {noun} containing {len(self.selected):,} checked {checked}. "
                "Unchecked copies are shown so you can see what will remain.")
        elif selected_view:
            self.filter_hint.setText("No files are checked. Choose files in any tab to show them here.")
        elif self.visible_paths:
            self.filter_hint.setText(f"{len(self.visible_paths):,} of {len(self.records):,} duplicate files shown by extension. "
                                     "Group totals and savings include copies in other tabs.")
        else:
            self.filter_hint.setText(f"No duplicate files in {category}. Choose All to see other file types.")
        self.filter_hint.setVisible(not self.result_filters and category != "All" and bool(self.groups))
        self.update_group_highlights()
        self.update_actions()
        self.update_details()

    def refresh_selection_view(self):
        if self.type_tabs.tabText(self.type_tabs.currentIndex()) == "Selected":
            self.filter_results()
        else:
            self.update_group_highlights()
            self.update_actions()
            self.update_details()

    def update_group_highlights(self):
        dark = self.dark_mode.isChecked()
        highlight = QBrush(QColor("#3b3520" if dark else "#fff1c2"))
        all_highlight = QBrush(QColor("#53252d" if dark else "#fee2e2"))
        checked_icon = control_icon("checked", dark)
        all_icon = control_icon("all_checked", dark, "#f87171" if dark else "#b91c1c")
        labels = []
        for index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(index)
            group = parent.data(0, Qt.ItemDataRole.UserRole)
            checked = sum(record.path in self.selected for record in group.files)
            all_checked = checked > 0 and checked == len(group.files)
            background = all_highlight if all_checked else highlight if checked else QBrush()
            for column in range(self.tree.columnCount()):
                parent.setBackground(column, background)
            parent.setIcon(0, all_icon if all_checked else checked_icon if checked else QIcon())
            description = f"{checked} of {len(group.files)} files selected for recycling" if checked else ""
            if all_checked:
                description += ". All copies selected; no listed copy will remain."
            parent.setData(0, Qt.ItemDataRole.AccessibleDescriptionRole, description)
            parent.setToolTip(0, f"{group.metadata_status}\nScan SHA-256: {group.digest or 'Not calculated'}"
                             + (f"\n{description}" if checked else ""))
            visible = sum(not parent.child(index).isHidden() for index in range(parent.childCount()))
            parts = [f"{visible} of {len(group.files)} files shown"] if visible != len(group.files) else []
            if checked:
                parts.append(f"{checked}/{len(group.files)} selected · {len(group.files) - checked} remaining")
                if all_checked:
                    parts.append("All copies selected")
            label = " · ".join(parts)
            if parent.text(1) != label:
                labels.append((parent, label))
        # Batch label updates; group ordering uses immutable scan-time keys, not these labels.
        if labels:
            sorting = self.tree.isSortingEnabled()
            self.tree.setSortingEnabled(False)
            try:
                for parent, label in labels:
                    parent.setText(1, label)
            finally:
                self.tree.setSortingEnabled(sorting)

    def start_job(self, job, handler, error_handler=None, *, scan_run=False):
        self.thumbnails.clear()
        self.comparison_preview.clear()
        worker = Worker(job, self, scan_run=scan_run)
        if scan_run:
            self.scan_worker = worker
            worker.mode_started.connect(self.on_mode_started)
            worker.mode_finished.connect(self.on_mode_finished)
            worker.progress.connect(self.on_scan_progress)
            worker.failed.connect(self.on_scan_failure)
            worker.finished.connect(self.scan_job_finished)
            worker.paused.connect(self.on_scan_paused)
        else:
            self.worker = worker
            worker.progress.connect(self.on_progress)
            worker.failed.connect(error_handler or self.on_failure)
            worker.finished.connect(self.job_finished)
        worker.outcome.connect(handler)
        self.update_actions()
        worker.start()

    def start_scan(self):
        modes, types, criteria = self.selected_modes(), self.selected_file_types(), self.search_criteria()
        if (self.worker is not None or self.scan_worker is not None or not self.folders.count() or not modes
                or ("duplicates" in modes and not criteria.enabled)
                or (any(mode != "folders" for mode in modes) and types == ())):
            return
        roots = tuple(self.folders.item(i).text() for i in range(self.folders.count()))
        recursive, exclusions = self.recursive.isChecked(), self.excluded_folder_paths()
        preset = self.similarity_preset.currentText()
        self._incremental_run = False
        self._scan_baselines = {}
        self._baseline_settings = None
        self._scan_recycled = []
        self._scan_cache_valid = True
        self._scan_settings = self.current_scan_settings()
        self._scan_paused = False
        self._scan_modes_running = modes
        self.scan_mode_states = {mode: "Waiting" for mode in modes}
        self._scan_messages = {}
        self._last_scan_progress = None
        self.update_scan_tab_titles()
        self.similar_tab.clear_preview()
        if "duplicates" in modes:
            self.session_available = False
            self.session_path = None
            self.scan_file_count = self.scan_total_bytes = 0
            self.summary_notice = "Scanning your folders…"
            self.set_groups([])
            self.issues = []
            self.empty.setText("Comparing the selected file types using your duplicate criteria. No files will be changed.")
        if "similarity" in modes:
            self.similar_tab.prepare_scan()
        if "folders" in modes:
            self.set_duplicate_folders([])
            self.duplicate_folder_issues = []
            self.duplicate_folder_summary.setText("Waiting for duplicate-folder scan…")
            self.duplicate_folder_hint.setText("Every file is compared, regardless of file-type or duplicate-file criteria. Excluded content disqualifies its parent folders.")
        pages = {"duplicates": self.duplicate_page, "similarity": self.similar_tab, "folders": self.folder_page}
        self.workflow_tabs.setCurrentWidget(pages[modes[0]])
        self.status.setText("Starting selected scans…")
        self.start_job(lambda **kwargs: run_selected_scans(roots, recursive, modes=modes,
            excluded_folders=exclusions, file_types=types, criteria=criteria, preset=preset, **kwargs),
            self.on_scan_run, scan_run=True)

    @staticmethod
    def normalized_roots(roots):
        return {Path(os.path.normcase(os.path.abspath(root))) for root in roots}

    def added_scan_scope(self):
        if self._baseline_settings is None or not self._scan_baselines:
            return (), "Complete a fresh scan first. Loaded sessions do not include the inventory needed for added-folder scans."
        current = self.current_scan_settings()
        old = self._baseline_settings
        # Root order is irrelevant. All criteria and mode choices must stay fixed.
        if (current[1] != old[1] or self.normalized_roots(current[2]) != self.normalized_roots(old[2])
                or current[3:] != old[3:]):
            return (), "Scan settings changed. Use Scan for a fresh scan, or restore the previous settings."
        old_roots, roots = self.normalized_roots(old[0]), self.normalized_roots(current[0])
        if not old_roots.issubset(roots):
            return (), "Previously scanned roots were removed. Use Scan for a fresh scan."
        added = roots - old_roots
        if any(new in root.parents for new in added for root in old_roots):
            return (), "An added folder contains a previously scanned root. Use Scan for that overlapping scope."
        if current[1]:
            added = {path for path in added if not any(root in path.parents for root in old_roots)}
        if not added:
            return (), "Add a folder outside the locations already scanned."
        return tuple(str(path) for path in sorted(added)), ""

    def start_added_scan(self):
        roots, reason = self.added_scan_scope()
        if self.worker is not None or self.scan_worker is not None or reason or not roots:
            return
        settings = self.current_scan_settings()
        self._scan_settings = settings
        self._incremental_states = dict(self.scan_mode_states)
        self._incremental_run = True
        self._scan_paused = False
        modes = settings[3]
        self._scan_modes_running = modes
        self.scan_mode_states = {mode: "Waiting" for mode in modes}
        self._scan_messages = {}
        self._last_scan_progress = None
        previous = dict(self._scan_baselines)
        self.update_scan_tab_titles()
        self.status.setText(f"Scanning {len(roots):,} added locations; previous results are retained…")
        self.start_job(lambda **kwargs: run_selected_scans(roots, settings[1], modes=modes,
            excluded_folders=settings[2], file_types=settings[4], criteria=settings[5], preset=settings[6],
            previous=previous, **kwargs), self.on_scan_run, scan_run=True)

    def current_scan_settings(self):
        return (tuple(self.folders.item(i).text() for i in range(self.folders.count())),
                self.recursive.isChecked(), self.excluded_folder_paths(), self.selected_modes(),
                self.selected_file_types(), self.search_criteria(), self.similarity_preset.currentText())

    def toggle_scan_pause(self):
        if self.scan_worker is None or self.worker is not None or self.scan_worker.cancel_event.is_set():
            return
        control = self.scan_worker.cancel_event
        if not self._scan_paused:
            self._scan_paused = True
            control.pause()
            self.status.setText("Pausing after the current read or media decode… You can edit scan locations and criteria.")
        elif self.current_scan_settings() != self._scan_settings:
            modes, types, criteria = self.selected_modes(), self.selected_file_types(), self.search_criteria()
            if (not self.folders.count() or not modes or ("duplicates" in modes and not criteria.enabled)
                    or (any(mode != "folders" for mode in modes) and types == ())):
                QMessageBox.warning(self, "Cannot restart scan", "Choose folders, scan modes, and valid search criteria first.")
                return
            self._restart_scan = True
            self._scan_paused = False
            control.set()
            self.status.setText("Restarting with the updated scan locations and criteria…")
        else:
            self._scan_paused = False
            control.resume()
            if self._last_scan_progress:
                self.on_progress(self._last_scan_progress)
            else:
                self.status.setText("Resuming scan…")
        self.update_actions()

    def on_scan_paused(self):
        if self._scan_paused and self.scan_worker is not None:
            self.status.setText("Scan paused. Add disks or change criteria, then Resume. Changed settings restart the scan.")

    def update_scan_tab_titles(self):
        pages = {"duplicates": self.duplicate_page, "similarity": self.similar_tab, "folders": self.folder_page}
        for mode, page in pages.items():
            state = self.scan_mode_states.get(mode)
            title = SCAN_MODES[mode] + (f" · {state}" if state else "")
            self.workflow_tabs.setTabText(self.workflow_tabs.indexOf(page), title)

    def on_mode_started(self, mode):
        self.scan_mode_states[mode] = "Scanning"
        self.update_scan_tab_titles()
        self.on_scan_progress(Progress(f"{SCAN_MODES[mode]} · Starting", 0, 0, ""))

    def on_mode_finished(self, mode, result, error=""):
        # Results are delivered once. Final scan completion must not reset checks
        # or restore files already recycled while a later mode was scanning.
        if mode in self._scan_messages:
            return
        if self._incremental_run:
            self._scan_messages[mode] = f"{SCAN_MODES[mode]}: {'failed' if error else 'cancelled' if result is None or result.cancelled else 'ready'}"
            self.scan_mode_states[mode] = "Failed" if error else "Cancelled" if result is None or result.cancelled else "Ready"
            self.update_scan_tab_titles()
            self.update_actions()
            return
        handlers = {"duplicates": self.on_scan, "similarity": self.similar_tab.on_result,
                    "folders": self.on_duplicate_folder_scan}
        previous_status = self.status.text()
        if result is not None:
            handlers[mode](result)
            state = "Cancelled" if result.cancelled else "Ready"
            count = len(result.folders) if mode == "folders" else len(result.groups)
            message = "cancelled" if result.cancelled else f"{count} {'folders' if mode == 'folders' else 'groups'}"
        else:
            state = "Failed" if error else "Cancelled"
            message = "Scan failed: " + error if error else "Not run — scan cancelled"
            if mode == "duplicates":
                self.summary_notice = message
                self.set_groups([])
            elif mode == "similarity":
                self.similar_tab.summary.setText(message)
                self.similar_tab.status.setText(message)
            else:
                self.duplicate_folder_summary.setText(message)
        self._scan_messages[mode] = f"{SCAN_MODES[mode]}: {message}"
        self.scan_mode_states[mode] = state
        if self.worker is not None:
            self.status.setText(previous_status)
        self.update_scan_tab_titles()
        self.update_actions()

    def on_scan_progress(self, progress):
        self._last_scan_progress = progress
        if self.worker is None and not self._scan_paused:
            self.on_progress(progress)

    def on_scan_run(self, outcome):
        if self._incremental_run:
            success = (not outcome.cancelled and not outcome.errors and
                       set(outcome.results) == set(self._scan_modes_running) and
                       all(not result.cancelled for result in outcome.results.values()))
            if success:
                selected_files, selected_folders = set(self.selected), set(self.selected_duplicate_folders)
                for mode, result in outcome.results.items():
                    if mode == "duplicates":
                        self.on_scan(result)
                    elif mode == "folders":
                        self.on_duplicate_folder_scan(result)
                    else:
                        self.similar_tab.on_result(result)
                self.restore_scan_selection(selected_files, selected_folders)
                self._scan_baselines = dict(outcome.results)
                self._baseline_settings = self._scan_settings
                self.scan_mode_states = {mode: "Ready" for mode in self._scan_modes_running}
                self.status.setText("Added-folder scan complete. Results combined; existing checks retained and new items unchecked.")
            else:
                self.scan_mode_states = dict(self._incremental_states)
                errors = " · ".join(outcome.errors.values())
                self.status.setText("Added-folder scan cancelled; previous results and checks retained." if outcome.cancelled
                                    else "Added-folder scan failed; previous results and checks retained. " + errors)
            self.update_scan_tab_titles()
            self.update_actions()
            return
        for mode in self._scan_modes_running:
            self.on_mode_finished(mode, outcome.results.get(mode), outcome.errors.get(mode, ""))
        if (self._scan_cache_valid and not outcome.cancelled and not outcome.errors and
                set(outcome.results) == set(self._scan_modes_running)):
            self._scan_baselines = dict(outcome.results)
            self._baseline_settings = self._scan_settings
            for paths, folders in self._scan_recycled:
                self.prune_scan_baselines(paths, folders=folders)
        state = "Cancelled" if outcome.cancelled else "Finished with errors" if outcome.errors else "Scan complete"
        if self.worker is None:
            self.status.setText(state + " · " + " · ".join(self._scan_messages.values()))

    def on_scan_failure(self, message):
        if self._incremental_run:
            self.scan_mode_states = dict(self._incremental_states)
            self.status.setText("Added-folder scan failed; previous results and checks retained. " + message)
            self.update_scan_tab_titles()
            return
        for mode in self._scan_modes_running:
            self.on_mode_finished(mode, None, message)
        if self.worker is None:
            self.status.setText("Scan stopped · " + message)

    def scan_job_finished(self):
        worker = self.scan_worker
        self.scan_worker = None
        self._incremental_run = False
        self._scan_paused = False
        if worker:
            worker.deleteLater()
        if self.worker is None:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self.current_path.clear()
        self.update_actions()
        self.restart_scan_if_ready()

    def restart_scan_if_ready(self):
        if self._restart_scan and self.scan_worker is None and self.worker is None:
            self._restart_scan = False
            self.start_scan()

    def restore_scan_selection(self, files, folders):
        self._changing_checks = True
        self._changing_empty_checks = True
        try:
            self.selected = files.intersection(self.records)
            self.selected_duplicate_folders = folders.intersection(record.path for record in self.duplicate_folders)
            for item in self.file_items():
                item.setCheckState(0, Qt.CheckState.Checked if item.data(0, Qt.ItemDataRole.UserRole).path in self.selected else Qt.CheckState.Unchecked)
            for item in self.duplicate_folder_items():
                item.setCheckState(0, Qt.CheckState.Checked if item.data(0, Qt.ItemDataRole.UserRole).path in self.selected_duplicate_folders else Qt.CheckState.Unchecked)
        finally:
            self._changing_checks = False
            self._changing_empty_checks = False
        self.refresh_selection_view()

    def prune_scan_baselines(self, paths, *, folders=False):
        paths = set(paths)
        if not paths:
            return
        def removed(path):
            return path in paths or (folders and any(root in path.parents for root in paths))

        for mode, result in tuple(self._scan_baselines.items()):
            if mode == "duplicates":
                index = {path: item for path, item in result.index.items() if not removed(path)}
                groups = []
                for group in result.groups:
                    files = tuple(record for record in group.files if not removed(record.path))
                    if len(files) > 1:
                        groups.append(DuplicateGroup(files, group.digest, group.byte_verified))
                self._scan_baselines[mode] = replace(result, index=index, groups=groups,
                    file_count=len(index), total_bytes=sum(item.record.total_size for item in index.values()))
            elif mode == "similarity":
                images = tuple(image for image in result.images if not removed(image.record.path))
                videos = tuple(video for video in result.videos if not removed(video.record.path))
                groups = []
                for group in result.groups:
                    if removed(group.reference.record.path):
                        continue
                    matches = tuple((item, distance) for item, distance in group.matches if not removed(item.record.path))
                    if matches:
                        groups.append(type(group)(group.reference, matches))
                self._scan_baselines[mode] = replace(result, images=images, videos=videos, groups=groups,
                    image_count=len(images), video_count=len(videos), compared_count=len(images) + len(videos),
                    videos_compared=len(videos))
            else:
                index = {path: tree for path, tree in result.index.items()
                         if not any(removed(file.path) for file in tree.files)
                         and not (folders and any(path == root or path in root.parents or root in path.parents for root in paths))}
                groups = []
                for group in result.groups:
                    copies = tuple(tree for tree in group.folders if tree.path in index)
                    if len(copies) > 1 or (copies and copies[0].empty):
                        groups.append(FolderGroup(copies))
                known_files = {file.path for tree in index.values() for file in tree.files}
                self._scan_baselines[mode] = replace(result, index=index, groups=groups,
                    file_ids={path: value for path, value in result.file_ids.items() if path in known_files})

    def record_scan_cleanup(self, paths, *, folders=False):
        self.prune_scan_baselines(paths, folders=folders)
        if self.scan_worker is not None and not self._incremental_run:
            self._scan_recycled.append((tuple(paths), folders))

    def save_session(self):
        if self.mode_busy("duplicates") or not self.session_available:
            return
        if self.session_path is None:
            default = Path.home() / f"Duplicate Cleaner {datetime.now():%Y-%m-%d %H%M}.dupsession"
        else:
            default = self.session_path
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save Duplicate Cleaner session", str(default),
            "Duplicate Cleaner sessions (*.dupsession)")
        if not filename:
            return
        path = Path(filename)
        if not path.suffix:
            path = path.with_suffix(".dupsession")
        data = SessionData(
            tuple(self.groups), frozenset(self.selected),
            tuple(self.folders.item(index).text() for index in range(self.folders.count())),
            self.recursive.isChecked(), tuple(self.issues), self.scan_file_count,
            self.scan_total_bytes, self.type_tabs.tabText(self.type_tabs.currentIndex()),
            excluded_folders=self.excluded_folder_paths(),
        )
        self.status.setText("Preparing session file…")
        self.start_job(
            lambda **kwargs: save_session_file(path, data, **kwargs), self.on_session_saved,
            lambda message: self.on_session_failure("Could not save session", message),
        )

    def on_session_saved(self, result: SaveResult):
        if result.cancelled:
            self.status.setText("Session save cancelled. Existing session files were not changed.")
            return
        self.session_path = result.path
        self.status.setText(f"Session saved · {result.path}")

    def load_session(self):
        if self.worker is not None or self.scan_worker is not None:
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "Load Duplicate Cleaner session", str(Path.home()),
            "Duplicate Cleaner sessions (*.dupsession);;All files (*.*)")
        if not filename:
            return
        path = Path(filename)
        self.status.setText("Reading and validating saved session…")
        self.start_job(
            lambda **kwargs: load_session_file(path, **kwargs), self.on_session_loaded,
            lambda message: self.on_session_failure("Could not load session", message),
        )

    def on_session_loaded(self, result: LoadResult):
        if result.cancelled:
            self.status.setText("Session load cancelled. Current results were not changed.")
            return
        data = result.data
        if data is None:
            self.on_session_failure("Could not load session", "The session did not contain usable data.")
            return
        restorable = len(data.selected)
        saved_when = datetime.fromisoformat(data.saved_at).strftime("%Y-%m-%d %H:%M:%S")
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Load saved session")
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setText(f"Load the duplicate results saved on {saved_when}?")
        information = (
            "Loading replaces the results and selections currently shown. Files were checked for "
            "identity and timestamps; contents will still be verified again before recycling."
        )
        if result.saved_selection_count:
            information = (
                f"This session saved {result.saved_selection_count:,} checked file(s). "
                f"{restorable:,} can be restored and {result.dropped_selected:,} cannot be restored.\n\n"
                + information
            )
        dialog.setInformativeText(information)
        without = dialog.addButton("Load with nothing checked", QMessageBox.ButtonRole.AcceptRole)
        restore = None
        if restorable:
            restore_noun = "file" if restorable == 1 else "files"
            restore = dialog.addButton(f"Restore {restorable:,} checked {restore_noun}",
                                       QMessageBox.ButtonRole.ActionRole)
        cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(without)
        dialog.exec()
        clicked = dialog.clickedButton()
        choices = tuple(button for button in (without, restore) if button is not None)
        if clicked == cancel or clicked not in choices:
            self.status.setText("Session load cancelled. Current results were not changed.")
            return
        restore_checks = restore is not None and clicked == restore
        self._scan_baselines = {}
        self._baseline_settings = None

        self.workflow_tabs.setCurrentWidget(self.duplicate_page)
        self.issues = list(data.issues) + list(result.validation_issues)
        self.scan_file_count = data.file_count
        self.scan_total_bytes = data.total_bytes
        self.session_available = True
        self.session_path = result.path
        self.folders.clear()
        self.excluded_folders.clear()
        for root in data.roots:
            self.add_folder_path(root)
        for folder in data.excluded_folders:
            self.add_excluded_folder_path(folder)
        self.recursive.setChecked(data.recursive)
        self.summary_notice = None
        self.summary_context = "loaded"
        self.set_groups(data.groups)
        if restore_checks:
            self._changing_checks = True
            try:
                for item in self.file_items():
                    record = item.data(0, Qt.ItemDataRole.UserRole)
                    if record.path in data.selected:
                        item.setCheckState(0, Qt.CheckState.Checked)
                self.selected.update(data.selected)
            finally:
                self._changing_checks = False
        tab_name = data.active_tab
        if tab_name == "Selected groups":
            tab_name = "Selected"
        if tab_name == "Selected" and not restore_checks:
            tab_name = "All"
        tab_index = next((index for index in range(self.type_tabs.count())
                          if self.type_tabs.tabText(index) == tab_name), 0)
        self.type_tabs.setCurrentIndex(tab_index)
        self.filter_results()
        self.empty.setText(
            "No usable duplicate groups remain in this session. Changed or missing files are listed under errors.")
        action = f"{len(data.selected):,} saved checks restored" if restore_checks else "all files left unchecked"
        self.status.setText(
            f"Session loaded · {action} · {result.dropped_files:,} changed or missing files · "
            f"{result.dropped_groups:,} incomplete groups removed")

    def on_session_failure(self, title, message):
        self.status.setText(message)
        QMessageBox.critical(self, title, message + "\n\nCurrent results were not changed.")

    @staticmethod
    def group_file_size(group):
        sizes = [record.total_size for record in group.files]
        low, high = min(sizes), max(sizes)
        return format_bytes(low) if low == high else f"{format_bytes(low)} – {format_bytes(high)}"

    def set_groups(self, groups):
        self.thumbnails.clear()
        self.comparison_preview.clear()
        self.details_text.clear()
        self.details_panel.hide()
        self.groups = list(groups)
        groups = self.groups
        self.selected.clear()
        self.records = {record.path: record for group in groups for record in group.files}
        self._changing_checks = True
        self.tree.setSortingEnabled(False)
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            for number, group in enumerate(groups, 1):
                parent = ResultItem(self.tree, [
                    f"Group {number} · {group.metadata_status} · {len(group.files)} files", "",
                    self.group_file_size(group), "",
                ])
                # Status text changes when checking files. Keep it out of sorting, and break
                # equal-key ties by the scan's group number so selection cannot reshuffle rows.
                parent.group_sort_keys = tuple((value, number) for value in (
                    parent.text(0).casefold(), min(str(record.path).casefold() for record in group.files),
                    min(record.total_size for record in group.files), ""))
                parent.setData(2, Qt.ItemDataRole.UserRole, min(record.total_size for record in group.files))
                parent.setData(0, Qt.ItemDataRole.UserRole, group)
                parent.setToolTip(0, f"{group.metadata_status}\nScan SHA-256: {group.digest or 'Not calculated'}")
                font = QFont()
                font.setBold(True)
                parent.setFont(0, font)
                for record in group.files:
                    modified = datetime.fromtimestamp(record.modified_ns / 1_000_000_000)
                    child = ResultItem(parent, [record.path.name, str(record.path),
                                               format_bytes(record.total_size), modified.strftime("%Y-%m-%d %H:%M:%S")])
                    child.setData(0, Qt.ItemDataRole.UserRole, record)
                    child.setData(2, Qt.ItemDataRole.UserRole, record.total_size)
                    child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.CheckState.Unchecked)
                    child.setToolTip(0, str(record.path))
                    child.setToolTip(1, str(record.path))
                parent.setExpanded(len(groups) <= 100)
        finally:
            self.tree.setSortingEnabled(True)
            self.tree.setUpdatesEnabled(True)
            self._changing_checks = False
        self.empty.setVisible(not groups)
        self.filter_results()

    def item_changed(self, item, column):
        if self._changing_checks or column != 0 or item.parent() is None:
            return
        record = item.data(0, Qt.ItemDataRole.UserRole)
        checked = item.checkState(0) == Qt.CheckState.Checked
        if checked:
            self.selected.add(record.path)
        else:
            self.selected.discard(record.path)
        self.refresh_selection_view()

    def preview_selection_changed(self, path, checked):
        if self.mode_busy("duplicates"):
            return
        for item in self.file_items():
            if item.data(0, Qt.ItemDataRole.UserRole).path == path:
                item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                break

    def file_items(self):
        for index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(index)
            for child_index in range(parent.childCount()):
                yield parent.child(child_index)

    def select_folder_duplicates(self, item=None):
        if self.mode_busy("duplicates"):
            return
        if item is None:
            item = self.tree.currentItem()
        if item is None or item.parent() is None:
            return
        record = item.data(0, Qt.ItemDataRole.UserRole)
        folder = record.path.parent
        folder_key = os.path.normcase(os.path.abspath(str(folder)))
        matches = []
        for child in self.file_items():
            child_record = child.data(0, Qt.ItemDataRole.UserRole)
            child_folder = os.path.normcase(os.path.abspath(str(child_record.path.parent)))
            if child_folder == folder_key:
                matches.append(child)
        paths = {child.data(0, Qt.ItemDataRole.UserRole).path for child in matches}
        selected = self.selected | paths
        kept = set()
        for group in self.groups:
            group_paths = {record.path for record in group.files}
            in_folder = group_paths & paths
            if in_folder and group_paths <= selected:
                # Prefer an unchecked copy in the target folder. A repeat click then preserves
                # the sole remaining copy without changing manual selections in other folders.
                candidates = in_folder - self.selected or in_folder
                keeper = random.choice(sorted(candidates))
                selected.remove(keeper)
                kept.add(keeper)
        newly_selected = len(selected - self.selected)
        paths &= selected
        self._changing_checks = True
        try:
            self.selected = selected
            for child in matches:
                path = child.data(0, Qt.ItemDataRole.UserRole).path
                child.setCheckState(0, Qt.CheckState.Checked if path in selected else Qt.CheckState.Unchecked)
        finally:
            self._changing_checks = False
        self.refresh_selection_view()
        hidden = len(paths - self.visible_paths)
        noun = "file" if len(paths) == 1 else "files"
        message = (f"Selected {newly_selected:,} new · {len(paths):,} duplicate {noun} selected in {folder}")
        if kept:
            message += f" · Kept one copy in {len(kept):,} group(s)"
        if hidden:
            message += f" · {hidden:,} hidden by this tab"
        self.status.setText(message)

    def show_result_menu(self, position):
        item = self.tree.itemAt(position)
        if item is None or item.parent() is None:
            return
        self.tree.setCurrentItem(item)
        menu = QMenu(self)
        open_location = menu.addAction("Open file location")
        open_location.triggered.connect(self.open_folder)
        menu.addSeparator()
        select_folder = menu.addAction("Select this folder's duplicates (keep one copy)")
        select_folder.setEnabled(not self.mode_busy("duplicates"))
        select_folder.triggered.connect(lambda: self.select_folder_duplicates(item))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def clear_selection(self):
        self._changing_checks = True
        for item in self.file_items():
            item.setCheckState(0, Qt.CheckState.Unchecked)
        self._changing_checks = False
        self.selected.clear()
        self.refresh_selection_view()

    def update_details(self):
        self.preview_toggle.setText("Hide preview" if self.preview_toggle.isChecked() else "Show preview")
        item = self.tree.currentItem()
        if (self._changing_checks or item is None or not item.isSelected()
                or self.workflow_tabs.currentWidget() is not self.duplicate_page):
            self.details_text.clear()
            self.comparison_preview.clear()
            self.details_panel.hide()
            self.results_splitter.setMinimumHeight(160)
            return
        is_file = item.parent() is not None
        group = (item.parent() if is_file else item).data(0, Qt.ItemDataRole.UserRole)
        record = item.data(0, Qt.ItemDataRole.UserRole) if is_file else group.files[0]
        if self.preview_toggle.isChecked() and not self.mode_busy("duplicates"):
            self.comparison_preview.set_group(group, record if is_file else None, self.selected)
        else:
            self.comparison_preview.clear()
        modified = datetime.fromtimestamp(record.modified_ns / 1_000_000_000)
        info = QFileInfo(str(record.path))
        created = info.birthTime()
        selection = "Selected for recycling" if record.path in self.selected else "Unchecked — will remain"
        try:
            ensure_current(record)
            state = "Matches the scanned file identity and timestamps"
        except OSError as exc:
            state = f"Changed or unavailable — scan again. {exc}"
        lines = [
            f"Name: {record.path.name}",
            f"Type: {record.path.suffix.upper()[1:] + ' file' if record.path.suffix else 'No extension'}",
            f"Full path: {record.path}",
            f"Size at scan: {format_bytes(record.total_size)} ({record.total_size:,} bytes)",
            f"Extra NTFS streams: {len(record.streams)} ({record.total_size - record.size:,} bytes)",
            f"Modified at scan: {modified:%Y-%m-%d %H:%M:%S}",
            f"Created: {created.toString('yyyy-MM-dd HH:mm:ss') if created.isValid() else 'Unavailable'}",
            (f"Duplicate group: {len(group.files)} copies with main contents verified byte for byte at scan time"
             if group.contents_verified else f"Possible match group: {len(group.files)} files; contents not verified"),
            f"SHA-256 at scan: {group.digest or 'Not calculated'}",
            f"Selection: {selection}",
            f"Current status: {state}",
        ]
        lines.extend(("", group.metadata_details))
        self.details_text.setPlainText("\n".join(lines))
        was_hidden = self.details_panel.isHidden()
        self.details_panel.setVisible(self.preview_toggle.isChecked())
        self.results_splitter.setMinimumHeight(
            self.details_panel.minimumSizeHint().height() if self.preview_toggle.isChecked() else 160)
        if was_hidden and self.preview_toggle.isChecked():
            width = self.results_splitter.width()
            self.results_splitter.setSizes([max(290, width * 2 // 5), max(380, width * 3 // 5)])

    def open_file(self, item, column=0):
        if self.mode_busy("duplicates") or item is None or item.parent() is None:
            return
        self.thumbnails.dismiss()
        record = item.data(0, Qt.ItemDataRole.UserRole)
        try:
            ensure_current(record)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot open this file", f"The file changed or is unavailable. Scan again.\n\n{exc}")
            self.update_details()
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.path))):
            QMessageBox.warning(self, "Could not open file",
                                f"Windows could not open this file. Check its default app association.\n\n{record.path}")

    def on_progress(self, progress: Progress):
        stage = progress.stage.casefold()
        unit = "frames" if "frames" in stage else "folders" if "folder" in stage else "files"
        self.status.setText(f"{progress.stage}  ·  {progress.completed:,}"
                            + (f" / {progress.total:,} {unit}" if progress.total else f" {unit}")
                            + (f"  ·  {format_bytes(progress.bytes_read)} read"
                               if progress.bytes_read or unit == "files" else ""))
        self.status.setToolTip(progress.path)
        self.current_path.setText(self.current_path.fontMetrics().elidedText(
            progress.path, Qt.TextElideMode.ElideMiddle, max(200, self.width() - 70)
        ))
        self.current_path.setToolTip(progress.path)
        if progress.total:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(min(1000, int(progress.completed / progress.total * 1000)))
        else:
            self.progress_bar.setRange(0, 0)

    def on_scan(self, result: ScanResult):
        self.issues = result.issues
        self.scan_file_count = result.file_count
        self.scan_total_bytes = result.total_bytes
        self.session_available = not result.cancelled
        self.summary_notice = "Scan cancelled — no files changed" if result.cancelled else None
        self.summary_context = ""
        self.set_groups(result.groups)
        if result.cancelled:
            self.empty.setText("Start another scan when you are ready. Partial results are not used for cleanup.")
        else:
            self.empty.setText("No matching groups found. Check skipped files / errors for anything we could not inspect.")
        self.status.setText(f"{'Cancelled' if result.cancelled else 'Scan complete'}  ·  "
                            f"{result.file_count:,} files discovered  ·  {format_bytes(result.total_bytes)}  ·  "
                            f"{len(result.issues):,} skipped / errors")

    def set_duplicate_folders(self, groups):
        self.folder_groups = list(groups)
        self.duplicate_folders = [folder for group in self.folder_groups for folder in group.folders]
        self.selected_duplicate_folders.clear()
        self._changing_empty_checks = True
        self.duplicate_folder_tree.setSortingEnabled(False)
        self.duplicate_folder_tree.setUpdatesEnabled(False)
        try:
            self.duplicate_folder_tree.clear()
            for number, group in enumerate(self.folder_groups, 1):
                parent = ResultItem(self.duplicate_folder_tree, [
                    "Empty folders" if all(folder.empty for folder in group.folders) else f"Group {number} · {len(group.folders)} folders",
                    "", self.group_folder_size(group), str(len(group.folders[0].files))])
                parent.group_sort_keys = (parent.text(0), "", group.folders[0].total_size, len(group.folders[0].files))
                parent.setData(0, Qt.ItemDataRole.UserRole, group)
                for record in group.folders:
                    item = ResultItem(parent, [record.path.name, str(record.path),
                                              format_bytes(record.total_size), str(len(record.files))])
                    item.setData(0, Qt.ItemDataRole.UserRole, record)
                    item.setData(2, Qt.ItemDataRole.UserRole, record.total_size)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(0, Qt.CheckState.Unchecked)
                    item.setToolTip(0, str(record.path))
                    item.setToolTip(1, str(record.path))
                parent.setExpanded(True)
        finally:
            self.duplicate_folder_tree.setSortingEnabled(True)
            self.duplicate_folder_tree.setUpdatesEnabled(True)
            self._changing_empty_checks = False
        self.update_actions()

    def duplicate_folder_items(self):
        for index in range(self.duplicate_folder_tree.topLevelItemCount()):
            parent = self.duplicate_folder_tree.topLevelItem(index)
            for child in range(parent.childCount()):
                yield parent.child(child)

    @staticmethod
    def group_folder_size(group):
        sizes = [folder.total_size for folder in group.folders]
        low, high = min(sizes), max(sizes)
        return format_bytes(low) if low == high else f"{format_bytes(low)} – {format_bytes(high)}"

    def duplicate_folder_item_changed(self, item, column):
        if self._changing_empty_checks or column != 0 or item.parent() is None:
            return
        record = item.data(0, Qt.ItemDataRole.UserRole)
        if item.checkState(0) == Qt.CheckState.Checked:
            self.selected_duplicate_folders.add(record.path)
        else:
            self.selected_duplicate_folders.discard(record.path)
        self.update_actions()

    def clear_duplicate_folder_selection(self):
        self._changing_empty_checks = True
        try:
            for item in self.duplicate_folder_items():
                item.setCheckState(0, Qt.CheckState.Unchecked)
        finally:
            self._changing_empty_checks = False
        self.selected_duplicate_folders.clear()
        self.update_actions()

    def on_duplicate_folder_scan(self, result: FolderScanResult):
        self.duplicate_folder_issues = result.issues
        self.set_duplicate_folders(result.groups)
        if result.cancelled:
            self.duplicate_folder_summary.setText("Duplicate-folder scan cancelled — no folders changed")
            self.duplicate_folder_hint.setText(
                "Start another duplicate-folder scan when ready. Partial results are not used for cleanup.")
        else:
            noun = "folder" if len(result.folders) == 1 else "folders"
            self.duplicate_folder_summary.setText(
                f"{len(result.folders):,} duplicate / empty {noun} found · nothing selected automatically")
            self.duplicate_folder_hint.setText(
                "No duplicate or empty folders found." if not result.folders else
                "Review full paths and check only the folders you want to recycle. "
                "Rescan after cleanup to discover parent folders that have become empty.")
        self.status.setText(
            f"{'Cancelled' if result.cancelled else 'Duplicate-folder scan complete'}  ·  "
            f"{result.folder_count:,} folders inspected  ·  {len(result.issues):,} skipped / errors")

    def confirm_duplicate_folder_recycle(self):
        if self.mode_busy("folders") or not self.selected_duplicate_folders:
            return
        selected = frozenset(self.selected_duplicate_folders)
        groups = tuple(self.folder_groups)
        if any(first != second and (first in second.parents or second in first.parents)
               for first in selected for second in selected):
            QMessageBox.warning(self, "Overlapping folder selection", "Select either a parent folder or its descendants, not both.")
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Confirm duplicate-folder recycling")
        dialog.setIcon(QMessageBox.Icon.Warning)
        noun = "folder" if len(selected) == 1 else "folders"
        dialog.setText(f"Send {len(selected):,} selected {noun} and everything inside them to the Recycle Bin?")
        all_selected = any(group.folders[0].files and all(folder.path in selected for folder in group.folders)
                           for group in groups)
        dialog.setInformativeText(
            ("All copies in at least one group are selected; no listed copy will remain.\n\n" if all_selected else "")
            + "Complete folder contents are rechecked. Changed folders or different NTFS streams are skipped. "
            "Avoid folders being modified or synchronized during cleanup. Nothing is permanently deleted.")
        details = ["SELECTED FOLDERS", *sorted(map(str, selected)), "", "UNCHECKED COMPARISON FOLDERS"]
        details.extend(str(folder.path) for group in groups if any(folder.path in selected for folder in group.folders)
                       for folder in group.folders if folder.path not in selected)
        dialog.setDetailedText("\n".join(details))
        recycle = dialog.addButton("Recycle selected folders", QMessageBox.ButtonRole.AcceptRole)
        cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(cancel)
        dialog.exec()
        if dialog.clickedButton() != recycle:
            return
        self.duplicate_folder_issues = []
        self.start_job(
            lambda **kwargs: recycle_duplicate_folders(groups, selected, **kwargs),
            self.on_duplicate_folders_recycled, self.on_duplicate_folder_failure,
        )

    def on_duplicate_folders_recycled(self, result: RecycleResult):
        self.record_scan_cleanup(result.recycled, folders=True)
        self.duplicate_folder_issues = result.issues
        recycled = set(result.recycled)
        remaining = []
        for group in self.folder_groups:
            folders = tuple(folder for folder in group.folders
                            if not any(folder.path == path or path in folder.path.parents
                                       or folder.path in path.parents for path in recycled))
            if len(folders) > 1 or (folders and folders[0].empty):
                remaining.append(FolderGroup(folders))
        self.set_duplicate_folders(remaining)
        if recycled:
            file_groups = []
            for group in self.groups:
                files = tuple(file for file in group.files
                              if not any(path in file.path.parents for path in recycled))
                if len(files) > 1:
                    file_groups.append(DuplicateGroup(files, group.digest, group.byte_verified))
            self.set_groups(file_groups)
            self.similar_tab.on_result(SimilarResult())
            self.similar_tab.summary.setText("Folder cleanup changed files. Scan again to refresh similar results.")
        noun = "folder" if len(self.duplicate_folders) == 1 else "folders"
        self.duplicate_folder_summary.setText(
            f"{len(self.duplicate_folders):,} listed {noun} remaining · all checks cleared")
        self.duplicate_folder_hint.setText(
            "Rescan to refresh changed folders and discover parent folders that became empty.")
        self.status.setText(
            "Duplicate-folder cleanup cancelled; already recycled folders remain in the Recycle Bin."
            if result.cancelled else
            "Duplicate-folder cleanup complete. No permanent deletion was requested.")
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Duplicate-folder recycling results")
        dialog.setText(
            f"{len(result.recycled):,} folders recycled  ·  {len(result.issues):,} skipped / errors")
        dialog.setInformativeText(
            self.status.text() + "\nUnchecked and skipped paths remain listed. All checkboxes were cleared.")
        dialog.setDetailedText("\n".join(
            ["RECYCLED", *map(str, result.recycled), "", "SKIPPED / ERRORS",
             *(f"{issue.path}: {issue.reason}" for issue in result.issues)]))
        dialog.exec()

    def on_duplicate_folder_failure(self, message):
        self._scan_cache_valid = False
        self._scan_baselines = {}
        self._baseline_settings = None
        self.set_duplicate_folders(self.folder_groups)
        self.duplicate_folder_summary.setText(
            "Operation stopped · duplicate-folder results may be out of date"
            if self.duplicate_folders else "Duplicate-folder operation stopped")
        self.duplicate_folder_hint.setText(
            "Scan duplicate folders again before continuing. Check the Recycle Bin if cleanup was running.")
        self.status.setText(message)
        QMessageBox.critical(
            self, "Duplicate-folder operation stopped",
            message + "\n\nThe duplicate-file results were not changed.")

    def confirm_recycle(self):
        if self.mode_busy("duplicates") or not self.selected:
            return
        selected = frozenset(self.selected)
        groups = tuple(self.groups)
        amount = sum(self.records[path].total_size for path in selected)
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Confirm recycling")
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setText(f"Send {len(selected):,} selected files ({format_bytes(amount)}) to the Recycle Bin?")
        information = "Only the files you checked will be recycled. Contents and file safety are rechecked before recycling.\n\nFiles that cannot be safely recycled will be skipped. Nothing is permanently deleted."
        if any(not group.contents_verified and any(record.path in selected for record in group.files)
               for group in groups):
            information += ("\n\nSome selected files are possible matches from a criteria-only scan. "
                            "They will be recycled only if their contents match the comparison copy.")
        hidden = len(selected - self.visible_paths)
        if hidden:
            information = (f"Includes {hidden:,} selected file(s) hidden by the current tab or filters. "
                           "Checked files from all tabs will be recycled. Review the full list in Show Details.\n\n" + information)
        details = ["SELECTED FOR RECYCLING", *sorted(map(str, selected)), "", "COPIES KEPT IN AFFECTED GROUPS"]
        all_copy_groups = 0
        zone_warning = False
        dropbox_warning = False
        keeper_extra_warning = False
        other_warning = False
        for number, group in enumerate(groups, 1):
            if any(record.path in selected for record in group.files):
                kept = [str(record.path) for record in group.files if record.path not in selected]
                details.append(f"Group {number}")
                details.extend(kept or ["NONE — every listed copy is selected for recycling."])
                if not kept:
                    all_copy_groups += 1
                differences = (group.differing_streams if group.metadata_checked else
                               {name for record in group.files for name, size in record.streams})
                if differences:
                    details.extend(("", group.metadata_details))
                if kept:
                    selected_streams = {name for record in group.files if record.path in selected
                                        for name, size in record.streams}
                    keeper_extra_warning |= bool(set(differences) - selected_streams)
                    differences = set(differences) & selected_streams
                zone_warning |= any(name.casefold() == ":zone.identifier:$data" for name in differences)
                dropbox_warning |= any(name.casefold() == ":com.dropbox.attrs:$data" for name in differences)
                other_warning |= any(name.casefold() not in {":zone.identifier:$data", ":com.dropbox.attrs:$data"}
                                     for name in differences)
        if keeper_extra_warning:
            information += "\n\nExtra streams found only on a kept comparison copy do not block recycling."
        if other_warning:
            information += ("\n\nSome extra NTFS streams differ or have not been checked. "
                            "Extra streams found only on a kept comparison copy do not block recycling. "
                            "Selected files whose other streams are missing or different in that copy will be skipped. "
                            "If every copy is selected, other streams must match.")
        if zone_warning:
            information += ("\n\nDownload metadata (Zone.Identifier) differs or has not been checked. "
                            "Allowing this recycles the selected file with its own download metadata, "
                            "even when that metadata differs or is absent in another copy. "
                            "Retained files and their metadata stay unchanged. Leave unchecked to block loss of differing "
                            "download metadata. Extra metadata found only on a kept comparison copy is allowed without this option.")
        if dropbox_warning:
            information += ("\n\nDropbox metadata (com.dropbox.attrs) differs or has not been checked. "
                            "Allowing this recycles each selected file with its own Dropbox metadata, even when "
                            "another copy lacks that metadata or stores different bytes. Metadata unique to the selected "
                            "copy may then exist only in the Recycle Bin. Retained files stay unchanged.")
        if zone_warning or dropbox_warning:
            names = " and ".join(name for enabled, name in (
                (zone_warning, "Zone.Identifier"), (dropbox_warning, "com.dropbox.attrs")) if enabled)
            consent = QCheckBox(f"Allow differences in {names} metadata for this batch")
            consent.setChecked(False)
            dialog.setCheckBox(consent)
        if all_copy_groups:
            information = (f"WARNING: All copies selected in {all_copy_groups:,} group(s). "
                           "No listed copy will be kept in those groups if recycling succeeds. "
                           "You can restore them from the Recycle Bin until you empty it.\n\n" + information)
        dialog.setInformativeText(information)
        dialog.setDetailedText("\n".join(details))
        recycle = dialog.addButton("Recycle selected files", QMessageBox.ButtonRole.AcceptRole)
        cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(cancel)
        dialog.exec()
        if dialog.clickedButton() != recycle:
            return
        options = {}
        if (zone_warning or dropbox_warning) and consent.isChecked():
            if zone_warning:
                options["allow_zone_differences"] = True
            if dropbox_warning:
                options["allow_dropbox_differences"] = True
        self.issues = []
        self.start_job(lambda **kwargs: recycle_selected(groups, selected, **options, **kwargs), self.on_recycled)

    def on_recycled(self, result: RecycleResult):
        self.record_scan_cleanup(result.recycled)
        if result.recycled:
            recycled_paths = set(result.recycled)
            remaining_folders = []
            for group in self.folder_groups:
                folders = tuple(folder for folder in group.folders
                                if not any(file.path in recycled_paths for file in folder.files))
                if len(folders) > 1 or (folders and folders[0].empty):
                    remaining_folders.append(FolderGroup(folders))
            self.set_duplicate_folders(remaining_folders)
        self.issues = result.issues
        recycled = set(result.recycled)
        remaining_groups = []
        for group in self.groups:
            remaining_files = tuple(record for record in group.files if record.path not in recycled)
            if len(remaining_files) > 1:
                remaining_groups.append(DuplicateGroup(remaining_files, group.digest, group.byte_verified))
        self.summary_notice = None
        self.summary_context = "remaining"
        self.set_groups(remaining_groups)
        self.empty.setText("No duplicate groups remain in these results. Groups with fewer than two copies are no longer listed. Scan again to check for new duplicates.")
        self.status.setText("Cleanup cancelled; already recycled files remain in the Recycle Bin."
                            if result.cancelled else "Cleanup complete. No permanent deletion was requested.")
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Recycling results")
        dialog.setText(f"{len(result.recycled):,} files recycled  ·  {len(result.issues):,} skipped / errors")
        dialog.setInformativeText(self.status.text() + "\nRemaining duplicate groups stay in the list. All checkboxes have been cleared so you can choose the next batch.")
        dialog.setDetailedText("\n".join(
            ["RECYCLED", *map(str, result.recycled), "", "SKIPPED / ERRORS",
             *(f"{issue.path}: {issue.reason}" for issue in result.issues)]
        ))
        dialog.exec()

    def on_failure(self, message):
        self._scan_cache_valid = False
        self._scan_baselines = {}
        self._baseline_settings = None
        self.summary_notice = "Operation stopped · results may be out of date" if self.groups else "Operation stopped"
        self.set_groups(self.groups)
        self.empty.setText("Scan again before continuing. If cleanup was running, check the Recycle Bin for files already moved.")
        self.status.setText(message)
        details = message
        if self.groups:
            details += "\n\nThe list has been kept, but some files may already have been recycled. Check the Recycle Bin and scan again to refresh these results."
        QMessageBox.critical(self, "Operation stopped", details)

    def job_finished(self):
        worker = self.worker
        self.worker = None
        if worker:
            worker.deleteLater()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.current_path.clear()
        if self.scan_worker is not None and self._last_scan_progress is not None:
            self.on_progress(self._last_scan_progress)
        self.update_actions()
        self.update_details()
        self.restart_scan_if_ready()

    def cancel_work(self):
        self._restart_scan = False
        self._scan_paused = False
        if self.worker is not None or self.scan_worker is not None:
            for worker in (self.worker, self.scan_worker):
                if worker is not None:
                    worker.cancel_event.set()
            self.status.setText("Stopping safely after the current read or Windows operation…")
            self.update_actions()

    def show_issues(self):
        self.show_issue_list("Skipped files and errors", self.issues)

    def show_duplicate_folder_issues(self):
        self.show_issue_list("Skipped folders and errors", self.duplicate_folder_issues)

    def show_issue_list(self, title, issues):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(800, 480)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n\n".join(f"{issue.path}\n{issue.reason}" for issue in issues))
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def open_folder(self):
        item = self.tree.currentItem()
        if item is not None and item.parent() is not None:
            record = item.data(0, Qt.ItemDataRole.UserRole)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.path.parent))):
                QMessageBox.warning(self, "Could not open folder", str(record.path.parent))

    def open_duplicate_folder(self, item, column=0):
        if self.mode_busy("folders") or item is None or item.parent() is None:
            return
        record = item.data(0, Qt.ItemDataRole.UserRole)
        try:
            ensure_folder_current(record, Reporter(Event(), None))
        except OSError as exc:
            QMessageBox.warning(
                self, "Cannot open this folder",
                f"The folder changed or is unavailable. Scan again.\n\n{exc}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.path))):
            QMessageBox.warning(self, "Could not open folder", str(record.path))

    def closeEvent(self, event: QCloseEvent):
        if self.worker is not None or self.scan_worker is not None:
            self.cancel_work()
            event.ignore()
            self.status.setText("Stopping safely. Close the window again once the operation has stopped.")
        else:
            self.thumbnails.close()
            self.comparison_preview.close()
            self.similar_tab.shutdown()
            event.accept()


def main():
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Duplicate Cleaner")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec()
