"""On-demand previews, isolated from the GUI in a short-lived helper process."""

import json
import os
import sys
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QObject, QPoint, QProcess, QSize, Qt, QTimer
from PySide6.QtGui import QCursor, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QStyle, QVBoxLayout

from .files import ensure_current
from .models import FileRecord, format_bytes

PREVIEW_SIZE = QSize(280, 180)


def windows_thumbnail(path: Path) -> QImage:
    import ctypes
    import uuid
    from ctypes import wintypes

    import pythoncom

    # pywin32 does not wrap IShellItemImageFactory. Use its documented COM ABI.
    iid = (ctypes.c_ubyte * 16).from_buffer_copy(
        uuid.UUID("bcc18b79-ba16-442f-80c4-8a59c30c463b").bytes_le
    )
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    create = shell.SHCreateItemFromParsingName
    create.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_void_p)]
    create.restype = ctypes.c_long
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi.DeleteObject.restype = wintypes.BOOL
    factory = ctypes.c_void_p()
    bitmap = wintypes.HBITMAP()
    pythoncom.CoInitialize()
    try:
        if create(str(path), None, ctypes.byref(iid), ctypes.byref(factory)) < 0:
            return QImage()
        vtable = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
        get_image = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, wintypes.SIZE, ctypes.c_int,
            ctypes.POINTER(wintypes.HBITMAP),
        )(vtable[3])
        try:
            result = get_image(factory, wintypes.SIZE(PREVIEW_SIZE.width(), PREVIEW_SIZE.height()),
                               0x8, ctypes.byref(bitmap))  # SIIGBF_THUMBNAILONLY
            return QImage.fromHBITMAP(bitmap.value).copy() if result >= 0 and bitmap else QImage()
        finally:
            if bitmap:
                gdi.DeleteObject(bitmap)
            release(factory)
    finally:
        pythoncom.CoUninitialize()


