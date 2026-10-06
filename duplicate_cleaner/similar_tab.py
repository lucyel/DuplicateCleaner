"""Manual selections and previews for visually similar images and videos."""

import os
import random

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QListView, QListWidget,
    QListWidgetItem, QMenu, QPlainTextEdit, QPushButton, QScrollArea, QSplitter,
    QStackedWidget, QStyle, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .models import format_bytes
from .preview import PreviewPane
from .similarity import SimilarResult
from .video_similarity import VideoFingerprint, preview_frame, timestamp
from .video_review import VideoComparison


class SimilarGallery(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setIconSize(QSize(180, 130))
        self.setGridSize(QSize(210, 205))
        self.setWordWrap(True)
        self.setMinimumHeight(250)
        self.setAccessibleName("Similar files in this group; double-click to compare")
        self.items = ()
        self.visible, self.requested = set(), set()
        self.placeholder = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self.loaders = (PreviewPane("Similarity thumbnail", self), PreviewPane("Similarity thumbnail", self))
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        self.verticalScrollBar().valueChanged.connect(lambda: self.timer.start(0))
        for loader in self.loaders:
            loader.hide()
            loader.preview_size = 320
            loader.ready.connect(lambda record, image, error, source=loader: self.loaded(source, record, image, error))

    def set_group(self, group):
        self.clear_group()
        self.setGridSize(QSize(210, 235 if isinstance(group.reference, VideoFingerprint) else 205))
        self.items = ((group.reference, None), *group.matches)
        for image, distance in self.items:
            video = isinstance(image, VideoFingerprint)
            label = "Reference" if distance is None else (f"{distance.matched} / 12 sections match" if video
                                                        else f"Visual distance: {distance} / 63")
            if video:
                label += f"\n{timestamp(image.duration)} · {image.width} × {image.height}"
            item = QListWidgetItem(self.placeholder, f"{image.record.path.name}\n{label}", self)
            item.setData(Qt.ItemDataRole.UserRole, image)
            item.setToolTip(str(image.record.path))
        self.timer.start(0)

    def refresh(self):
        if not self.items or not self.isVisible():
            return
        visible = {i for i in range(self.count()) if self.visualItemRect(self.item(i)).intersects(self.viewport().rect())}
        for i in self.visible - visible:
            self.item(i).setIcon(self.placeholder)
            self.requested.discard(i)
        self.visible = visible
        pending = sorted(visible - self.requested)
        for index in list(pending):
            video = self.items[index][0]
            if isinstance(video, VideoFingerprint):
                try:
                    self.item(index).setIcon(QIcon(QPixmap.fromImage(preview_frame(video, 12))))
                except OSError as exc:
                    self.item(index).setToolTip(str(video.record.path) + "\n" + str(exc))
                self.requested.add(index)
                pending.remove(index)
        for loader in self.loaders:
            if pending and loader.record is None and loader.process is None:
                index = pending.pop(0)
                self.requested.add(index)
                loader.set_record(self.items[index][0].record)
        if pending or any(loader.record is not None or loader.process is not None for loader in self.loaders):
            self.timer.start(100)

    def loaded(self, loader, record, image, error):
        for index in self.visible:
            if self.items[index][0].record == record:
                self.item(index).setIcon(self.placeholder if image.isNull() else QIcon(QPixmap.fromImage(image)))
                self.item(index).setToolTip(str(record.path) + (f"\n{error}" if error else ""))
                break
        loader.set_record(None)
        self.timer.start(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.timer.start(0)

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start(0)

    def clear_group(self):
        self.timer.stop()
        for loader in self.loaders:
            loader.set_record(None)
        self.items, self.visible, self.requested = (), set(), set()
        self.clear()

    def shutdown(self):
        self.clear_group()
        for loader in self.loaders:
            loader.close()


class SimilarTab(QWidget):
    recycle_requested = Signal()
    open_location_requested = Signal(object)
    results_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.busy = False
        self.result = SimilarResult()
        self.session_available = False
        self.current_group = None
        self.selected = set()
        self.records = {}
        self._changing_checks = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QVBoxLayout(content)
        intro = QLabel("Review similar images and videos")
        intro.setObjectName("section")
        layout.addWidget(intro)
        formats = QLabel("Images: JPEG, PNG, BMP, WebP, TIFF. Videos: MP4, M4V, MOV, MKV, AVI, WebM. "
                         "Video matching compares near-complete visual copies; audio is not compared.")
        formats.setWordWrap(True)
        layout.addWidget(formats)
        self.summary = QLabel("Choose folders in Scan location, enable Similar files in Search criteria, then click Scan.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        splitter = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Group / file", "Match evidence", "Resolution", "Size", "Full path", "Duration"])
        self.tree.setColumnWidth(0, 210)
        self.tree.setColumnWidth(1, 155)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAccessibleName("Similar image and video suggestions")
        self.tree.currentItemChanged.connect(self.review_selection)
        self.tree.itemChanged.connect(self.item_changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_result_menu)
        splitter.addWidget(self.tree)
        review = QWidget()
        review_layout = QVBoxLayout(review)
        self.review_note = QLabel("Choose a group to see every file. Double-click a gallery card to compare.\n"
                                  "Images show visual distance; videos show matching sampled sections.")
        self.review_note.setWordWrap(True)
        review_layout.addWidget(self.review_note)
        self.back_button = QPushButton("Show whole group")
        self.back_button.clicked.connect(self.show_gallery)
        review_layout.addWidget(self.back_button)
        self.stack = QStackedWidget()
        self.stack.setMinimumHeight(300)
        self.gallery = SimilarGallery()
        self.gallery.itemActivated.connect(self.compare_gallery_item)
        self.gallery.itemChanged.connect(self.gallery_item_changed)
        self.gallery.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.gallery.customContextMenuRequested.connect(self.show_gallery_menu)
        self.stack.addWidget(self.gallery)
        pair = QWidget()
        pair_layout = QHBoxLayout(pair)
        self.panes = (PreviewPane("Reference image"), PreviewPane("Candidate image"))
        for pane in self.panes:
            pane.chooser.hide()
            pane.info.hide()
            pane.selection_changed.connect(self.set_checked)
            pair_layout.addWidget(pane)
        self.stack.addWidget(pair)
        self.video_review = VideoComparison()
        self.video_review.selection_changed.connect(self.set_checked)
        self.stack.addWidget(self.video_review)
        review_layout.addWidget(self.stack, 1)
        splitter.addWidget(review)
        splitter.setSizes([450, 650])
        layout.addWidget(splitter, 1)
        self.selection_label = QLabel()
        layout.addWidget(self.selection_label)
        actions = QHBoxLayout()
        self.clear_button = QPushButton("Clear file selection")
        self.clear_button.clicked.connect(self.clear_selection)
        actions.addWidget(self.clear_button)
        actions.addStretch()
        self.recycle_button = QPushButton("Recycle selected files")
        self.recycle_button.setObjectName("primary")
        self.recycle_button.clicked.connect(self.recycle_requested)
        actions.addWidget(self.recycle_button)
        layout.addLayout(actions)
        self.status = QLabel("Ready")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.issues_button = QPushButton("Skipped files / errors (0)")
        self.issues_button.clicked.connect(self.show_issues)
        layout.addWidget(self.issues_button)
        self.update_actions()

    def set_busy(self, busy):
        self.busy = busy
        self.tree.setEnabled(not busy)
        self.stack.setEnabled(not busy)
        self.update_actions()

    def update_actions(self):
        self.issues_button.setEnabled(bool(self.result.issues))
        self.issues_button.setText(f"Skipped files / errors ({len(self.result.issues)})")
        self.back_button.setEnabled(not self.busy and self.current_group is not None)
        self.clear_button.setEnabled(not self.busy and bool(self.selected))
        self.recycle_button.setEnabled(not self.busy and bool(self.selected))
        amount = sum(self.records[path].total_size for path in self.selected)
        noun = "file" if len(self.selected) == 1 else "files"
        self.selection_label.setText(f"{len(self.selected):,} {noun} selected for recycling · {format_bytes(amount)}")

    def prepare_scan(self):
        self.clear_preview()
        self.tree.clear()
        self.result = SimilarResult()
        self.session_available = False
        self.selected.clear()
        self.records.clear()
        self.summary.setText("Waiting for similarity scan…")
        self.status.setText("The shared scan progress is shown below.")
        self.update_actions()
        self.results_changed.emit()

    def on_result(self, result, *, selected=(), session_available=None):
        selected = set(selected)
        self.result = result
        self.session_available = not result.cancelled and (session_available is None or session_available)
        self.records = {image.record.path: image.record for group in result.groups
                        for image, distance in ((group.reference, None), *group.matches)}
        self.selected.clear()
        self.selected.update(selected.intersection(self.records))
        self.clear_preview()
        self._changing_checks = True
        self.tree.clear()
        for number, group in enumerate(result.groups, 1):
            video = isinstance(group.reference, VideoFingerprint)
            kind = "videos" if video else "images"
            parent = QTreeWidgetItem(self.tree, [f"Group {number} · {len(group.matches) + 1} {kind}"])
            parent.setData(0, Qt.ItemDataRole.UserRole, (group, None))
            for image, distance in ((group.reference, None), *group.matches):
                item = QTreeWidgetItem(parent, [image.record.path.name,
                    "Reference" if distance is None else (f"{distance.matched} / 12 sections" if video else f"{distance} / 63"),
                    f"{image.width} × {image.height}", format_bytes(image.record.size), str(image.record.path),
                    timestamp(image.duration) if video else ""])
                item.setData(0, Qt.ItemDataRole.UserRole, (group, image))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked if image.record.path in self.selected else Qt.CheckState.Unchecked)
            parent.setExpanded(len(result.groups) <= 50)
        self._changing_checks = False
        if result.cancelled:
            self.summary.setText("Similarity scan cancelled. No partial groups were kept.")
        else:
            self.summary.setText(f"{len(result.groups)} candidate groups · "
                                 f"{result.compared_count - result.videos_compared} images + {result.videos_compared} videos compared · "
                                 f"{result.ignored_count} files outside chosen formats")
        self.status.setText("Cancelled" if result.cancelled else "Similarity scan complete. Review each candidate visually.")
        self.update_actions()
        self.results_changed.emit()

    def on_failure(self, message):
        self.status.setText("Similarity scan failed: " + message)
        self.summary.setText("Scan failed. Check the error below before trying again.")

    def review_selection(self):
        item = self.tree.currentItem()
        if item is None:
            self.clear_preview()
            return
        self.current_group, image = item.data(0, Qt.ItemDataRole.UserRole)
        if image is None:
            self.show_gallery()
        else:
            self.compare(image)
        self.update_actions()

    def show_gallery(self):
        if self.current_group:
            self.video_review.clear()
            for pane in self.panes:
                pane.set_record(None)
            self.stack.setCurrentIndex(0)
            self._changing_checks = True
            try:
                self.gallery.set_group(self.current_group)
                for index in range(self.gallery.count()):
                    item = self.gallery.item(index)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    path = item.data(Qt.ItemDataRole.UserRole).record.path
                    item.setCheckState(Qt.CheckState.Checked if path in self.selected else Qt.CheckState.Unchecked)
            finally:
                self._changing_checks = False

    def compare_gallery_item(self, item):
        self.compare(item.data(Qt.ItemDataRole.UserRole))

    def compare(self, image):
        reference = self.current_group.reference
        if image == reference:
            image = self.current_group.matches[0][0]
        self.gallery.clear_group()
        if isinstance(reference, VideoFingerprint):
            for pane in self.panes:
                pane.set_record(None)
            evidence = next(evidence for candidate, evidence in self.current_group.matches if candidate == image)
            self.stack.setCurrentWidget(self.video_review)
            self.video_review.set_pair(reference, image, evidence)
            self.sync_checks()
            return
        self.video_review.clear()
        self.stack.setCurrentIndex(1)
        self.panes[0].set_record(reference.record)
        self.panes[1].set_record(image.record)
        self.sync_checks()

    def file_items(self):
        for index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(index)
            for child in range(parent.childCount()):
                yield parent.child(child)

    def item_changed(self, item, column):
        if self._changing_checks or column != 0 or item.parent() is None:
            return
        image = item.data(0, Qt.ItemDataRole.UserRole)[1]
        self.set_checked(image.record.path, item.checkState(0) == Qt.CheckState.Checked)

    def gallery_item_changed(self, item):
        if not self._changing_checks:
            self.set_checked(item.data(Qt.ItemDataRole.UserRole).record.path,
                             item.checkState() == Qt.CheckState.Checked)

    def set_checked(self, path, checked):
        if self.busy or path not in self.records:
            self.sync_checks()
            return
        if checked:
            self.selected.add(path)
        else:
            self.selected.discard(path)
        self.sync_checks()

    def sync_checks(self):
        self._changing_checks = True
        try:
            for item in self.file_items():
                image = item.data(0, Qt.ItemDataRole.UserRole)[1]
                item.setCheckState(0, Qt.CheckState.Checked if image.record.path in self.selected else Qt.CheckState.Unchecked)
            for index in range(self.gallery.count()):
                item = self.gallery.item(index)
                path = item.data(Qt.ItemDataRole.UserRole).record.path
                item.setCheckState(Qt.CheckState.Checked if path in self.selected else Qt.CheckState.Unchecked)
            for pane in self.panes:
                pane.selected = self.selected
                pane.update_info()
            self.video_review.set_selection(self.selected)
        finally:
            self._changing_checks = False
        self.update_actions()

    def clear_selection(self):
        if self.busy:
            return
        self.selected.clear()
        self.sync_checks()

    def select_group(self, group):
        if self.busy:
            return
        self.selected.update(image.record.path for image, distance in ((group.reference, None), *group.matches)
                             if image.record.path in self.records)
        self.sync_checks()

    def select_folder(self, image):
        if self.busy:
            return
        folder = os.path.normcase(str(image.record.path.parent))
        paths = {path for path in self.records if os.path.normcase(str(path.parent)) == folder}
        selected = self.selected | paths
        kept = 0
        for group in self.result.groups:
            group_paths = {item.record.path for item, distance in ((group.reference, None), *group.matches)}
            in_folder = group_paths & paths
            if in_folder and group_paths <= selected:
                keeper = random.choice(sorted(in_folder - self.selected or in_folder))
                selected.remove(keeper)
                kept += 1
        self.selected.clear()
        self.selected.update(selected)
        self.sync_checks()
        self.status.setText(f"Selected similar files in {image.record.path.parent} · kept one file in {kept} group(s)")

    def show_result_menu(self, position):
        item = self.tree.itemAt(position)
        if item is None:
            return
        self.tree.setCurrentItem(item)
        group, image = item.data(0, Qt.ItemDataRole.UserRole)
        self.show_menu(group, image, self.tree.viewport().mapToGlobal(position))

    def show_gallery_menu(self, position):
        item = self.gallery.itemAt(position)
        if item is not None and self.current_group is not None:
            self.show_menu(self.current_group, item.data(Qt.ItemDataRole.UserRole),
                           self.gallery.viewport().mapToGlobal(position))

    def show_menu(self, group, image, position):
        menu = QMenu(self)
        if image is not None:
            open_location = menu.addAction("Open file location")
            open_location.setEnabled(not self.busy)
            open_location.triggered.connect(lambda: self.open_location_requested.emit(image.record))
            menu.addSeparator()
            select_folder = menu.addAction("Select this folder's similar files (keep one file)")
            select_folder.setEnabled(not self.busy)
            select_folder.triggered.connect(lambda: self.select_folder(image))
        select_group = menu.addAction("Select all items in this group")
        select_group.setEnabled(not self.busy)
        select_group.triggered.connect(lambda: self.select_group(group))
        menu.exec(position)

    def clear_preview(self):
        self.current_group = None
        self.video_review.clear()
        self.gallery.clear_group()
        for pane in self.panes:
            pane.set_record(None)

    def show_issues(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Similarity scan — skipped files / errors")
        dialog.resize(750, 420)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n\n".join(f"{issue.path}\n{issue.reason}" for issue in self.result.issues))
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def shutdown(self):
        self.video_review.clear()
        self.gallery.shutdown()
        for pane in self.panes:
            pane.close()
