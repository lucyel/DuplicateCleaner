import json
import os
import struct
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from time import monotonic
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QProcess, QSettings, Qt
from PySide6.QtGui import QColor, QFontDatabase, QImage, QPainter, QPolygon, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from duplicate_cleaner.files import capture
from duplicate_cleaner.gui import MainWindow
from duplicate_cleaner.preview import render_preview
from duplicate_cleaner.scanner import scan
from tests.support import FileTestCase


class PreviewTests(FileTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if os.name == "nt":
            for font in ("segoeui.ttf", "segoeuib.ttf"):
                QFontDatabase.addApplicationFont(os.path.join(os.environ["WINDIR"], "Fonts", font))
        cls.app.setStyle("Fusion")

    def setUp(self):
        super().setUp()
        self.image = QImage(900, 600, QImage.Format.Format_RGB32)
        self.image.fill(QColor("#8bc7e8"))
        painter = QPainter(self.image)
        painter.setBrush(QColor("#ffd879"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(640, 70, 110, 110)
        painter.setBrush(QColor("#537763"))
        painter.drawPolygon(QPolygon([QPoint(0, 600), QPoint(340, 190), QPoint(750, 600)]))
        painter.setBrush(QColor("#33554b"))
        painter.drawPolygon(QPolygon([QPoint(300, 600), QPoint(670, 260), QPoint(900, 600)]))
        painter.end()
        first = self.root / "Photos" / "Mountain.png"
        first.parent.mkdir()
        self.assertTrue(self.image.save(str(first)))
        self.file("Backup/Mountain copy.png", first.read_bytes())
        self.file("Archive/Mountain archive.bin", first.read_bytes())
        settings = patch("duplicate_cleaner.gui.QSettings", side_effect=lambda *args: QSettings(
            str(self.root / "preferences.ini"), QSettings.Format.IniFormat))
        settings.start()
        self.addCleanup(settings.stop)
        self.window = MainWindow()
        self.addCleanup(self.window.close)
        self.window.add_folder_path(str(first.parent))
        self.window.on_scan(scan([self.root]))
        self.preview = self.window.comparison_preview

    def wait_until(self, condition):
        deadline = monotonic() + 12
        while not condition() and monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(condition(), "Preview did not complete within the test deadline")

    def select_file(self, group_row=False):
        item = self.window.tree.topLevelItem(0)
        self.window.tree.setCurrentItem(item if group_row else item.child(0))
        return item

    def wait_for_images(self):
        self.wait_until(lambda: all(pane.stack.currentWidget() is pane.view for pane in self.preview.panes))

    def test_group_and_file_selection_show_distinct_copies_without_checking(self):
        group = self.select_file(group_row=True)
        self.assertFalse(self.window.details_panel.isHidden())
        gallery = self.preview.gallery
        self.assertIs(self.preview.stack.currentWidget(), gallery)
        self.assertEqual(gallery.count(), 3)
        self.assertEqual({gallery.item(i).data(Qt.ItemDataRole.UserRole) for i in range(gallery.count())},
                         set(self.window.groups[0].files))
        item = group.child(2)
        self.window.tree.setCurrentItem(item)
        self.assertEqual(self.preview.panes[0].record, item.data(0, Qt.ItemDataRole.UserRole))
        self.assertNotEqual(self.preview.panes[0].record, self.preview.panes[1].record)
        self.assertFalse(self.window.selected)

    def test_two_copy_group_and_keyboard_selection(self):
        group = self.window.groups[0]
        self.window.set_groups([replace(group, files=group.files[:2])])
        item = self.select_file(group_row=True)
        self.assertEqual(len(self.preview.files), 2)
        self.window.show()
        self.window.tree.setFocus()
        QTest.keyClick(self.window.tree, Qt.Key.Key_Down)
        self.assertEqual(self.window.tree.currentItem(), item.child(0))
        self.assertEqual(self.preview.panes[0].record, item.child(0).data(0, Qt.ItemDataRole.UserRole))
        self.assertNotEqual(self.preview.panes[0].record, self.preview.panes[1].record)
        self.assertFalse(self.window.selected)

    def test_pair_choosers_swap_and_checkbox_updates_preserve_pair(self):
        group = self.select_file()
        left, right = self.preview.panes
        left.chooser.setCurrentIndex(2)
        self.assertEqual(left.record, self.preview.files[2])
        previous = left.record
        left.chooser.setCurrentIndex(right.chooser.currentIndex())
        self.assertEqual(right.record, previous)
        pair = (left.record, right.record)
        item = next(group.child(i) for i in range(group.childCount())
                    if group.child(i).data(0, Qt.ItemDataRole.UserRole) == left.record)
        item.setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual((left.record, right.record), pair)
        self.assertIn("Selected for recycling", left.info.text())

    def test_file_type_filter_still_previews_another_copy_outside_filter(self):
        index = next(i for i in range(self.window.type_tabs.count())
                     if self.window.type_tabs.tabText(i) == "Images")
        self.window.type_tabs.setCurrentIndex(index)
        group = self.window.tree.topLevelItem(0)
        item = next(group.child(i) for i in range(group.childCount()) if not group.child(i).isHidden())
        self.window.tree.setCurrentItem(item)
        self.assertEqual(self.preview.panes[1].record.path.suffix, ".bin")
        self.assertIn("Archive", self.preview.panes[1].path.toolTip())

    def test_helper_loads_native_resolution_images_and_zoom_resets(self):
        self.window.show()
        self.select_file()
        self.wait_for_images()
        for pane in self.preview.panes:
            self.assertEqual(pane.view.sceneRect().width(), 900)
            self.assertEqual(pane.view.sceneRect().height(), 600)
            pane.actual_button.click()
            self.assertEqual(pane.view.transform().m11(), 1)
            pane.view.horizontalScrollBar().setValue(0)
            QTest.mousePress(pane.view.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(90, 80))
            QTest.mouseMove(pane.view.viewport(), QPoint(30, 30))
            QTest.mouseRelease(pane.view.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
            self.assertGreater(pane.view.horizontalScrollBar().value(), 0)
            event = QWheelEvent(QPointF(50, 50), QPointF(50, 50), QPoint(), QPoint(0, 120),
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(pane.view.viewport(), event)
            self.assertGreater(pane.view.transform().m11(), 1)
            pane.fit_button.click()
            self.assertTrue(pane.view.fitted)
            self.assertLess(pane.view.transform().m11(), 1)

    def test_rapid_selection_discards_previous_load(self):
        self.select_file()
        pane = self.preview.panes[0]
        pane.delay.stop()
        pane.load()
        self.assertIsNotNone(pane.process)
        replacement = self.root / "replacement.png"
        image = QImage(80, 120, QImage.Format.Format_RGB32)
        image.fill(QColor("#ff0000"))
        self.assertTrue(image.save(str(replacement)))
        record = capture(replacement)
        pane.set_record(record)
        self.wait_until(lambda: pane.stack.currentWidget() is pane.view and pane.process is None)
        self.assertEqual(pane.record, record)
        rendered = pane.view.scene().items()[0].pixmap().toImage()
        self.assertEqual(rendered.size(), image.size())
        self.assertEqual(rendered.pixelColor(40, 60).name(), "#ff0000")

    def test_changed_file_is_rejected_and_other_pane_still_loads(self):
        self.select_file()
        left, right = self.preview.panes
        left.record.path.write_bytes(b"changed after scan")
        self.wait_until(lambda: "changed" in left.message.text().lower())
        self.wait_until(lambda: right.stack.currentWidget() is right.view)
        self.assertTrue(left.view.sceneRect().isEmpty())

    def test_change_during_decode_is_rejected_on_completion(self):
        self.select_file()
        pane = self.preview.panes[0]
        pane.delay.stop()
        pane.load()
        pane.record.path.write_bytes(b"changed before decode completes")
        self.wait_until(lambda: pane.process is None)
        self.assertIs(pane.stack.currentWidget(), pane.placeholder)
        self.assertTrue(pane.view.sceneRect().isEmpty())

    def test_timeout_stops_helper_without_retrying(self):
        self.select_file()
        pane = self.preview.panes[0]
        pane.delay.stop()
        process = QProcess(pane)
        pane.process = process
        record, generation = pane.record, pane.generation
        process.finished.connect(lambda code, status: pane.finished(process, record, generation, code))
        process.start(sys.executable, ["-c", "import time; time.sleep(30)"])
        pane.deadline.setInterval(50)
        pane.deadline.start()
        self.wait_until(lambda: pane.process is None)
        self.assertIn("timed out", pane.message.text())
        QTest.qWait(150)
        self.assertIsNone(pane.process)
        self.assertFalse(pane.delay.isActive())

    def test_hide_tab_change_and_results_refresh_release_previews(self):
        group = self.select_file()
        self.window.preview_toggle.click()
        self.assertTrue(self.window.details_panel.isHidden())
        self.assertFalse(self.preview.files)
        self.window.preview_toggle.click()
        self.assertTrue(self.preview.files)
        self.window.workflow_tabs.setCurrentIndex(1)
        self.assertFalse(self.preview.files)
        self.window.workflow_tabs.setCurrentIndex(0)
        self.assertTrue(self.preview.files)
        group.child(0).setCheckState(0, Qt.CheckState.Checked)
        selected = set(self.window.selected)
        self.window.tree.clearSelection()
        self.window.tree.setCurrentItem(None)
        self.assertFalse(self.preview.files)
        self.assertEqual(self.window.selected, selected)
        self.window.set_groups(self.window.groups)
        self.assertFalse(self.preview.files)

    def test_native_reader_handles_exif_orientation(self):
        path = self.root / "rotated.jpg"
        self.assertTrue(self.image.save(str(path)))
        exif = b"Exif\x00\x00II" + struct.pack("<HIH", 42, 8, 1)
        exif += struct.pack("<HHIHHI", 0x112, 3, 1, 6, 0, 0)
        data = path.read_bytes()
        path.write_bytes(data[:2] + b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif + data[2:])
        metadata, payload = render_preview(capture(path))
        rendered = QImage.fromData(payload)
        self.assertEqual((metadata["width"], metadata["height"]), (600, 900))
        self.assertEqual((rendered.width(), rendered.height()), (600, 900))

    def test_large_image_uses_labeled_reduced_preview(self):
        path = self.root / "large.jpg"
        image = QImage(6000, 4500, QImage.Format.Format_RGB32)
        image.fill(QColor("#5289b0"))
        self.assertTrue(image.save(str(path)))
        metadata, payload = render_preview(capture(path))
        self.assertTrue(metadata["reduced"])
        self.assertEqual((metadata["width"], metadata["height"]), (6000, 4500))
        rendered = QImage.fromData(payload)
        self.assertLessEqual(max(rendered.width(), rendered.height()), 4096)
        pane = self.preview.panes[0]
        pane.set_record(capture(path))
        self.wait_until(lambda: pane.stack.currentWidget() is pane.view)
        self.assertIn("Reduced preview", pane.info.text())
        self.assertFalse(pane.actual_button.isEnabled())

    def test_unsupported_file_shows_fallback_and_can_be_opened(self):
        record = capture(self.file("data.unknown", b"not a supported image"))
        pane = self.preview.panes[0]
        pane.set_record(record)
        self.wait_until(lambda: "unavailable for this file type" in pane.message.text())
        with patch("duplicate_cleaner.preview.QDesktopServices.openUrl", return_value=True) as opener:
            pane.open_button.click()
        self.assertEqual(opener.call_args.args[0].toLocalFile(), str(record.path).replace("\\", "/"))
        record.path.write_bytes(b"changed")
        with patch("duplicate_cleaner.preview.QDesktopServices.openUrl") as opener:
            pane.open_button.click()
        opener.assert_not_called()

    def test_corrupt_and_missing_images_do_not_keep_previous_picture(self):
        self.select_file()
        self.wait_for_images()
        pane = self.preview.panes[0]
        corrupt = capture(self.file("corrupt.png", b"\x89PNG\r\n\x1a\ntruncated image"))
        pane.set_record(corrupt)
        self.assertTrue(pane.view.sceneRect().isEmpty())
        self.wait_until(lambda: "unavailable" in pane.message.text().lower())
        missing = capture(self.file("missing.png", b"temporary"))
        missing.path.unlink()
        pane.set_record(missing)
        self.wait_until(lambda: "unavailable" in pane.message.text().lower())
        self.assertTrue(pane.view.sceneRect().isEmpty())

    def test_two_image_layout_at_small_and_large_window_sizes(self):
        self.window.show()
        self.select_file()
        self.wait_for_images()
        for width, height in ((940, 720), (1220, 800), (1500, 960)):
            self.window.resize(width, height)
            QTest.qWait(60)
            self.assertTrue(self.window.grab().save(str(self.root.parent / f"comparison-{width}.png")))
            for pane in self.preview.panes:
                self.assertGreaterEqual(pane.view.viewport().width(), 100)
                self.assertGreaterEqual(pane.view.height(), 140)
                self.assertGreaterEqual(pane.view.viewport().height(), 120)
                self.assertTrue(pane.rect().contains(pane.stack.geometry()),
                                f"pane={pane.rect()}, image={pane.stack.geometry()}, "
                                f"panel={self.window.details_panel.size()}, preview={self.preview.size()}, "
                                f"minimum={self.window.details_panel.minimumSizeHint()}")

    def test_portable_preview_helper(self):
        executable = os.environ.get("DUPLICATE_CLEANER_EXE")
        if not executable:
            self.skipTest("Set DUPLICATE_CLEANER_EXE to test the packaged preview helper")
        record = self.window.groups[0].files[0]
        environment = os.environ.copy()
        environment["PATH"] = str(Path(os.environ["WINDIR"]) / "System32")
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        for preview_size, changed in ((None, False), (320, False), (None, True)):
            if changed:
                record.path.write_bytes(b"changed")
            request = asdict(record)
            if preview_size:
                request["preview_size"] = preview_size
            result = subprocess.run([str(Path(executable).resolve()), "--render-preview"],
                                    input=json.dumps(request, default=str).encode("utf-8"),
                                    capture_output=True, timeout=30, env=environment, cwd=self.root,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            header, payload = result.stdout.split(b"\n", 1)
            metadata = json.loads(header)
            if changed:
                self.assertIn("error", metadata)
                self.assertFalse(payload)
            else:
                self.assertEqual(metadata["kind"], "image")
                image = QImage.fromData(payload)
                self.assertFalse(image.isNull())
                if preview_size:
                    self.assertLessEqual(max(image.width(), image.height()), preview_size)

    def test_group_gallery_loads_all_visible_files_and_removes_group_labels(self):
        self.window.resize(1500, 1000)
        self.window.show()
        group = self.select_file(group_row=True)
        gallery = self.preview.gallery
        self.wait_until(lambda: bool(gallery.visible) and all(
            gallery.item(i).icon().cacheKey() != gallery.file_icon.cacheKey() for i in gallery.visible))
        self.assertEqual(gallery.count(), 3)
        self.assertEqual([group.text(column) for column in (1, 2, 3)], ["", "", ""])
        self.window.filter_results()
        self.assertEqual([group.text(column) for column in (1, 2, 3)], ["", "", ""])
        group.child(0).setCheckState(0, Qt.CheckState.Checked)
        checked = group.child(0).data(0, Qt.ItemDataRole.UserRole)
        index = gallery.files.index(checked)
        self.assertIn("Selected for recycling", gallery.item(index).text())
        self.assertTrue(self.window.grab().save(str(self.root.parent / "group-gallery.png")))

    def test_gallery_scroll_loads_later_files_and_releases_hidden_images(self):
        payload = self.window.groups[0].files[0].path.read_bytes()
        for i in range(20):
            self.file(f"Copies/Copy {i:02}.png", payload)
        self.window.on_scan(scan([self.root]))
        self.window.show()
        self.select_file(group_row=True)
        gallery = self.preview.gallery
        self.assertEqual(gallery.count(), 23)
        self.wait_until(lambda: gallery.item(0).icon().cacheKey() != gallery.file_icon.cacheKey())
        self.assertLess(len(gallery.requested), gallery.count())
        gallery.scrollToItem(gallery.item(gallery.count() - 1))
        self.wait_until(lambda: gallery.item(gallery.count() - 1).icon().cacheKey() != gallery.file_icon.cacheKey())
        self.assertEqual(gallery.item(0).icon().cacheKey(), gallery.file_icon.cacheKey())
        self.assertLessEqual(sum(loader.process is not None for loader in gallery.loaders), 2)
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(0).child(0))
        self.assertEqual(gallery.count(), 0)
        self.assertTrue(all(loader.record is None for loader in gallery.loaders))

    def test_gallery_decoder_limits_image_size(self):
        record = self.window.groups[0].files[0]
        metadata, data = render_preview(record, 320)
        image = QImage.fromData(data)
        self.assertLessEqual(max(image.width(), image.height()), 320)
        self.assertEqual((metadata["width"], metadata["height"]), (900, 600))

    def test_gallery_handles_missing_files_and_replaces_old_group_images(self):
        self.window.show()
        self.select_file(group_row=True)
        gallery = self.preview.gallery
        gallery.files[0].path.unlink()
        self.wait_until(lambda: "cannot find" in gallery.item(0).toolTip().lower()
                        or "no such file" in gallery.item(0).toolTip().lower())
        self.wait_until(lambda: gallery.item(1).icon().cacheKey() != gallery.file_icon.cacheKey())
        replacement = self.root / "New group"
        replacement.mkdir()
        image = QImage(80, 60, QImage.Format.Format_RGB32)
        image.fill(QColor("#ff0000"))
        image.save(str(replacement / "red.png"))
        self.file("New group/red copy.png", (replacement / "red.png").read_bytes())
        self.window.on_scan(scan([replacement]))
        self.select_file(group_row=True)
        self.wait_until(lambda: gallery.item(0).icon().cacheKey() != gallery.file_icon.cacheKey())
        self.assertEqual(gallery.count(), 2)
        for i in range(2):
            self.assertEqual(gallery.item(i).data(Qt.ItemDataRole.UserRole).path.parent, replacement)
        preview = gallery.item(0).icon().pixmap(80, 60).toImage()
        self.assertEqual(preview.pixelColor(40, 30).name(), "#ff0000")
