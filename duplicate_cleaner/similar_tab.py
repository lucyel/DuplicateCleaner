"""A self-contained, read-only workflow for visually similar images and videos."""

import os
from threading import Event

from PySide6.QtCore import QThread, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QListView, QListWidget,
    QListWidgetItem, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSplitter,
    QStackedWidget, QStyle, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .models import format_bytes
from .preview import PreviewPane
from .similarity import PRESETS, SimilarResult, scan_similar
from .video_similarity import VideoFingerprint, preview_frame, timestamp
from .video_review import VideoComparison


class SimilarWorker(QThread):
    progress = Signal(object)
    outcome = Signal(object)
    failed = Signal(str)

    def __init__(self, roots, recursive, exclusions, preset, parent, media_kind="All"):
        super().__init__(parent)
        self.roots, self.recursive, self.exclusions, self.preset = roots, recursive, exclusions, preset
        self.cancel = Event()
        self.media_kind = media_kind

    def run(self):
        try:
            self.outcome.emit(scan_similar(self.roots, self.recursive, excluded_folders=self.exclusions,
                                          preset=self.preset, cancel=self.cancel, progress=self.progress.emit,
                                          media_kind=self.media_kind))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


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
    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.result = SimilarResult()
        self.current_group = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QVBoxLayout(content)
        intro = QLabel("Find modified copies of images and videos · Independent, read-only scan")
        intro.setObjectName("section")
        layout.addWidget(intro)
        formats = QLabel("Images: JPEG, PNG, BMP, WebP, TIFF. Videos: MP4, M4V, MOV, MKV, AVI, WebM. "
                         "Video matching compares near-complete visual copies; audio is not compared.")
        formats.setWordWrap(True)
        layout.addWidget(formats)
        scope = QGridLayout()
        self.folders, self.exclusions = QListWidget(), QListWidget()
        self.scope_buttons = []
        for column, (title, listing) in enumerate((("Folders for this scan", self.folders),
                                                  ("Excluded folders", self.exclusions))):
            scope.addWidget(QLabel(title), 0, column)
            listing.setAccessibleName(title)
            listing.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            listing.setMaximumHeight(85)
            scope.addWidget(listing, 1, column)
            buttons = QHBoxLayout()
            add, remove = QPushButton("Add folder…"), QPushButton("Remove")
            add.clicked.connect(lambda checked=False, target=listing: self.pick_folder(target))
            remove.clicked.connect(lambda checked=False, target=listing: self.remove_folders(target))
            buttons.addWidget(add)
            buttons.addWidget(remove)
            scope.addLayout(buttons, 2, column)
            self.scope_buttons.extend((add, remove))
        layout.addLayout(scope)
        actions = QHBoxLayout()
        self.recursive = QCheckBox("Include subfolders")
        self.recursive.setChecked(True)
        actions.addWidget(self.recursive)
        self.media_kind = QComboBox()
        self.media_kind.addItems(["All", "Images", "Videos"])
        self.media_kind.setAccessibleName("Media types to scan")
        actions.addWidget(self.media_kind)
        actions.addWidget(QLabel("Similarity"))
        self.preset = QComboBox()
        self.preset.addItems(PRESETS)
        self.preset.setCurrentText("Balanced")
        self.preset.setAccessibleName("Image similarity preset")
        self.preset.setToolTip("Strict: fewer, closer matches. Broad: more candidates, with more false matches.")
        actions.addWidget(self.preset)
        actions.addStretch()
        self.scan_button = QPushButton("Scan similar files")
        self.scan_button.setObjectName("primary")
        self.scan_button.clicked.connect(self.start_scan)
        self.cancel_button = QPushButton("Cancel similarity scan")
        self.cancel_button.clicked.connect(self.cancel_scan)
        actions.addWidget(self.scan_button)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)
        self.summary = QLabel("Add folders above to start. These folders belong only to Similar files.")
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
        self.stack.addWidget(self.gallery)
        pair = QWidget()
        pair_layout = QHBoxLayout(pair)
        self.panes = (PreviewPane("Reference image"), PreviewPane("Candidate image"))
        for pane in self.panes:
            # Reuse only the decoder/viewer; no controls or signals join the duplicate workflow.
            pane.chooser.hide()
            pane.recycle_check.hide()
            pane.info.hide()
            pair_layout.addWidget(pane)
        self.stack.addWidget(pair)
        self.video_review = VideoComparison()
        self.stack.addWidget(self.video_review)
        review_layout.addWidget(self.stack, 1)
        splitter.addWidget(review)
        splitter.setSizes([450, 650])
        layout.addWidget(splitter, 1)
        self.status = QLabel("Ready")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.issues_button = QPushButton("Skipped files / errors (0)")
        self.issues_button.clicked.connect(self.show_issues)
        layout.addWidget(self.issues_button)
        self.update_actions()

    def add_folder(self, path, excluded=False):
        listing = self.exclusions if excluded else self.folders
        path = os.path.abspath(path)
        if all(os.path.normcase(listing.item(i).text()) != os.path.normcase(path) for i in range(listing.count())):
            listing.addItem(path)
        self.update_actions()

    def pick_folder(self, listing):
        path = QFileDialog.getExistingDirectory(self, "Choose similarity scan folder")
        if path:
            self.add_folder(path, listing is self.exclusions)

    def remove_folders(self, listing):
        for item in listing.selectedItems():
            listing.takeItem(listing.row(item))
        self.update_actions()

    def update_actions(self):
        busy = self.worker is not None
        for widget in (*self.scope_buttons, self.folders, self.exclusions, self.recursive, self.preset, self.tree,
                       self.media_kind):
            widget.setEnabled(not busy)
        self.scan_button.setEnabled(not busy and self.folders.count() > 0)
        self.cancel_button.setEnabled(busy and not self.worker.cancel.is_set())
        self.issues_button.setEnabled(bool(self.result.issues))
        self.issues_button.setText(f"Skipped files / errors ({len(self.result.issues)})")
        self.back_button.setEnabled(not busy and self.current_group is not None)

    def start_scan(self):
        if self.worker is not None or not self.folders.count():
            return
        roots = [self.folders.item(i).text() for i in range(self.folders.count())]
        exclusions = [self.exclusions.item(i).text() for i in range(self.exclusions.count())]
        self.clear_preview()
        self.tree.clear()
        self.result = SimilarResult()
        self.summary.setText("Scanning similar files…")
        self.progress.setRange(0, 0)
        self.worker = SimilarWorker(roots, self.recursive.isChecked(), exclusions, self.preset.currentText(), self,
                                    self.media_kind.currentText())
        self.worker.progress.connect(self.on_progress)
        self.worker.outcome.connect(self.on_result)
        self.worker.failed.connect(self.on_failure)
        self.worker.finished.connect(self.job_finished)
        self.update_actions()
        self.worker.start()

    def cancel_scan(self):
        if self.worker:
            self.worker.cancel.set()
            self.status.setText("Stopping similarity scan…")
            self.update_actions()

    def on_progress(self, progress):
        self.status.setText(f"{progress.stage} · {progress.completed}"
                            + (f" / {progress.total}" if progress.total else "")
                            + (f"\n{progress.path}" if progress.path else ""))
        self.progress.setRange(0, progress.total if progress.total else 0)
        self.progress.setValue(progress.completed)

    def on_result(self, result):
        self.result = result
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
            parent.setExpanded(len(result.groups) <= 50)
        if result.cancelled:
            self.summary.setText("Similarity scan cancelled. No partial groups were kept.")
        else:
            self.summary.setText(f"{len(result.groups)} candidate groups · "
                                 f"{result.compared_count - result.videos_compared} images + {result.videos_compared} videos compared · "
                                 f"{result.ignored_count} files outside chosen formats")
        self.status.setText("Cancelled" if result.cancelled else "Similarity scan complete. Review each candidate visually.")
        self.update_actions()

    def on_failure(self, message):
        self.status.setText("Similarity scan failed: " + message)
        self.summary.setText("Scan failed. Check the error below before trying again.")

    def job_finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.update_actions()

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
            self.gallery.set_group(self.current_group)

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
            return
        self.video_review.clear()
        self.stack.setCurrentIndex(1)
        self.panes[0].set_record(reference.record)
        self.panes[1].set_record(image.record)

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
