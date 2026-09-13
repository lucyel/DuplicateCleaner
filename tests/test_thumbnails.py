import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QProcess, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QTreeWidget, QWidget

from duplicate_cleaner.files import capture
from duplicate_cleaner.thumbnails import ThumbnailController, render_thumbnail, windows_thumbnail
from tests.support import FileTestCase


class ThumbnailTests(FileTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.parent = QWidget()
        self.tree = QTreeWidget(self.parent)
        self.controller = ThumbnailController(self.tree, self.parent)
        self.addCleanup(self.parent.close)
        self.addCleanup(self.controller.close)

    def picture(self):
        path = self.root / "Ảnh mẫu.png"
        image = QImage(640, 360, QImage.Format.Format_RGB32)
        image.fill(QColor("#2563eb"))
        self.assertTrue(image.save(str(path)))
        return capture(path)

    def wait_until(self, condition):
        loop = QEventLoop()
        poll = QTimer()
        poll.timeout.connect(lambda: loop.quit() if condition() else None)
        poll.start(10)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        timeout.start(8000)
        loop.exec()
        poll.stop()
        timeout.stop()
        self.assertTrue(condition(), "Preview did not complete within the test deadline")

    def test_image_thumbnail_preserves_size_and_content(self):
        record = self.picture()
        image = QImage.fromData(render_thumbnail(record))
        self.assertFalse(image.isNull())
        self.assertLessEqual(image.width(), 280)
        self.assertLessEqual(image.height(), 180)
        self.assertAlmostEqual(image.width() / image.height(), 640 / 360, delta=0.03)
        self.assertEqual(image.pixelColor(image.width() // 2, image.height() // 2).name(), "#2563eb")

    def test_windows_shell_returns_a_real_image_thumbnail(self):
        if os.name != "nt":
            self.skipTest("Windows thumbnail handler test")
        self.assertFalse(windows_thumbnail(self.picture().path).isNull())

    def test_common_images_work_without_a_shell_thumbnail_handler(self):
        record = self.picture()
        with patch("duplicate_cleaner.thumbnails.windows_thumbnail", return_value=QImage()):
            self.assertFalse(QImage.fromData(render_thumbnail(record)).isNull())

    def test_unknown_file_type_returns_no_thumbnail(self):
        record = capture(self.file("data.unknown", b"not an image"))
        self.assertEqual(render_thumbnail(record), b"")

    def test_helper_works_without_python_standard_streams(self):
        if os.name != "nt":
            self.skipTest("Windows windowed executable pipes")
        record = self.picture()
        command = ("import sys; from duplicate_cleaner.thumbnails import render_main; "
                   "sys.stdin = sys.stdout = None; raise SystemExit(render_main())")
        result = subprocess.run(
            [sys.executable, "-c", command],
            input=json.dumps(asdict(record), default=str).encode("utf-8"),
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=Path(__file__).resolve().parents[1],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        image = QImage.fromData(result.stdout)
        self.assertFalse(image.isNull())
        self.assertEqual(image.pixelColor(image.width() // 2, image.height() // 2).name(), "#2563eb")

    def test_portable_executable_renders_and_rejects_changed_files(self):
        executable = os.environ.get("DUPLICATE_CLEANER_EXE")
        if not executable:
            self.skipTest("Set DUPLICATE_CLEANER_EXE to test the packaged thumbnail helper")
        record = self.picture()
        environment = os.environ.copy()
        environment["PATH"] = str(Path(os.environ["WINDIR"]) / "System32")
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        for changed in (False, True):
            with self.subTest(changed=changed):
                if changed:
                    record.path.write_bytes(b"changed after scanning")
                result = subprocess.run(
                    [str(Path(executable).resolve()), "--render-thumbnail"],
                    input=json.dumps(asdict(record), default=str).encode("utf-8"),
                    capture_output=True, timeout=30, env=environment, cwd=self.root,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                self.assertEqual(result.returncode, 1 if changed else 0, result.stderr)
                if changed:
                    self.assertFalse(result.stdout)
                else:
                    image = QImage.fromData(result.stdout)
                    self.assertFalse(image.isNull())
                    self.assertEqual(image.pixelColor(image.width() // 2, image.height() // 2).name(), "#2563eb")

    def test_changed_file_is_rejected(self):
        record = self.picture()
        record.path.write_bytes(b"changed")
        with self.assertRaises(OSError):
            render_thumbnail(record)

    def test_hover_uses_helper_and_caches_the_thumbnail(self):
        record = self.picture()
        self.controller.hover(record)
        self.wait_until(lambda: record in self.controller.cache)
        self.assertFalse(QImage.fromData(self.controller.cache[record]).isNull())
        self.assertEqual(self.controller.popup.caption.text(), record.path.name)
        self.assertIsNone(self.controller.process)
        self.assertFalse(self.controller.deadline.isActive())
        self.assertTrue(self.controller.popup.grab().save(str(self.root.parent / "hover-thumbnail.png")))
        self.controller.dismiss()
        self.controller.hover(record)
        self.controller.delay.stop()
        self.controller.load()
        self.assertIsNone(self.controller.process)

    def test_leaving_a_row_cancels_the_pending_preview(self):
        self.controller.hover(self.picture())
        self.controller.hover(None)
        self.assertFalse(self.controller.delay.isActive())
        self.assertFalse(self.controller.popup.isVisible())
        self.assertIsNone(self.controller.record)
        self.assertIsNone(self.controller.process)

    def test_stale_result_is_ignored_even_after_returning_to_same_file(self):
        record = self.picture()
        self.controller.hover(record)
        old_generation = self.controller.generation
        self.controller.dismiss()
        self.controller.hover(record)
        old_process = Mock()
        self.controller.process = old_process
        self.controller.delay.stop()
        with patch.object(self.controller.popup, "display") as display, \
             patch.object(self.controller, "load") as load:
            self.controller.finished(old_process, record, old_generation, 1)
        display.assert_not_called()
        load.assert_called_once()
        self.assertNotIn(record, self.controller.cache)

    def test_slow_helper_is_killed_without_blocking_gui(self):
        record = self.picture()
        self.controller.hover(record)
        self.controller.delay.stop()
        process = QProcess(self.controller)
        self.controller.process = process
        generation = self.controller.generation
        process.finished.connect(lambda code, status: self.controller.finished(process, record, generation, code))
        executable = Path(sys.executable)
        if os.name == "nt":
            executable = executable.with_name("pythonw.exe")
        process.start(str(executable), ["-c", "import time; time.sleep(30)"])
        self.controller.deadline.setInterval(50)
        self.controller.deadline.start()
        self.wait_until(lambda: self.controller.process is None)
        self.assertIn("No thumbnail available", self.controller.popup.note.text())

    def test_changed_file_does_not_show_a_cached_preview(self):
        record = self.picture()
        self.controller.cache[record] = render_thumbnail(record)
        record.path.write_bytes(b"changed")
        self.controller.hover(record)
        self.controller.delay.stop()
        self.controller.load()
        self.assertIn("changed or is unavailable", self.controller.popup.note.text())
        self.assertIsNone(self.controller.process)
