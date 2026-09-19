"""Group galleries and two-file previews, decoded in disposable helpers."""

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QProcess, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel,
    QListView, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSizePolicy, QStackedWidget, QStyle, QVBoxLayout, QWidget,
)

from .files import ensure_current, open_checked
from .models import FileRecord, format_bytes
from .thumbnails import _binary_pipe, render_thumbnail


def render_preview(record, preview_size=None):
    # Keep a checked read handle open while Qt decodes the same path.
    with open_checked(record):
        QImageReader.setAllocationLimit(128)
        reader = QImageReader(str(record.path))
        reader.setAutoTransform(True)
        if reader.canRead():
            size = reader.size()
            reduced = size.isValid() and size.width() * size.height() > 24_000_000
            if preview_size and size.isValid() and max(size.width(), size.height()) > preview_size:
                reduced = True
            if reduced:
                limit = preview_size or 4096
                reader.setScaledSize(size.scaled(QSize(limit, limit), Qt.AspectRatioMode.KeepAspectRatio))
            if reader.transformation().value & 4:  # EXIF orientations that rotate by 90 degrees.
                size.transpose()
            image = reader.read()
            if image.isNull():
                raise OSError(reader.errorString())
            kind = "image"
        else:
            image = QImage.fromData(render_thumbnail(record))
            kind = "thumbnail"
            reduced = False
            size = image.size()
        ensure_current(record)
        if image.isNull():
            return {"kind": "unavailable"}, b""
        output = QBuffer()
        output.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(output, "PNG"):
            raise OSError("Could not render this preview")
        return {"kind": kind, "width": size.width(), "height": size.height(),
                "reduced": reduced}, bytes(output.data())


def render_main():
    with _binary_pipe(sys.stdin, -10, "rb") as source, _binary_pipe(sys.stdout, -11, "wb") as output:
        try:
            fields = json.loads(source.read())
            preview_size = fields.pop("preview_size", None)
            fields["path"] = Path(fields["path"])
            fields["streams"] = tuple(tuple(pair) for pair in fields.get("streams", ()))
            fields["stream_hashes"] = tuple(tuple(pair) for pair in fields.get("stream_hashes", ()))
            metadata, data = render_preview(FileRecord(**fields), preview_size)
        except Exception as exc:
            metadata, data = {"error": str(exc)}, b""
        output.write(json.dumps(metadata).encode("utf-8") + b"\n" + data)
    return 0


class ImageView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setMinimumSize(100, 140)
        self.fitted = True

    def display(self, image):
        self.scene().clear()
        self.resetTransform()
        if not image.isNull():
            self.scene().addPixmap(QPixmap.fromImage(image))
        self.setSceneRect(self.scene().itemsBoundingRect())
        self.fit()

    def sizeHint(self):
        return QSize(240, 180)

    def fit(self):
        self.fitted = True
        if not self.sceneRect().isEmpty():
            self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def actual_size(self):
        self.fitted = False
        self.resetTransform()

    def wheelEvent(self, event):
        if self.sceneRect().isEmpty():
            return
        self.fitted = False
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        scale = self.transform().m11() * factor
        if 0.01 <= scale <= 32:
            self.scale(factor, factor)
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fitted:
            self.fit()