def render_thumbnail(record: FileRecord) -> bytes:
    ensure_current(record)
    image = windows_thumbnail(record.path) if os.name == "nt" else QImage()
    if image.isNull():
        # Also support common images when a Windows thumbnail handler is absent.
        QImageReader.setAllocationLimit(64)
        reader = QImageReader(str(record.path))
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid():
            reader.setScaledSize(size.scaled(PREVIEW_SIZE, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
    ensure_current(record)
    if image.isNull():
        return b""
    image = image.scaled(PREVIEW_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    output = QBuffer()
    output.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(output, "PNG"):
        return b""
    return bytes(output.data())


class ThumbnailPopup(QFrame):
    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.ToolTip)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setObjectName("thumbnailPopup")
        self.setFixedWidth(304)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        self.preview = QLabel()
        self.preview.setFixedSize(PREVIEW_SIZE)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption = QLabel()
        self.caption.setTextFormat(Qt.TextFormat.PlainText)
        self.caption.setWordWrap(True)
        self.note = QLabel()
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setWordWrap(True)
        self.note.setObjectName("hint")
        layout.addWidget(self.preview)
        layout.addWidget(self.caption)
        layout.addWidget(self.note)

    def display(self, record: FileRecord, data: bytes | None, message: str = ""):
        image = QImage.fromData(data) if data else QImage()
        if image.isNull():
            self.preview.setPixmap(self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon).pixmap(64, 64))
            self.note.setText(message or "No thumbnail available.")
        else:
            self.preview.setPixmap(QPixmap.fromImage(image))
            self.note.setText(f"{format_bytes(record.size)} · Double-click the file to open")
        self.caption.setText(record.path.name)
        self.adjustSize()
        self.reposition()
        self.show()

    def reposition(self):
        cursor = QCursor.pos()
        screen = QApplication.screenAt(cursor) or QApplication.primaryScreen()
        bounds = screen.availableGeometry()
        point = cursor + QPoint(18, 18)
        point.setX(max(bounds.left(), min(point.x(), bounds.right() - self.width())))
        point.setY(max(bounds.top(), min(point.y(), bounds.bottom() - self.height())))
        self.move(point)


class ThumbnailController(QObject):
    def __init__(self, tree, parent):
        super().__init__(parent)
        self.tree = tree
        self.viewport = tree.viewport()
        self.popup = ThumbnailPopup(parent)
        self.record = None
        self.process = None
        self.generation = 0
        self.cache = OrderedDict()
        self.delay = QTimer(self)
        self.delay.setSingleShot(True)
        self.delay.setInterval(400)
        self.delay.timeout.connect(self.load)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.setInterval(6000)
        self.deadline.timeout.connect(self.timed_out)
        tree.setMouseTracking(True)
        self.viewport.installEventFilter(self)
        parent.installEventFilter(self)
        tree.verticalScrollBar().valueChanged.connect(self.dismiss)
        tree.horizontalScrollBar().valueChanged.connect(self.dismiss)

    def eventFilter(self, watched, event):
        if watched is self.viewport:
            if event.type() == QEvent.Type.MouseMove:
                item = self.tree.itemAt(event.position().toPoint())
                record = item.data(0, Qt.ItemDataRole.UserRole) if item and item.parent() else None
                if not self.tree.isEnabled():
                    record = None
                self.hover(record)
            elif event.type() in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress,
                                  QEvent.Type.MouseButtonDblClick, QEvent.Type.Wheel):
                self.dismiss()
            elif event.type() == QEvent.Type.ToolTip:
                item = self.tree.itemAt(event.pos())
                if item and item.parent():
                    return True  # The thumbnail replaces the ordinary file tooltip.
        elif event.type() in (QEvent.Type.Hide, QEvent.Type.WindowDeactivate):
            self.dismiss()
        return super().eventFilter(watched, event)

    def hover(self, record):
        if record == self.record:
            if self.popup.isVisible():
                self.popup.reposition()
            return
        self.dismiss()
        self.record = record
        if record:
            self.delay.start()

    def load(self):
        record = self.record
        if record is None:
            return
        try:
            ensure_current(record)
        except OSError:
            self.popup.display(record, None, "File changed or is unavailable. Scan again.")
            return
        if record in self.cache:
            self.cache.move_to_end(record)
            self.popup.display(record, self.cache[record])
            return
        if self.process is not None:
            return  # A killed helper's finished signal will start the pending preview.
        self.popup.display(record, None, "Loading thumbnail…")
        process = QProcess(self)
        self.process = process
        generation = self.generation
        executable = Path(sys.executable)
        if getattr(sys, "frozen", False):
            arguments = ["--render-thumbnail"]
        else:
            if os.name == "nt":
                executable = executable.with_name("pythonw.exe")
            arguments = ["-m", "duplicate_cleaner.thumbnails", "--render"]
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))

        def send_record():
            process.write(json.dumps(asdict(record), default=str).encode("utf-8"))
            process.closeWriteChannel()

        process.started.connect(send_record)
        process.finished.connect(lambda code, status: self.finished(process, record, generation, code))
        process.errorOccurred.connect(lambda error: self.failed_to_start(process, record, generation, error))
        process.start(str(executable), arguments)
        self.deadline.start()

    def finished(self, process, record, generation, code):
        data = bytes(process.readAllStandardOutput()) if code == 0 else b""
        if self.process is process:
            self.process = None
            self.deadline.stop()
        process.deleteLater()
        if generation != self.generation or record != self.record:
            if self.record and not self.delay.isActive():
                self.load()
            return
        try:
            ensure_current(record)
        except OSError:
            self.popup.display(record, None, "File changed or is unavailable. Scan again.")
            return
        self.cache[record] = data
        while len(self.cache) > 64:
            self.cache.popitem(last=False)
        self.popup.display(record, data)

    def failed_to_start(self, process, record, generation, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.finished(process, record, generation, 1)

    def timed_out(self):
        if self.process:
            self.process.kill()

    def dismiss(self):
        self.generation += 1
        self.record = None
        self.delay.stop()
        self.deadline.stop()
        self.popup.hide()
        if self.process:
            self.process.kill()

    def clear(self):
        self.dismiss()
        self.cache.clear()

    def close(self):
        self.viewport.removeEventFilter(self)
        self.parent().removeEventFilter(self)
        self.clear()
        if self.process:
            self.process.waitForFinished(1000)


def _binary_pipe(stream, handle, mode):
    if stream is not None:
        return stream.buffer
    # Windowed executables lack Python's standard streams even when QProcess
    # supplies Windows pipe handles. Wrap those pipes without creating a console.
    import msvcrt
    import win32api

    flags = os.O_BINARY | (os.O_RDONLY if mode == "rb" else os.O_WRONLY)
    return os.fdopen(msvcrt.open_osfhandle(win32api.GetStdHandle(handle), flags), mode)


def render_main():
    try:
        with _binary_pipe(sys.stdin, -10, "rb") as source, _binary_pipe(sys.stdout, -11, "wb") as output:
            fields = json.loads(source.read())
            fields["path"] = Path(fields["path"])
            output.write(render_thumbnail(FileRecord(**fields)))
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(render_main() if sys.argv[1:] == ["--render"] else 2)
