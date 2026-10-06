import os
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QEventLoop, QSettings, Qt, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from duplicate_cleaner.cleanup import recycle_reviewed_files, recycle_selected
from duplicate_cleaner.duplicate_folders import FolderScanResult, recycle_duplicate_folders, scan_duplicate_folders
from duplicate_cleaner.gui import MainWindow
from duplicate_cleaner.models import ScanResult
from duplicate_cleaner.scanner import Reporter, scan
from duplicate_cleaner.scan_workflow import SCAN_MODES
from duplicate_cleaner.similarity import scan_similar
from duplicate_cleaner.sessions import load_session
from tests.support import FileTestCase
from tests.test_similarity import sample_image


class IncrementalScanTests(FileTestCase):
    def complete_scan_for_additions(self, *modes):
        old = self.root / "old"
        old.mkdir()
        for path in (self.root / "first.png", self.root / "copy.png"):
            path.rename(old / path.name)
        self.file("old/unique.txt", b"unique old contents")
        self.window.folders.clear()
        self.window.add_folder_path(str(old))
        self.choose_modes(*(modes or ("duplicates",)))
        self.window.start_scan()
        self.wait_for(lambda: self.window.scan_worker is None)
        self.assertTrue(self.window._scan_baselines)
        return old

    def test_added_scan_keeps_checks_and_matches_old_unique_files(self):
        old = self.complete_scan_for_additions()
        selected = self.check_first_file()
        self.file("new/renamed.txt", b"unique old contents")
        new = self.root / "new"
        self.window.add_folder_path(str(new))
        self.assertTrue(self.window.scan_added_button.isEnabled())
        actual = scan
        roots_scanned = []
        def scanner(roots, *args, **kwargs):
            roots_scanned.append(tuple(roots))
            return actual(roots, *args, **kwargs)
        with patch("duplicate_cleaner.scan_workflow.scan", side_effect=scanner):
            self.window.scan_added_button.click()
            self.wait_for(lambda: self.window.scan_worker is None)
        self.assertEqual(roots_scanned, [(os.path.normcase(str(new)),)])
        self.assertEqual(len(self.window.groups), 2)
        self.assertEqual(self.window.scan_file_count, 4)
        self.assertEqual(self.window.selected, {selected})
        self.assertFalse(self.window.scan_added_button.isEnabled())
        self.assertIn(old / "unique.txt", self.window.records)
        self.assertNotIn(new / "renamed.txt", self.window.selected)

    def test_added_scan_cancellation_keeps_baseline_results_and_checks(self):
        self.complete_scan_for_additions()
        selected = self.check_first_file()
        groups, baseline = list(self.window.groups), self.window._scan_baselines["duplicates"]
        self.file("new/a.txt", b"new content")
        self.window.add_folder_path(str(self.root / "new"))
        entered = Event()
        def blocked(*args, cancel, progress, **kwargs):
            entered.set()
            reporter = Reporter(cancel, progress)
            while True:
                reporter.check()
                cancel.wait(.01)
        with patch("duplicate_cleaner.scan_workflow.scan", side_effect=blocked):
            self.window.start_added_scan()
            self.wait_for(entered.is_set)
            self.assertFalse(self.window.recycle_button.isEnabled())
            self.assertEqual(self.window.groups, groups)
            self.window.pause_button.click()
            self.wait_for(lambda: self.window.status.text().startswith("Scan paused."))
            self.window.cancel_work()
            self.wait_for(lambda: self.window.scan_worker is None)
        self.assertEqual(self.window.groups, groups)
        self.assertEqual(self.window.selected, {selected})
        self.assertIs(self.window._scan_baselines["duplicates"], baseline)
        self.assertTrue(self.window.scan_added_button.isEnabled())

    def test_added_scan_combines_all_modes_and_keeps_existing_checks(self):
        old = self.complete_scan_for_additions(*SCAN_MODES)
        selected = self.check_first_file()
        similar_path = self.window.similar_tab.result.groups[0].matches[0][0].record.path
        self.window.similar_tab.set_checked(similar_path, True)
        for path in old.iterdir():
            self.file("new/" + path.name, path.read_bytes())
        new = self.root / "new"
        self.window.add_folder_path(str(new))
        self.window.start_added_scan()
        self.wait_for(lambda: self.window.scan_worker is None)
        self.assertEqual(self.window.selected, {selected})
        self.assertEqual(self.window.similar_tab.selected, {similar_path})
        self.assertEqual(len(self.window.groups), 2)
        self.assertEqual({folder.path for folder in self.window.duplicate_folders}, {old, new})
        images = self.window._scan_baselines["similarity"].images
        self.assertEqual(len(images), 4)
        self.assertEqual(len(self.window.similar_tab.result.groups[0].matches), 3)
        self.assertEqual(self.window.scan_mode_states, {mode: "Ready" for mode in SCAN_MODES})

    def test_folder_cleanup_prunes_all_modes_before_an_added_scan(self):
        self.complete_scan_for_additions(*SCAN_MODES)
        old = self.root / "old"
        image = (old / "first.png").read_bytes()
        self.file("old/left/image.png", image)
        self.file("old/right/copy.png", image)
        self.window.start_scan()
        self.wait_for(lambda: self.window.scan_worker is None)
        removed = old / "left"
        result = recycle_duplicate_folders(self.window.folder_groups, [removed],
            recycler=lambda path, check: (check(), path.rename(self.root / "recycled-folder-fixture")))
        self.assertEqual(result.recycled, [removed])
        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.on_duplicate_folders_recycled(result)
        self.file("new/new-copy.png", image)
        self.window.add_folder_path(str(self.root / "new"))
        self.window.start_added_scan()
        self.wait_for(lambda: self.window.scan_worker is None)
        removed_file = removed / "image.png"
        self.assertNotIn(removed_file, self.window.records)
        self.assertNotIn(removed, self.window._scan_baselines["folders"].index)
        self.assertNotIn(removed_file, {item.record.path for item in self.window._scan_baselines["similarity"].images})
        self.assertTrue(any({old / "right", self.root / "new"}.issubset(
            {folder.path for folder in group.folders}) for group in self.window.folder_groups))

    def test_failed_later_mode_rolls_back_successful_additions_in_other_modes(self):
        self.complete_scan_for_additions(*SCAN_MODES)
        selected = self.check_first_file()
        similar_path = self.window.similar_tab.result.groups[0].matches[0][0].record.path
        self.window.similar_tab.set_checked(similar_path, True)
        groups, folders = list(self.window.groups), list(self.window.folder_groups)
        baseline = dict(self.window._scan_baselines)
        self.file("new/renamed.txt", b"unique old contents")
        self.window.add_folder_path(str(self.root / "new"))
        with patch("duplicate_cleaner.scan_workflow.scan_duplicate_folders", side_effect=OSError("Test folder failure")):
            self.window.start_added_scan()
            self.wait_for(lambda: self.window.scan_worker is None)
        self.assertEqual(self.window.groups, groups)
        self.assertEqual(self.window.folder_groups, folders)
        self.assertEqual(self.window.selected, {selected})
        self.assertEqual(self.window.similar_tab.selected, {similar_path})
        self.assertEqual(self.window._scan_baselines, baseline)
        self.assertIn("Test folder failure", self.window.status.text())

    def test_added_scan_requires_same_criteria_roots_and_scope(self):
        old = self.complete_scan_for_additions()
        self.file("new/a.txt", b"new")
        self.window.add_folder_path(str(self.root / "new"))
        self.assertTrue(self.window.scan_added_button.isEnabled())
        self.window.criteria_checks["filename"].setChecked(True)
        self.assertFalse(self.window.scan_added_button.isEnabled())
        self.assertIn("settings changed", self.window.scan_added_button.toolTip())
        self.window.criteria_checks["filename"].setChecked(False)
        self.assertTrue(self.window.scan_added_button.isEnabled())
        self.window.folders.item(0).setSelected(True)
        self.window.remove_folders()
        self.assertFalse(self.window.scan_added_button.isEnabled())
        self.window.add_folder_path(str(old))
        self.assertTrue(self.window.scan_added_button.isEnabled())
        self.window.add_folder_path(str(self.root))
        self.assertFalse(self.window.scan_added_button.isEnabled())
        self.assertIn("contains a previously scanned root", self.window.scan_added_button.toolTip())

    def test_file_cleanup_then_addition_never_restores_recycled_records(self):
        self.complete_scan_for_additions()
        selected = self.check_first_file()
        def recycler(path, revalidate):
            revalidate()
            path.rename(self.root / "recycled-fixture.png")
        result = recycle_selected(self.window.groups, [selected], recycler=recycler)
        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.on_recycled(result)
        self.assertNotIn(selected, self.window._scan_baselines["duplicates"].index)
        self.file("new/new-copy.png", (self.root / "recycled-fixture.png").read_bytes())
        self.window.add_folder_path(str(self.root / "new"))
        self.window.start_added_scan()
        self.wait_for(lambda: self.window.scan_worker is None)
        self.assertNotIn(selected, self.window.records)
        self.assertEqual(len(self.window.groups), 1)
        self.assertEqual(len(self.window.groups[0].files), 2)

    def test_full_run_cleanup_is_pruned_before_enabling_added_scans(self):
        old = self.root / "old"
        old.mkdir()
        for path in (self.root / "first.png", self.root / "copy.png"):
            path.rename(old / path.name)
        self.window.folders.clear()
        self.window.add_folder_path(str(old))
        self.choose_modes("duplicates", "folders")
        with patch("duplicate_cleaner.scan_workflow.scan_duplicate_folders", side_effect=self.blocked_empty):
            self.window.start_scan()
            self.wait_for(lambda: self.window.scan_mode_states.get("duplicates") == "Ready")
            selected = self.check_first_file()
            result = recycle_selected(self.window.groups, [selected],
                recycler=lambda path, check: (check(), path.rename(self.root / "recycled-fixture.png")))
            with patch.object(QMessageBox, "exec", return_value=0):
                self.window.on_recycled(result)
            self.release.set()
            self.wait_for(lambda: self.window.scan_worker is None)
        self.assertNotIn(selected, self.window._scan_baselines["duplicates"].index)
        self.assertFalse(self.window._scan_baselines["duplicates"].groups)

    def test_late_similarity_results_do_not_restore_recycled_duplicate_files(self):
        entered = Event()
        self.choose_modes("duplicates", "similarity")
        def delayed(*args, **kwargs):
            result = scan_similar(*args, **kwargs)
            entered.set()
            if not self.release.wait(10):
                raise TimeoutError("Test did not release cached similarity results")
            return result
        with patch("duplicate_cleaner.scan_workflow.scan_similar", side_effect=delayed):
            self.window.start_scan()
            self.wait_for(entered.is_set)
            selected = self.check_first_file()
            result = recycle_selected(self.window.groups, [selected],
                recycler=lambda path, check: (check(), path.rename(self.root / "recycled-fixture.png")))
            with patch.object(QMessageBox, "exec", return_value=0):
                self.window.on_recycled(result)
            self.release.set()
            self.wait_for(lambda: self.window.scan_worker is None)
        self.assertNotIn(selected, self.window.similar_tab.records)
        self.assertNotIn(selected, {image.record.path for image in self.window._scan_baselines["similarity"].images})

    def test_similar_cleanup_prunes_other_tabs_and_late_folder_results(self):
        old = self.complete_scan_for_additions(*SCAN_MODES)
        image = (old / "first.png").read_bytes()
        removed = self.file("old/left/a.png", image)
        self.file("old/right/b.png", image)
        entered = Event()
        def delayed(*args, **kwargs):
            result = scan_duplicate_folders(*args, **kwargs)
            entered.set()
            if not self.release.wait(10):
                raise TimeoutError("Test did not release cached folder results")
            return result
        with patch("duplicate_cleaner.scan_workflow.scan_duplicate_folders", side_effect=delayed):
            self.window.start_scan()
            self.wait_for(entered.is_set)
            self.window.similar_tab.set_checked(removed, True)
            self.assertTrue(self.window.similar_tab.recycle_button.isEnabled())
            result = recycle_reviewed_files(self.window.similar_tab.records.values(), [removed],
                recycler=lambda path, check: (check(), path.rename(self.root / "reviewed-fixture.png")))
            with patch.object(QMessageBox, "exec", return_value=0):
                self.window.on_similar_recycled(result)
            self.assertNotIn(removed, self.window.records)
            self.release.set()
            self.wait_for(lambda: self.window.scan_worker is None)
        self.assertTrue(all(removed != file.path for folder in self.window.duplicate_folders for file in folder.files))
        self.assertNotIn(removed, self.window._scan_baselines["duplicates"].index)
        self.assertNotIn(old / "left", self.window._scan_baselines["folders"].index)
        self.file("new/next.png", image)
        self.window.add_folder_path(str(self.root / "new"))
        self.window.start_added_scan()
        self.wait_for(lambda: self.window.scan_worker is None)
        self.assertNotIn(removed, self.window.records)
        self.assertNotIn(removed, self.window.similar_tab.records)

    def test_settings_restart_waits_for_a_completed_result_operation(self):
        entered = Event()
        finish_action = Event()
        calls = []
        def scanner(roots, recursive, *, cancel, progress, **options):
            calls.append(tuple(roots))
            if len(calls) == 1:
                entered.set()
                reporter = Reporter(cancel, progress)
                while True:
                    reporter.check()
                    cancel.wait(.01)
            return ScanResult()
        def action(**kwargs):
            if not finish_action.wait(10):
                raise TimeoutError("Test did not finish the result operation")
            return "saved"
        completed = []
        self.choose_modes("duplicates")
        try:
            with patch("duplicate_cleaner.scan_workflow.scan", side_effect=scanner):
                self.window.start_scan()
                self.wait_for(entered.is_set)
                self.window.pause_button.click()
                self.wait_for(lambda: self.window.status.text().startswith("Scan paused."))
                self.window.criteria_checks["extension"].setChecked(True)
                self.window.pause_button.click()
                self.window.start_job(action, completed.append)
                self.wait_for(lambda: self.window.scan_worker is None)
                self.assertIsNotNone(self.window.worker)
                self.assertEqual(len(calls), 1)
                finish_action.set()
                self.wait_for(lambda: len(calls) == 2 and self.window.worker is None and self.window.scan_worker is None)
                self.assertEqual(completed, ["saved"])
        finally:
            finish_action.set()

    def test_pause_unlocks_settings_and_unchanged_resume_continues_same_scan(self):
        entered = Event()
        calls = []
        def scanner(roots, recursive, *, cancel, progress, **options):
            calls.append(tuple(roots))
            entered.set()
            reporter = Reporter(cancel, progress)
            while not self.release.is_set():
                reporter.check()
                cancel.wait(.01)
            return ScanResult()
        self.choose_modes("duplicates")
        with patch("duplicate_cleaner.scan_workflow.scan", side_effect=scanner):
            self.window.start_scan()
            self.wait_for(entered.is_set)
            worker = self.window.scan_worker
            self.window.pause_button.click()
            self.wait_for(lambda: self.window.status.text().startswith("Scan paused."))
            self.assertTrue(self.window.criteria_page.isEnabled())
            self.assertTrue(self.window.add_button.isEnabled())
            self.assertTrue(self.window.exclude_button.isEnabled())
            self.assertFalse(self.window.load_session_button.isEnabled())
            self.window.pause_button.click()
            self.assertIs(self.window.scan_worker, worker)
            self.assertFalse(self.window.criteria_page.isEnabled())
            self.release.set()
            self.wait_for(lambda: self.window.scan_worker is None)
            self.assertEqual(len(calls), 1)

    def test_changed_roots_and_criteria_restart_from_new_snapshot_on_resume(self):
        entered = Event()
        calls = []
        extra = self.root / "new-disk"
        extra.mkdir()
        self.file("new-disk/copy-again.png", (self.root / "first.png").read_bytes())
        def scanner(roots, recursive, *, cancel, progress, **options):
            calls.append((tuple(roots), options["criteria"]))
            if len(calls) == 1:
                entered.set()
                reporter = Reporter(cancel, progress)
                while True:
                    reporter.check()
                    cancel.wait(.01)
            return scan(roots, recursive, cancel=cancel, progress=progress, **options)
        self.choose_modes("duplicates")
        with patch("duplicate_cleaner.scan_workflow.scan", side_effect=scanner):
            self.window.start_scan()
            self.wait_for(entered.is_set)
            self.window.pause_button.click()
            self.wait_for(lambda: self.window.status.text().startswith("Scan paused."))
            self.window.add_folder_path(str(extra))
            self.window.criteria_checks["extension"].setChecked(True)
            self.window.pause_button.click()
            self.wait_for(lambda: len(calls) == 2 and self.window.scan_worker is None)
            self.assertEqual(len(calls[0][0]), 1)
            self.assertEqual(calls[1][0], (str(self.root), str(extra)))
            self.assertFalse(calls[0][1].extension)
            self.assertTrue(calls[1][1].extension)
            self.assertEqual(self.window.scan_file_count, 3)
            self.assertEqual(len(self.window.groups[0].files), 3)

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if os.name == "nt":
            for font in ("segoeui.ttf", "segoeuib.ttf"):
                QFontDatabase.addApplicationFont(os.path.join(os.environ["WINDIR"], "Fonts", font))
        cls.app.setStyle("Fusion")

    def setUp(self):
        super().setUp()
        settings = patch("duplicate_cleaner.gui.QSettings", side_effect=lambda *args: QSettings(
            str(self.root / "settings.ini"), QSettings.Format.IniFormat))
        settings.start()
        self.addCleanup(settings.stop)
        path = self.root / "first.png"
        self.assertTrue(sample_image().save(str(path)))
        self.file("copy.png", path.read_bytes())
        self.window = MainWindow()
        self.window.add_folder_path(str(self.root))
        self.release = Event()
        self.addCleanup(self.shutdown)

    def wait_for(self, condition):
        if condition():
            return
        loop = QEventLoop()
        poll = QTimer()
        poll.timeout.connect(lambda: loop.quit() if condition() else None)
        poll.start(10)
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        deadline.start(10_000)
        loop.exec()
        poll.stop()
        deadline.stop()
        self.assertTrue(condition(), "Background operation did not reach the expected state")

    def shutdown(self):
        self.release.set()
        self.window.cancel_work()
        self.wait_for(lambda: self.window.scan_worker is None and self.window.worker is None)
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def choose_modes(self, *modes):
        for mode, checkbox in self.window.mode_checks.items():
            checkbox.setChecked(mode in modes)

    def blocked_empty(self, *args, cancel, **kwargs):
        if not self.release.wait(10):
            raise TimeoutError("Test did not release the empty-folder scanner")
        return FolderScanResult(cancelled=cancel.is_set())

    def start_with_duplicates_ready(self):
        self.choose_modes("duplicates", "folders")
        self.window.start_scan()
        self.wait_for(lambda: self.window.scan_mode_states.get("duplicates") == "Ready")

    def check_first_file(self):
        item = self.window.tree.topLevelItem(0).child(0)
        self.window.tree.setCurrentItem(item)
        item.setCheckState(0, Qt.CheckState.Checked)
        return item.data(0, Qt.ItemDataRole.UserRole).path

    def test_completed_mode_supports_preview_selection_and_save_while_scanning(self):
        with patch("duplicate_cleaner.scan_workflow.scan_duplicate_folders", side_effect=self.blocked_empty):
            self.start_with_duplicates_ready()
            window = self.window
            self.assertIsNotNone(window.scan_worker)
            self.assertTrue(window.tree.isEnabled())
            self.assertFalse(window.duplicate_folder_tree.isEnabled())
            self.assertFalse(window.criteria_page.isEnabled())
            self.assertFalse(window.scan_button.isEnabled())
            self.assertFalse(window.load_session_button.isEnabled())
            self.assertIn("Duplicate folders", window.status.text())
            path = self.check_first_file()
            self.assertTrue(window.recycle_button.isEnabled())
            self.assertTrue(window.select_folder_button.isEnabled())
            self.assertTrue(window.comparison_preview.isEnabled())
            self.assertEqual(len(window.comparison_preview.files), 2)
            groups = list(window.groups)
            destination = self.root / "completed.dupsession"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(destination), "")):
                window.save_session()
            self.assertIsNotNone(window.worker)
            self.wait_for(lambda: window.worker is None)
            self.assertTrue(destination.is_file())
            self.assertIsNotNone(window.scan_worker)
            self.release.set()
            self.wait_for(lambda: window.scan_worker is None)
            self.assertEqual(window.selected, {path})
            self.assertEqual(window.groups, groups)
            self.assertTrue(window.scan_button.isEnabled())

    def test_completed_similarity_can_be_saved_before_folder_scan_finishes(self):
        with patch("duplicate_cleaner.scan_workflow.scan_duplicate_folders", side_effect=self.blocked_empty):
            self.choose_modes("similarity", "folders")
            self.window.start_scan()
            self.wait_for(lambda: self.window.scan_mode_states.get("similarity") == "Ready")
            self.assertIsNotNone(self.window.scan_worker)
            self.assertTrue(self.window.save_session_button.isEnabled())
            self.window.similar_tab.select_group(self.window.similar_tab.result.groups[0])
            selected = set(self.window.similar_tab.selected)
            destination = self.root / "completed-similar.dupsession"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(destination), "")):
                self.window.save_session()
            self.wait_for(lambda: self.window.worker is None)
            self.assertTrue(destination.is_file())
            self.assertIsNotNone(self.window.scan_worker)
            loaded = load_session(destination).data
            self.assertFalse(loaded.duplicate_available)
            self.assertIsNone(loaded.folder_result)
            self.assertEqual(loaded.similar_result.groups, self.window.similar_tab.result.groups)
            self.assertEqual(loaded.selected_similar, selected)
            self.release.set()
            self.wait_for(lambda: self.window.scan_worker is None)
            self.assertEqual(self.window.similar_tab.selected, selected)

    def test_recycling_completed_mode_is_not_undone_by_final_scan_result(self):
        def recycler(path, revalidate):
            revalidate()
            path.unlink()  # Only the disposable fixture inside .verification.

        def cleanup(groups, selected, **kwargs):
            return recycle_selected(groups, selected, recycler=recycler, **kwargs)

        def accept(dialog):
            for button in dialog.buttons():
                if button.text() == "Recycle selected files":
                    button.click()
            return 0

        with patch("duplicate_cleaner.scan_workflow.scan_duplicate_folders", side_effect=self.blocked_empty), \
                patch("duplicate_cleaner.gui.recycle_selected", side_effect=cleanup), \
                patch.object(QMessageBox, "exec", accept):
            self.start_with_duplicates_ready()
            path = self.check_first_file()
            self.window.confirm_recycle()
            self.wait_for(lambda: self.window.worker is None)
            self.assertFalse(path.exists())
            self.assertFalse(self.window.groups)
            self.assertIsNotNone(self.window.scan_worker)
            self.release.set()
            self.wait_for(lambda: self.window.scan_worker is None)
            self.assertFalse(self.window.groups)
            self.assertFalse(self.window.selected)

    def test_cancellation_keeps_completed_results_and_checks(self):
        with patch("duplicate_cleaner.scan_workflow.scan_duplicate_folders", side_effect=self.blocked_empty):
            self.start_with_duplicates_ready()
            path = self.check_first_file()
            self.window.cancel_work()
            self.assertTrue(self.window.scan_worker.cancel_event.is_set())
            self.assertTrue(self.window.tree.isEnabled())
            self.release.set()
            self.wait_for(lambda: self.window.scan_worker is None)
            self.assertEqual(self.window.selected, {path})
            self.assertEqual(len(self.window.groups), 1)
            self.assertEqual(self.window.scan_mode_states, {"duplicates": "Ready", "folders": "Cancelled"})

    def test_similarity_results_unlock_before_empty_scan_finishes(self):
        self.choose_modes("similarity", "folders")
        with patch("duplicate_cleaner.scan_workflow.scan_duplicate_folders", side_effect=self.blocked_empty):
            self.window.start_scan()
            self.wait_for(lambda: self.window.scan_mode_states.get("similarity") == "Ready")
            self.assertIsNotNone(self.window.scan_worker)
            self.assertTrue(self.window.similar_tab.tree.isEnabled())
            self.assertEqual(self.window.similar_tab.tree.topLevelItemCount(), 1)
            self.assertFalse(self.window.duplicate_folder_tree.isEnabled())
            self.window.workflow_tabs.setCurrentWidget(self.window.similar_tab)
            self.window.similar_tab.tree.setCurrentItem(self.window.similar_tab.tree.topLevelItem(0))
            self.assertIsNotNone(self.window.similar_tab.current_group)
            self.release.set()
            self.wait_for(lambda: self.window.scan_worker is None)
            self.assertIsNotNone(self.window.similar_tab.current_group)

    def test_failure_in_one_mode_does_not_block_other_mode_results(self):
        self.choose_modes(*SCAN_MODES)
        with patch("duplicate_cleaner.scan_workflow.scan_similar", side_effect=OSError("test failure")), \
                patch("duplicate_cleaner.scan_workflow.scan_duplicate_folders", side_effect=self.blocked_empty):
            self.window.start_scan()
            self.wait_for(lambda: self.window.scan_mode_states.get("similarity") == "Failed")
            self.assertTrue(self.window.tree.isEnabled())
            path = self.check_first_file()
            self.assertIn("test failure", self.window.similar_tab.status.text())
            self.release.set()
            self.wait_for(lambda: self.window.scan_worker is None)
            self.assertEqual(self.window.selected, {path})
            self.assertEqual(self.window.scan_mode_states["folders"], "Ready")

    def test_empty_results_unlock_while_another_mode_is_still_pending(self):
        folder = self.root / "folders"
        folder.mkdir()
        # Exercise the same per-mode contract independently of the default run order.
        self.window.scan_worker = SimpleNamespace(cancel_event=Event())
        self.window.scan_mode_states = {"folders": "Scanning", "similarity": "Waiting"}
        try:
            self.window.on_mode_finished("folders", scan_duplicate_folders([self.root]))
            next(self.window.duplicate_folder_items()).setCheckState(0, Qt.CheckState.Checked)
            self.assertTrue(self.window.duplicate_folder_tree.isEnabled())
            self.assertTrue(self.window.recycle_duplicate_folders_button.isEnabled())
            self.assertFalse(self.window.similar_tab.tree.isEnabled())
        finally:
            self.window.scan_worker = None

    def test_close_requests_cancellation_and_waits_for_background_scan(self):
        with patch("duplicate_cleaner.scan_workflow.scan_duplicate_folders", side_effect=self.blocked_empty):
            self.start_with_duplicates_ready()
            self.assertFalse(self.window.close())
            self.assertTrue(self.window.scan_worker.cancel_event.is_set())
            self.release.set()
            self.wait_for(lambda: self.window.scan_worker is None)
            self.assertTrue(self.window.close())

    def test_finishing_scan_does_not_finish_or_replace_an_active_result_operation(self):
        finish_action = Event()
        completed = []

        def action(**kwargs):
            if not finish_action.wait(10):
                raise TimeoutError("Test did not release the result operation")
            return "saved"

        try:
            with patch("duplicate_cleaner.scan_workflow.scan_duplicate_folders", side_effect=self.blocked_empty):
                self.start_with_duplicates_ready()
                self.window.start_job(action, completed.append)
                worker = self.window.worker
                self.window.status.setText("Saving completed results")
                self.release.set()
                self.wait_for(lambda: self.window.scan_worker is None)
                self.assertIs(self.window.worker, worker)
                self.assertFalse(self.window.scan_button.isEnabled())
                self.assertTrue(self.window.cancel_button.isEnabled())
                self.assertEqual(self.window.status.text(), "Saving completed results")
                finish_action.set()
                self.wait_for(lambda: self.window.worker is None)
                self.assertEqual(completed, ["saved"])
                self.assertTrue(self.window.scan_button.isEnabled())
        finally:
            finish_action.set()