class PreviewPane(QWidget):
    ready = Signal(object, object, str)
    selection_changed = Signal(object, bool)

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.record = None
        self.preview_size = None
        self.process = None
        self.generation = 0
        self.metadata = {}
        self.expired = False
        self.selected = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = QLabel(title)
        heading.setObjectName("section")
        layout.addWidget(heading)
        self.chooser = QComboBox()
        self.chooser.setMinimumContentsLength(8)
        self.chooser.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.chooser.setAccessibleName(title + " file")
        layout.addWidget(self.chooser)
        self.stack = QStackedWidget()
        self.stack.setMinimumHeight(160)
        self.view = ImageView()
        self.view.setAccessibleName(title + " image; scroll to zoom, drag to pan")
        self.placeholder = QWidget()
        placeholder_layout = QVBoxLayout(self.placeholder)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon.setPixmap(self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon).pixmap(QSize(48, 48)))
        self.message = QLabel("Choose a duplicate group or file to preview.")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder_layout.addStretch()
        placeholder_layout.addWidget(self.icon)
        placeholder_layout.addWidget(self.message)
        placeholder_layout.addStretch()
        self.stack.addWidget(self.placeholder)
        self.stack.addWidget(self.view)
        layout.addWidget(self.stack, 1)
        controls = QHBoxLayout()
        self.fit_button = QPushButton("Fit")
        self.fit_button.clicked.connect(self.view.fit)
        self.actual_button = QPushButton("100%")
        self.actual_button.clicked.connect(self.view.actual_size)
        self.open_button = QPushButton("Open")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_file)
        for button in (self.fit_button, self.actual_button, self.open_button):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.recycle_check = QCheckBox("Select for recycling")
        self.recycle_check.setAccessibleName(title + " selected for recycling")
        self.recycle_check.setEnabled(False)
        self.recycle_check.toggled.connect(self.selection_toggled)
        layout.addWidget(self.recycle_check)
        self.info = QLabel()
        self.info.setMinimumWidth(0)
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.PlainText)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.info)
        self.path = QLabel()
        self.path.setMinimumWidth(0)
        self.path.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.path.setTextFormat(Qt.TextFormat.PlainText)
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path)
        self.delay = QTimer(self)
        self.delay.setSingleShot(True)
        self.delay.setInterval(120)
        self.delay.timeout.connect(self.load)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.setInterval(10000)
        self.deadline.timeout.connect(self.timed_out)
        self.show_message("Choose a duplicate group or file to preview.")

    def show_message(self, message):
        self.message.setText(message)
        self.stack.setCurrentWidget(self.placeholder)
        self.view.display(QImage())
        self.fit_button.setEnabled(False)
        self.actual_button.setEnabled(False)

    def set_record(self, record):
        if record == self.record:
            self.update_info()
            return
        self.generation += 1
        self.record = record
        self.metadata = {}
        self.delay.stop()
        self.deadline.stop()
        if self.process:
            self.process.kill()
        self.open_button.setEnabled(record is not None)
        self.show_message("Loading preview…" if record else "Choose a duplicate group or file to preview.")
        self.update_info()
        if record:
            self.delay.start()

    def update_info(self):
        blocked = self.recycle_check.blockSignals(True)
        self.recycle_check.setEnabled(self.record is not None)
        self.recycle_check.setChecked(self.record is not None and self.record.path in self.selected)
        self.recycle_check.blockSignals(blocked)
        self.recycle_check.setToolTip(str(self.record.path) if self.record else "Choose a file first.")
        if self.record is None:
            self.info.clear()
            self.path.clear()
            self.path.setToolTip("")
            return
        state = "Selected for recycling" if self.record.path in self.selected else "Unchecked — will remain"
        dimensions = ""
        if "width" in self.metadata:
            dimensions = f"{self.metadata['width']} × {self.metadata['height']} px · "
            if self.metadata.get("kind") == "thumbnail":
                dimensions = "Thumbnail preview · "
            elif self.metadata.get("reduced"):
                dimensions += "Reduced preview · "
        self.info.setText(f"{dimensions}{format_bytes(self.record.total_size)}\n{state}")
        self.path.setText(self.path.fontMetrics().elidedText(
            str(self.record.path), Qt.TextElideMode.ElideMiddle, max(80, self.path.width())))
        self.path.setToolTip(str(self.record.path))
        self.path.setAccessibleName(str(self.record.path))

    def selection_toggled(self, checked):
        if self.record is not None and checked != (self.record.path in self.selected):
            self.selection_changed.emit(self.record.path, checked)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_info()

    def load(self):
        if self.record is None or self.process is not None:
            return
        record, generation = self.record, self.generation
        try:
            ensure_current(record)
        except OSError as exc:
            self.show_message(f"File changed or is unavailable. Scan again.\n{exc}")
            self.ready.emit(record, QImage(), str(exc))
            return
        process = QProcess(self)
        self.process = process
        self.expired = False
        executable = Path(sys.executable)
        arguments = ["--render-preview"]
        if not getattr(sys, "frozen", False):
            if os.name == "nt":
                executable = executable.with_name("pythonw.exe")
            arguments = ["-m", "duplicate_cleaner.preview", "--render"]
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))

        def send_record():
            fields = asdict(record)
            if self.preview_size:
                fields["preview_size"] = self.preview_size
            process.write(json.dumps(fields, default=str).encode("utf-8"))
            process.closeWriteChannel()

        process.started.connect(send_record)
        process.finished.connect(lambda code, status: self.finished(process, record, generation, code))
        process.errorOccurred.connect(lambda error: self.finished(process, record, generation, 1)
                                     if error == QProcess.ProcessError.FailedToStart else None)
        process.start(str(executable), arguments)
        self.deadline.start()

    def finished(self, process, record, generation, code):
        if self.process is not process:
            return
        payload = bytes(process.readAllStandardOutput()) if code == 0 else b""
        self.process = None
        self.deadline.stop()
        process.deleteLater()
        if generation != self.generation or record != self.record:
            if self.record and not self.delay.isActive():
                self.load()
            return
        try:
            ensure_current(record)
            if not payload:
                if self.expired:
                    raise OSError("Preview timed out. Open the file externally or select it again.")
                raise OSError("Preview could not be loaded. Try opening the file externally.")
            header, data = payload.split(b"\n", 1)
            metadata = json.loads(header)
            if "error" in metadata:
                raise OSError(metadata["error"])
            image = QImage.fromData(data)
            self.metadata = metadata
            if image.isNull():
                self.show_message("Preview unavailable for this file type. Use Open to view it.")
            else:
                self.view.display(image)
                self.stack.setCurrentWidget(self.view)
                self.view.fit()
                self.fit_button.setEnabled(True)
                self.actual_button.setEnabled(not metadata.get("reduced", False))
            self.update_info()
            self.ready.emit(record, image, "Preview unavailable" if image.isNull() else "")
        except (OSError, ValueError) as exc:
            self.show_message(f"Preview unavailable.\n{exc}")
            self.ready.emit(record, QImage(), str(exc))

    def timed_out(self):
        if self.process:
            self.expired = True
            self.process.kill()

    def open_file(self):
        if self.record:
            try:
                ensure_current(self.record)
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.record.path))):
                    raise OSError("Windows could not open this file")
            except OSError as exc:
                self.show_message(f"File changed or is unavailable.\n{exc}")

    def close(self):
        self.set_record(None)
        if self.process:
            self.process.waitForFinished(1000)
        return super().close()


