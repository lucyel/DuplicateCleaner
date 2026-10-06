import os
from threading import Event
from time import monotonic, sleep
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtGui import QCloseEvent, QFontDatabase
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from duplicate_cleaner.gui import MainWindow
from duplicate_cleaner.cleanup import recycle_reviewed_files
from duplicate_cleaner.scanner import scan
from duplicate_cleaner.similarity import SimilarResult, scan_similar
from tests.support import FileTestCase
from tests.test_similarity import sample_image
from tests.test_video_similarity import make_video


class SimilarTabTests(FileTestCase):
    def test_tree_gallery_and_image_preview_checks_stay_synchronized(self):
        self.tab.on_result(scan_similar([self.images]))
        self.assertFalse(self.tab.selected)
        group = self.tab.tree.topLevelItem(0)
        self.tab.tree.setCurrentItem(group)
        first = group.child(0).data(0, Qt.ItemDataRole.UserRole)[1].record.path
        group.child(0).setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(self.tab.selected, {first})
        self.assertIn("1 file selected", self.tab.selection_label.text())
        self.assertEqual(self.tab.gallery.item(0).checkState(), Qt.CheckState.Checked)
        self.tab.gallery.item(1).setCheckState(Qt.CheckState.Checked)
        self.assertEqual(group.child(1).checkState(0), Qt.CheckState.Checked)
        self.tab.tree.setCurrentItem(group.child(1))
        self.assertTrue(all(pane.recycle_check.isChecked() for pane in self.tab.panes))
        self.tab.panes[0].recycle_check.setChecked(False)
        self.assertNotIn(first, self.tab.selected)
        self.assertEqual(group.child(0).checkState(0), Qt.CheckState.Unchecked)
        self.tab.clear_button.click()
        self.assertFalse(self.tab.selected)
        self.assertFalse(self.tab.recycle_button.isEnabled())
        self.assertTrue(all(not pane.recycle_check.isChecked() for pane in self.tab.panes))

    def test_group_and_item_menus_match_duplicate_actions(self):
        before = self.old_state()
        self.tab.on_result(scan_similar([self.images]))
        parent = self.tab.tree.topLevelItem(0)
        menu = QMenu(self.tab)
        with patch("duplicate_cleaner.similar_tab.QMenu", return_value=menu), \
                patch.object(menu, "exec", side_effect=lambda position: menu.actions()[-1].trigger()):
            self.tab.show_result_menu(self.tab.tree.visualItemRect(parent).center())
        self.assertEqual(self.tab.selected, set(self.tab.records))
        self.tab.clear_selection()
        menu = QMenu(self.tab)
        def select_folder(position):
            self.assertEqual([action.text() for action in menu.actions() if not action.isSeparator()],
                             ["Open file location", "Select this folder's similar files (keep one file)",
                              "Select all items in this group"])
            menu.actions()[2].trigger()
        with patch("duplicate_cleaner.similar_tab.QMenu", return_value=menu), \
                patch.object(menu, "exec", side_effect=select_folder):
            self.tab.show_result_menu(self.tab.tree.visualItemRect(parent.child(1)).center())
        self.assertEqual(len(self.tab.selected), 1)
        selected = set(self.tab.selected)
        self.tab.select_folder(parent.child(1).data(0, Qt.ItemDataRole.UserRole)[1])
        self.assertEqual(self.tab.selected, selected)
        self.tab.tree.setCurrentItem(parent)
        gallery_menu = QMenu(self.tab)
        with patch("duplicate_cleaner.similar_tab.QMenu", return_value=gallery_menu), \
                patch.object(gallery_menu, "exec", side_effect=lambda position: gallery_menu.actions()[0].trigger()), \
                patch("duplicate_cleaner.gui.QDesktopServices.openUrl", return_value=True) as opened:
            self.tab.show_gallery_menu(self.tab.gallery.visualItemRect(self.tab.gallery.item(0)).center())
        self.assertEqual(opened.call_args.args[0].toLocalFile().replace("/", "\\"), str(self.images))
        self.assertEqual(self.tab.selected, selected)
        self.assertEqual(self.old_state(), before)

    def test_busy_similar_results_reject_every_selection_path(self):
        self.tab.on_result(scan_similar([self.images]))
        parent = self.tab.tree.topLevelItem(0)
        group, image = parent.child(0).data(0, Qt.ItemDataRole.UserRole)
        self.tab.tree.setCurrentItem(parent)
        self.tab.set_busy(True)
        self.tab.set_checked(image.record.path, True)
        self.tab.select_group(group)
        self.tab.select_folder(image)
        parent.child(0).setCheckState(0, Qt.CheckState.Checked)
        self.tab.gallery.item(0).setCheckState(Qt.CheckState.Checked)
        self.assertFalse(self.tab.selected)
        self.assertEqual(parent.child(0).checkState(0), Qt.CheckState.Unchecked)
        self.assertFalse(self.tab.gallery.isEnabled())
        self.assertFalse(self.tab.recycle_button.isEnabled())

    def test_similar_cleanup_confirmation_and_partial_result_are_independent(self):
        before = self.old_state()
        self.tab.on_result(scan_similar([self.images]))
        group = self.tab.result.groups[0]
        selected = group.matches[0][0].record.path
        self.tab.set_checked(selected, True)
        dialogs = []
        def accept(dialog):
            dialogs.append(dialog)
            self.assertEqual(dialog.defaultButton().text(), "Cancel")
            self.assertIn("may contain different content", dialog.informativeText())
            self.assertIn("does not compare audio", dialog.informativeText())
            self.assertIn(str(selected), dialog.detailedText())
            next(button for button in dialog.buttons() if button.text() == "Recycle selected files").click()
            return 0
        with patch.object(QMessageBox, "exec", accept), patch.object(self.window, "start_job") as start:
            self.tab.recycle_button.click()
        job, handler = start.call_args.args[:2]
        def recycler(path, revalidate):
            revalidate()
            path.rename(self.root / "reviewed-fixture.jpg")
        with patch("duplicate_cleaner.gui.recycle_reviewed_files", side_effect=lambda *args, **kwargs:
                   recycle_reviewed_files(*args, recycler=recycler, **kwargs)):
            outcome = job()
        self.assertEqual(outcome.recycled, [selected])
        with patch.object(QMessageBox, "exec", return_value=0):
            handler(outcome)
        self.assertFalse(self.tab.selected)
        self.assertFalse(self.tab.result.groups)
        self.assertNotIn(selected, {image.record.path for image in self.tab.result.images})
        self.assertEqual(self.old_state(), before)
        self.assertTrue(group.reference.record.path.exists())

    def test_recycling_reference_drops_group_but_retains_unmatched_survivors(self):
        self.tab.on_result(scan_similar([self.images]))
        group = self.tab.result.groups[0]
        reference = group.reference.record.path
        self.tab.select_group(group)
        outcome = recycle_reviewed_files(self.tab.records.values(), [reference],
            recycler=lambda path, check: (check(), path.rename(self.root / "reviewed-reference.png")))
        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.on_similar_recycled(outcome)
        self.assertFalse(self.tab.result.groups)
        self.assertEqual({image.record.path for image in self.tab.result.images}, {group.matches[0][0].record.path})
        self.assertFalse(self.tab.selected)

    def test_all_selected_warning_and_cancel_do_not_start_cleanup(self):
        self.tab.on_result(scan_similar([self.images]))
        self.tab.select_group(self.tab.result.groups[0])
        def cancel(dialog):
            self.assertIn("All files selected", dialog.informativeText())
            self.assertIn("No listed file will remain", dialog.informativeText())
            self.assertEqual(dialog.defaultButton().text(), "Cancel")
            return 0
        with patch.object(QMessageBox, "exec", cancel), patch.object(self.window, "start_job") as start:
            self.window.confirm_similar_recycle()
        start.assert_not_called()
        self.assertEqual(self.tab.selected, set(self.tab.records))

    def test_unexpected_cleanup_failure_invalidates_cache_and_clears_checks(self):
        self.tab.on_result(scan_similar([self.images]))
        self.tab.select_group(self.tab.result.groups[0])
        self.window._scan_baselines = {"similarity": self.tab.result}
        self.window._baseline_settings = self.window.current_scan_settings()
        self.tab.set_busy(True)
        with patch.object(QMessageBox, "critical") as message:
            self.window.on_similar_recycle_failure("Unexpected failure")
        self.assertFalse(self.window._scan_baselines)
        self.assertIsNone(self.window._baseline_settings)
        self.assertFalse(self.tab.selected)
        self.assertIn("check the Recycle Bin", self.tab.status.text())
        message.assert_called_once()

    def test_shared_locations_modes_and_types_drive_all_three_results(self):
        self.assertIs(self.window.workflow_tabs.widget(0), self.window.location_page)
        self.assertTrue(self.window.location_page.isAncestorOf(self.window.folders))
        self.assertFalse(hasattr(self.tab, "folders"))
        self.assertFalse(hasattr(self.tab, "scan_button"))
        self.assertFalse(hasattr(self.window, "empty_scan_button"))
        for checkbox in self.window.mode_checks.values():
            self.assertTrue(self.window.criteria_page.isAncestorOf(checkbox))
            checkbox.setChecked(True)
        empty = self.root / "old" / "folders"
        empty.mkdir()
        self.window.start_scan()
        self.assertFalse(self.window.criteria_page.isEnabled())
        self.assertFalse(self.window.folders.isEnabled())
        self.wait_for(lambda: self.window.scan_worker is None, seconds=20)
        self.assertEqual(len(self.window.groups), 1)
        self.assertEqual(len(self.tab.result.groups), 1)
        self.assertEqual([record.path for record in self.window.duplicate_folders], [empty])
        self.assertIn("Similar files: 1 groups", self.window.status.text())
        similarity = self.tab.result
        folders = list(self.window.duplicate_folders)
        self.window.mode_checks["similarity"].setChecked(False)
        self.window.mode_checks["folders"].setChecked(False)
        self.window.file_type_checks["Documents"].setChecked(True)
        self.window.start_scan()
        self.wait_for(lambda: self.window.scan_worker is None)
        self.assertEqual(self.window.scan_file_count, 2)
        self.assertIs(self.tab.result, similarity)
        self.assertEqual(self.window.duplicate_folders, folders)

    def test_file_type_selection_all_multiple_and_none(self):
        self.assertIsNone(self.window.selected_file_types())
        self.window.file_type_checks["Images"].setChecked(True)
        self.assertFalse(self.window.all_file_types.isChecked())
        self.window.file_type_checks["Videos"].setChecked(True)
        self.assertEqual(set(self.window.selected_file_types()), {"Images", "Videos"})
        self.window.all_file_types.setChecked(True)
        self.assertFalse(any(box.isChecked() for box in self.window.file_type_checks.values()))
        self.window.all_file_types.setChecked(False)
        self.assertFalse(self.window.scan_button.isEnabled())
        with patch.object(self.window, "start_job") as start:
            self.window.start_scan()
        start.assert_not_called()
        self.window.mode_checks["similarity"].setChecked(False)
        self.window.mode_checks["folders"].setChecked(True)
        for checkbox in self.window.criteria_checks.values():
            checkbox.setChecked(False)
        self.assertTrue(self.window.scan_button.isEnabled())
        self.assertFalse(self.window.file_types_box.isEnabled())
        self.window.mode_checks["folders"].setChecked(False)
        self.assertFalse(self.window.scan_button.isEnabled())

    def test_scan_uses_a_snapshot_of_shared_settings(self):
        self.window.file_type_checks["Images"].setChecked(True)
        self.window.similarity_preset.setCurrentText("Strict")
        roots = tuple(self.window.folders.item(i).text() for i in range(self.window.folders.count()))
        with patch.object(self.window, "start_job") as start:
            self.window.start_scan()
        self.window.file_type_checks["Videos"].setChecked(True)
        self.window.similarity_preset.setCurrentText("Broad")
        self.window.recursive.setChecked(False)
        self.window.folders.clear()
        with patch("duplicate_cleaner.gui.run_selected_scans") as run:
            start.call_args.args[0]()
        self.assertEqual(run.call_args.args, (roots, True))
        self.assertEqual(run.call_args.kwargs["file_types"], ("Images",))
        self.assertEqual(run.call_args.kwargs["modes"], ("similarity",))
        self.assertEqual(run.call_args.kwargs["preset"], "Strict")

    def test_video_scan_review_and_type_selection_preserve_main_flow(self):
        before = self.old_state()
        original = make_video(self.images)
        make_video(self.images, "copy.mkv", original.path, "scale=160:90,fps=24")
        self.window.add_folder_path(str(self.images))
        self.assertTrue(self.window.all_file_types.isChecked())
        self.window.start_scan()
        self.wait_for(lambda: self.window.scan_worker is None, seconds=30)
        self.assertEqual((self.tab.result.image_count, self.tab.result.video_count), (2, 2))
        self.assertEqual(len(self.tab.result.groups), 2, self.tab.result.issues)
        video_group = self.tab.tree.topLevelItem(1)
        self.assertIn("videos", video_group.text(0))
        self.tab.tree.setCurrentItem(video_group)
        self.wait_for(lambda: len(self.tab.gallery.requested) == 2)
        self.assertEqual(self.tab.gallery.count(), 2)
        self.assertTrue(all(loader.record is None for loader in self.tab.gallery.loaders))
        self.tab.tree.setCurrentItem(video_group.child(1))
        self.assertIs(self.tab.stack.currentWidget(), self.tab.video_review)
        self.assertEqual(self.tab.video_review.samples.count(), 12)
        video_path = video_group.child(1).data(0, Qt.ItemDataRole.UserRole)[1].record.path
        self.tab.video_review.checks[1].setChecked(True)
        self.assertEqual(self.tab.selected, {video_path})
        self.assertEqual(video_group.child(1).checkState(0), Qt.CheckState.Checked)
        self.tab.set_busy(True)
        self.tab.video_review.checks[0].setChecked(True)
        self.assertEqual(self.tab.selected, {video_path})
        self.assertFalse(self.tab.video_review.checks[0].isChecked())
        self.tab.set_busy(False)
        self.assertTrue(all(view.scene().items() for view in self.tab.video_review.views))
        self.tab.video_review.samples.setCurrentIndex(7)
        self.assertIn("audio is not compared", self.tab.video_review.summary.text())
        with patch("duplicate_cleaner.video_review.QDesktopServices.openUrl", return_value=True) as opened:
            self.tab.video_review.open_video(0)
        self.assertEqual(opened.call_args.args[0].toLocalFile(), str(original.path).replace('\\', '/'))
        self.tab.back_button.click()
        self.assertEqual(self.tab.gallery.count(), 2)
        self.assertEqual(self.tab.gallery.item(1).checkState(), Qt.CheckState.Checked)
        self.assertEqual(self.old_state(), before)

        self.window.file_type_checks["Videos"].setChecked(True)
        self.window.start_scan()
        self.wait_for(lambda: self.window.scan_worker is None, seconds=30)
        self.assertEqual(self.tab.result.image_count, 0)
        self.assertEqual(self.tab.result.videos_compared, 2)
        self.assertEqual(self.old_state(), before)
        self.window.workflow_tabs.setCurrentWidget(self.window.duplicate_page)
        self.assertFalse(self.tab.video_review.videos)

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if os.name == "nt":
            for font in ("segoeui.ttf", "segoeuib.ttf"):
                QFontDatabase.addApplicationFont(os.path.join(os.environ["WINDIR"], "Fonts", font))
        cls.app.setStyle("Fusion")

    def setUp(self):
        super().setUp()
        self.addCleanup(self.app.setPalette, self.app.palette())
        self.addCleanup(self.app.setStyleSheet, self.app.styleSheet())
        settings = patch("duplicate_cleaner.gui.QSettings", side_effect=lambda *args: QSettings(
            str(self.root / "preferences.ini"), QSettings.Format.IniFormat))
        settings.start()
        self.addCleanup(settings.stop)
        self.window = MainWindow()
        self.addCleanup(self.dispose_window)
        self.addCleanup(self.stop_worker)
        self.tab = self.window.similar_tab
        self.images = self.root / "images"
        self.images.mkdir()
        image = sample_image()
        self.assertTrue(image.save(str(self.images / "original.png")))
        self.assertTrue(image.scaled(180, 120).save(str(self.images / "resized.jpg"), quality=60))
        self.file("old/one.txt", b"identical old files")
        self.file("old/two.txt", b"identical old files")
        self.window.add_folder_path(str(self.root / "old"))
        self.window.add_folder_path(str(self.images))
        self.window.mode_checks["duplicates"].setChecked(False)
        self.window.mode_checks["similarity"].setChecked(True)
        self.window.on_scan(scan([self.root / "old"]))
        self.window.tree.topLevelItem(0).child(0).setCheckState(0, Qt.CheckState.Checked)
        self.window.show()
        self.window.workflow_tabs.setCurrentWidget(self.tab)
        self.app.processEvents()

    def dispose_window(self):
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait_for(self, condition, seconds=10):
        deadline = monotonic() + seconds
        while not condition() and monotonic() < deadline:
            self.app.processEvents()
            # QTest.qWait can hold the GIL and starve a Python QThread doing frame sampling.
            sleep(.01)
        self.assertTrue(condition(), "Timed out waiting for the UI")

    def stop_worker(self):
        if self.window.scan_worker is not None:
            self.window.cancel_work()
            self.wait_for(lambda: self.window.scan_worker is None)

    def old_state(self):
        window = self.window
        return (list(window.groups), set(window.selected), dict(window.records), window.search_criteria(),
                [window.folders.item(i).text() for i in range(window.folders.count())],
                window.recursive.isChecked(), window.excluded_folder_paths(), window.session_available,
                window.session_path, window.summary_notice)

    def test_scan_and_review_preserve_duplicate_flow(self):
        before = self.old_state()
        self.assertFalse(hasattr(self.tab, "folders"))
        self.assertTrue(self.window.scan_button.isEnabled())
        self.assertFalse(self.window.scan_button.isHidden())
        self.assertFalse(self.window.save_session_button.isHidden())
        self.assertFalse(self.window.load_session_button.isHidden())
        self.assertTrue(self.window.filter_toggle.isHidden())
        self.window.add_folder_path(str(self.images))
        self.window.add_folder_path(str(self.images))  # Repeated choices do not duplicate roots.
        self.assertEqual(self.window.folders.count(), 2)
        self.window.scan_button.click()
        self.assertIsNotNone(self.window.scan_worker)
        self.assertFalse(self.window.scan_button.isEnabled())
        self.wait_for(lambda: self.window.scan_worker is None)
        self.assertEqual(len(self.tab.result.groups), 1, self.tab.status.text())
        self.assertEqual(self.old_state(), before)

        group = self.tab.tree.topLevelItem(0)
        self.tab.tree.setCurrentItem(group)
        self.assertEqual(self.tab.gallery.count(), 2)
        self.assertEqual(self.tab.stack.currentIndex(), 0)
        for i in range(2):
            self.assertEqual(self.tab.gallery.item(i).checkState(), Qt.CheckState.Unchecked)
            self.assertEqual(group.child(i).checkState(0), Qt.CheckState.Unchecked)
        self.tab.tree.setCurrentItem(group.child(1))
        self.assertEqual(self.tab.stack.currentIndex(), 1)
        self.assertNotEqual(self.tab.panes[0].record.path, self.tab.panes[1].record.path)
        self.assertTrue(all(not pane.recycle_check.isHidden() for pane in self.tab.panes))
        self.wait_for(lambda: all(pane.process is None for pane in self.tab.panes))
        self.tab.back_button.click()
        self.assertEqual(self.tab.gallery.count(), 2)
        self.assertEqual(self.old_state(), before)
        self.window.workflow_tabs.setCurrentWidget(self.window.duplicate_page)
        self.assertFalse(self.window.scan_button.isHidden())
        self.assertFalse(self.window.save_session_button.isHidden())
        self.assertFalse(self.window.filter_toggle.isHidden())
        self.assertEqual(self.old_state(), before)
        self.assertEqual(len(self.tab.result.groups), 1)
        self.assertTrue(all(pane.record is None for pane in self.tab.panes))

    def test_shared_cancel_preserves_unselected_duplicate_results(self):
        before = self.old_state()
        started = Event()

        def slow_scan(*args, cancel, **kwargs):
            started.set()
            if not cancel.wait(5):
                raise RuntimeError("Test worker was not cancelled")
            return SimilarResult(cancelled=True)

        with patch("duplicate_cleaner.scan_workflow.scan_similar", side_effect=slow_scan):
            self.window.start_scan()
            self.wait_for(started.is_set)
            self.window.cancel_button.click()
            self.wait_for(lambda: self.window.scan_worker is None)
        self.assertTrue(self.tab.result.cancelled)
        self.assertFalse(self.tab.result.groups)
        self.assertIn("cancelled", self.tab.summary.text())
        self.assertEqual(self.old_state(), before)

    def test_failure_recovers_only_new_controls(self):
        before = self.old_state()
        with patch("duplicate_cleaner.scan_workflow.scan_similar", side_effect=OSError("Read failed")):
            self.window.add_folder_path(str(self.images))
            self.window.start_scan()
            self.wait_for(lambda: self.window.scan_worker is None)
        self.assertTrue(self.window.scan_button.isEnabled())
        self.assertFalse(self.window.cancel_button.isEnabled())
        self.assertIn("Read failed", self.tab.status.text())
        self.assertEqual(self.old_state(), before)

    def test_close_waits_for_similarity_worker(self):
        with patch("duplicate_cleaner.scan_workflow.scan_similar",
                   side_effect=lambda *args, cancel, **kwargs: (cancel.wait(5), SimilarResult(cancelled=True))[1]):
            self.window.add_folder_path(str(self.images))
            self.window.start_scan()
            event = QCloseEvent()
            self.window.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertTrue(self.window.scan_worker.cancel_event.is_set())
            self.wait_for(lambda: self.window.scan_worker is None)
        event = QCloseEvent()
        self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())

    def test_gallery_loads_images_and_clears_helpers_on_tab_switch(self):
        self.tab.on_result(scan_similar([self.images]))
        self.tab.tree.setCurrentItem(self.tab.tree.topLevelItem(0))
        self.wait_for(lambda: len(self.tab.gallery.requested) == 2 and
                      all(loader.record is None and loader.process is None for loader in self.tab.gallery.loaders))
        self.assertTrue(all(self.tab.gallery.item(i).icon().cacheKey() != self.tab.gallery.placeholder.cacheKey()
                            for i in range(2)))
        self.window.workflow_tabs.setCurrentWidget(self.window.duplicate_page)
        self.assertFalse(self.tab.gallery.items)
        self.assertTrue(all(loader.process is None for loader in self.tab.gallery.loaders))
        self.window.workflow_tabs.setCurrentWidget(self.tab)
        self.assertEqual(self.tab.gallery.count(), 2)