class GroupGallery(QListWidget):
    selection_changed = Signal(object, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("All files in the selected duplicate group")
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        # Keep the layout width stable when a group gains or loses a scrollable row.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setIconSize(QSize(190, 140))
        self.setGridSize(QSize(210, 210))
        self.setWordWrap(True)
        self.setMinimumHeight(360)
        self.files = ()
        self.selected = set()
        self.visible = set()
        self.requested = set()
        self.images = {}
        self.file_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self.loaders = (PreviewPane("Gallery loader", self), PreviewPane("Gallery loader", self))
        for loader in self.loaders:
            loader.hide()
            loader.preview_size = 1280
            loader.ready.connect(lambda record, image, error, source=loader:
                                 self.loaded(source, record, image, error))
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.timeout.connect(self.refresh_visible)
        self.verticalScrollBar().valueChanged.connect(lambda: self.refresh_timer.start(0))
        self.itemChanged.connect(self.selection_toggled)

    def set_group(self, files, selected):
        blocked = self.blockSignals(True)
        try:
            if files != self.files:
                self.clear()
                self.files = files
                for record in files:
                    item = QListWidgetItem(self.file_icon, record.path.name, self)
                    item.setSizeHint(self.gridSize())
                    item.setData(Qt.ItemDataRole.UserRole, record)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setToolTip(str(record.path))
            self.selected = set(selected)
            for index, record in enumerate(files):
                checked = record.path in selected
                state = "Selected for recycling" if checked else "Unchecked"
                self.item(index).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                self.item(index).setText(f"{record.path.name}\n{format_bytes(record.total_size)} · {state}")
        finally:
            self.blockSignals(blocked)
        self.refresh_timer.start(0)

    def selection_toggled(self, item):
        record = item.data(Qt.ItemDataRole.UserRole)
        checked = item.checkState() == Qt.CheckState.Checked
        # Thumbnail and label changes also emit itemChanged; only forward check changes.
        if record is not None and checked != (record.path in self.selected):
            if checked:
                self.selected.add(record.path)
            else:
                self.selected.discard(record.path)
            self.selection_changed.emit(record.path, checked)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh_timer.start(0)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_timer.start(0)

    def refresh_visible(self):
        if not self.isVisible() or not self.files:
            return
        self.resize_tiles()
        visible = {index for index in range(self.count())
                   if self.visualItemRect(self.item(index)).intersects(self.viewport().rect())}
        for index in self.visible - visible:
            self.item(index).setIcon(self.file_icon)
            self.requested.discard(index)
            self.images.pop(index, None)
        self.visible = visible
        pending = sorted(visible - self.requested)
        for loader in self.loaders:
            if not pending:
                break
            if loader.process is None and loader.record is None:
                index = pending.pop(0)
                self.requested.add(index)
                loader.set_record(self.files[index])
        if pending or any(loader.record is not None or loader.process is not None for loader in self.loaders):
            self.refresh_timer.start(100)

    def resize_tiles(self):
        area = self.viewport().size()
        columns = min(self.count(), max(1, area.width() // 280))
        rows = (self.count() + columns - 1) // columns
        visible_rows = min(rows, max(1, area.height() // 250))
        grid = QSize(max(1, (area.width() - 4) // columns),
                     max(1, (area.height() - 4) // visible_rows))
        size = QSize(max(1, grid.width() - 28), max(1, grid.height() - 60))
        if grid == self.gridSize() and size == self.iconSize():
            return
        self.setGridSize(grid)
        self.setIconSize(size)
        for index in range(self.count()):
            self.item(index).setSizeHint(grid)
        for index, image in self.images.items():
            self.set_thumbnail(index, image)
        self.doItemsLayout()

    def set_thumbnail(self, index, image):
        size = self.iconSize()
        thumbnail = image.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                 Qt.TransformationMode.SmoothTransformation)
        # Fill the tile without stretching; retain the source for panel resizing.
        thumbnail = thumbnail.copy((thumbnail.width() - size.width()) // 2,
                                   (thumbnail.height() - size.height()) // 2,
                                   size.width(), size.height())
        self.item(index).setIcon(QIcon(QPixmap.fromImage(thumbnail)))

    def loaded(self, loader, record, image, error):
        if record in self.files:
            index = self.files.index(record)
            if index in self.visible:
                item = self.item(index)
                if image.isNull():
                    item.setIcon(self.file_icon)
                else:
                    self.images[index] = image
                    self.set_thumbnail(index, image)
                item.setToolTip(str(record.path) + (f"\n{error}" if error else ""))
        loader.set_record(None)
        self.refresh_timer.start(0)

    def clear(self):
        self.refresh_timer.stop()
        for loader in self.loaders:
            loader.set_record(None)
        self.files, self.visible, self.requested = (), set(), set()
        self.selected.clear()
        self.images.clear()
        super().clear()

    def close(self):
        self.clear()
        for loader in self.loaders:
            loader.close()
        return super().close()


class ComparisonPreview(QWidget):
    selection_changed = Signal(object, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.files = ()
        self.anchor = None
        self.pair_indices = (0, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.metadata_badge = QLabel()
        self.metadata_badge.setObjectName("metadataBadge")
        self.metadata_badge.setTextFormat(Qt.TextFormat.PlainText)
        self.metadata_badge.setWordWrap(True)
        layout.addWidget(self.metadata_badge)
        self.metadata_toggle = QPushButton("Show stream details")
        self.metadata_toggle.setCheckable(True)
        layout.addWidget(self.metadata_toggle)
        self.metadata_details = QPlainTextEdit()
        self.metadata_details.setReadOnly(True)
        self.metadata_details.setAccessibleName("NTFS stream differences by file")
        self.metadata_details.setMaximumHeight(160)
        layout.addWidget(self.metadata_details)
        self.metadata_toggle.toggled.connect(self.metadata_details.setVisible)
        self.metadata_toggle.toggled.connect(lambda checked: self.metadata_toggle.setText(
            "Hide stream details" if checked else "Show stream details"))
        self.metadata_details.hide()
        self.metadata_badge.hide()
        self.metadata_toggle.hide()
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)
        self.gallery = GroupGallery()
        self.gallery.selection_changed.connect(self.selection_changed)
        self.stack.addWidget(self.gallery)
        self.pair = QWidget()
        pair_layout = QHBoxLayout(self.pair)
        pair_layout.setContentsMargins(0, 0, 0, 0)
        self.stack.addWidget(self.pair)
        self.panes = (PreviewPane("File A"), PreviewPane("File B"))
        for index, pane in enumerate(self.panes):
            pair_layout.addWidget(pane, 1)
            pane.selection_changed.connect(self.selection_changed)
            pane.chooser.currentIndexChanged.connect(lambda value, slot=index: self.choose(slot, value))

    def set_group(self, group, anchor, selected):
        files = group.files
        self.metadata_badge.setText(group.metadata_status + (
            " · Main file contents match" if group.contents_verified else ""))
        warning = group.metadata_status != "Exact match"
        if self.metadata_badge.property("warning") != warning:
            self.metadata_badge.setProperty("warning", warning)
            self.metadata_badge.style().unpolish(self.metadata_badge)
            self.metadata_badge.style().polish(self.metadata_badge)
        self.metadata_badge.show()
        has_streams = any(record.streams for record in files)
        self.metadata_toggle.setVisible(has_streams)
        self.metadata_details.setPlainText(group.metadata_details)
        self.metadata_details.setVisible(has_streams and self.metadata_toggle.isChecked())
        group_changed = files != self.files
        if group_changed:
            self.pair_indices = (0, 1)
        if anchor in files and (group_changed or anchor != self.anchor):
            index = files.index(anchor)
            if index not in self.pair_indices:
                # Keep the comparison copy and follow the group's order, rather
                # than moving every clicked file into the left-hand pane.
                self.pair_indices = tuple(sorted((self.pair_indices[0], index)))
        self.files, self.anchor = files, anchor
        if anchor is None:
            for pane in self.panes:
                pane.set_record(None)
            self.stack.setCurrentWidget(self.gallery)
            self.gallery.set_group(files, selected)
            return
        self.gallery.clear()
        self.stack.setCurrentWidget(self.pair)
        for pane in self.panes:
            pane.selected = selected
        for pane, index in zip(self.panes, self.pair_indices):
            if group_changed or pane.record != files[index]:
                pane.chooser.blockSignals(True)
                pane.chooser.clear()
                for record in files:
                    pane.chooser.addItem(record.path.name)
                    pane.chooser.setItemData(pane.chooser.count() - 1, str(record.path), Qt.ItemDataRole.ToolTipRole)
                pane.chooser.setCurrentIndex(index)
                pane.chooser.blockSignals(False)
                pane.set_record(files[index])
            else:
                pane.update_info()

    def choose(self, slot, index):
        if not 0 <= index < len(self.files):
            return
        pane, other = self.panes[slot], self.panes[1 - slot]
        previous = self.files.index(pane.record)
        if other.chooser.currentIndex() == index:
            other.chooser.blockSignals(True)
            other.chooser.setCurrentIndex(previous)
            other.chooser.blockSignals(False)
            other.set_record(self.files[previous])
        pane.set_record(self.files[index])
        self.pair_indices = tuple(self.files.index(current.record) for current in self.panes)

    def clear(self):
        self.files, self.anchor = (), None
        self.pair_indices = (0, 1)
        self.metadata_badge.hide()
        self.metadata_toggle.hide()
        self.metadata_details.hide()
        self.metadata_details.clear()
        self.gallery.clear()
        for pane in self.panes:
            pane.set_record(None)
            pane.chooser.blockSignals(True)
            pane.chooser.clear()
            pane.chooser.blockSignals(False)

    def close(self):
        self.gallery.close()
        for pane in self.panes:
            pane.close()
        return super().close()


if __name__ == "__main__":
    raise SystemExit(render_main() if sys.argv[1:] == ["--render"] else 2)
