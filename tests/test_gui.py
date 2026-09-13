import os
from dataclasses import replace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QSettings, Qt, QTimer
from PySide6.QtGui import QDesktopServices, QFontDatabase, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox

from duplicate_cleaner.cleanup import recycle_selected
from duplicate_cleaner.empty_folders import (EmptyFolderRecycleResult, recycle_empty_folders,
                                             scan_empty_folders)
from duplicate_cleaner.gui import MainWindow, STYLE
from duplicate_cleaner.models import Issue, RecycleResult, format_bytes
from duplicate_cleaner.scanner import scan
from duplicate_cleaner.sessions import LoadResult, SaveResult, SessionData
from tests.support import FileTestCase


class GuiTests(FileTestCase):
    def test_icon_buttons_have_accessible_action_names_in_both_themes(self):
        for dark in (True, False):
            self.window.dark_mode.setChecked(dark)
            self.assertEqual(self.window.dark_mode.text(), "")
            self.assertFalse(self.window.dark_mode.icon().isNull())
            self.assertEqual(self.window.dark_mode.accessibleName(),
                             "Switch to light mode" if dark else "Switch to dark mode")
            self.assertFalse(self.window.sidebar_toggle.icon().isNull())
            self.assertEqual(self.window.sidebar_toggle.arrowType(), Qt.ArrowType.NoArrow)

    def test_exclusion_picker_and_removal_control_scan_scope(self):
        folder = str(self.root / "Backup")
        with patch.object(QFileDialog, "getExistingDirectory", return_value=folder):
            self.window.choose_excluded_folder()
            self.window.choose_excluded_folder()
        self.assertEqual(self.window.excluded_folder_paths(), (folder,))
        for method, scanner in ((self.window.start_scan, "scan"),
                                (self.window.start_empty_folder_scan, "scan_empty_folders")):
            with patch.object(self.window, "start_job") as start:
                method()
            with patch(f"duplicate_cleaner.gui.{scanner}") as scan_job:
                start.call_args.args[0]()
            self.assertEqual(scan_job.call_args.kwargs["excluded_folders"], (folder,))
        self.window.excluded_folders.item(0).setSelected(True)
        self.assertTrue(self.window.remove_exclusion_button.isEnabled())
        self.window.remove_exclusion_button.click()
        self.assertEqual(self.window.excluded_folder_paths(), ())

    def test_exclusions_must_be_subfolders_and_follow_removed_roots(self):
        with patch.object(QMessageBox, "warning") as warning:
            self.window.add_excluded_folder_path(self.root)
            self.window.add_excluded_folder_path(self.root.parent)
        self.assertEqual(warning.call_count, 2)
        self.assertEqual(self.window.excluded_folder_paths(), ())
        self.window.add_excluded_folder_path(self.root / "Backup")
        self.window.folders.item(0).setSelected(True)
        self.window.remove_folders()
        self.assertEqual(self.window.excluded_folder_paths(), ())
        self.assertFalse(self.window.exclude_button.isEnabled())

    def test_exclusion_is_kept_when_another_scan_root_still_contains_it(self):
        self.window.add_folder_path(self.root / "Backup")
        folder = self.root / "Backup" / "nested"
        self.window.add_excluded_folder_path(folder)
        self.window.folders.item(0).setSelected(True)
        self.window.remove_folders()
        self.assertEqual(self.window.excluded_folder_paths(), (str(folder),))

    def test_exclusion_controls_are_disabled_during_operations(self):
        self.window.add_excluded_folder_path(self.root / "Backup")
        self.window.excluded_folders.item(0).setSelected(True)
        self.window.worker = Mock()
        try:
            self.window.update_actions()
            for widget in (self.window.excluded_folders, self.window.exclude_button,
                           self.window.remove_exclusion_button):
                self.assertFalse(widget.isEnabled())
        finally:
            self.window.worker = None
            self.window.update_actions()

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if os.name == "nt":
            for font in ("segoeui.ttf", "segoeuib.ttf"):
                QFontDatabase.addApplicationFont(os.path.join(os.environ["WINDIR"], "Fonts", font))
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        super().setUp()
        self.addCleanup(self.app.setPalette, self.app.palette())
        self.addCleanup(self.app.setStyleSheet, self.app.styleSheet())
        settings = patch("duplicate_cleaner.gui.QSettings", side_effect=lambda *args: QSettings(
            str(self.root / "preferences.ini"), QSettings.Format.IniFormat))
        settings.start()
        self.addCleanup(settings.stop)
        self.file("Documents/meeting-notes.txt", b"meeting notes\n" * 100)
        self.file("Backup/meeting-notes-copy.txt", b"meeting notes\n" * 100)
        self.file("Archive/meeting-notes.txt", b"meeting notes\n" * 100)
        self.window = MainWindow()
        self.addCleanup(self.window.settings.sync)
        self.window.add_folder_path(str(self.root))
        self.window.on_scan(scan([self.root]))
        self.addCleanup(self.window.close)

    def test_switching_theme_preserves_results_checks_and_file_details(self):
        self.window.show()
        self.app.processEvents()
        self.assertFalse(self.window.dark_mode.isChecked())
        light_background = self.app.palette().color(QPalette.ColorRole.Window).lightness()
        item = self.window.tree.topLevelItem(0).child(0)
        item.setCheckState(0, Qt.CheckState.Checked)
        self.window.tree.setCurrentItem(item)
        groups = list(self.window.groups)
        selected = set(self.window.selected)
        details = self.window.details_text.toPlainText()

        QTest.mouseClick(self.window.dark_mode, Qt.MouseButton.LeftButton)
        self.assertTrue(self.window.dark_mode.isChecked())
        self.assertLess(self.app.palette().color(QPalette.ColorRole.Window).lightness(), light_background)
        QTest.mouseClick(self.window.dark_mode, Qt.MouseButton.LeftButton)
        self.assertFalse(self.window.dark_mode.isChecked())
        self.assertEqual(self.app.palette().color(QPalette.ColorRole.Window).lightness(), light_background)
        self.assertEqual(self.window.groups, groups)
        self.assertEqual(self.window.selected, selected)
        self.assertEqual(self.window.details_text.toPlainText(), details)
        self.assertTrue(self.window.recycle_button.isEnabled())

    def test_theme_preference_is_restored_on_next_window(self):
        for dark in (True, False):
            with self.subTest(dark=dark):
                self.window.dark_mode.setChecked(dark)
                self.window.settings.sync()
                reopened = MainWindow()
                try:
                    self.assertEqual(reopened.dark_mode.isChecked(), dark)
                    self.assertEqual(reopened.settings.value("appearance/dark_mode", type=bool), dark)
                finally:
                    reopened.close()

    def saved_session_data(self, selected=(), active_tab="All"):
        return SessionData(
            tuple(self.window.groups), frozenset(selected), (str(self.root),), True,
            tuple(self.window.issues), 3, 4_200, active_tab, "2026-09-03T10:00:00+07:00",
        )

    def test_save_session_sends_complete_snapshot_to_background_worker(self):
        self.window.add_excluded_folder_path(self.root / "Backup")
        item = self.window.tree.topLevelItem(0).child(0)
        item.setCheckState(0, Qt.CheckState.Checked)
        destination = self.root / "work.dupsession"
        with patch.object(QFileDialog, "getSaveFileName",
                          return_value=(str(destination), "Duplicate Cleaner sessions")), \
             patch.object(self.window, "start_job") as start:
            self.window.save_session()
        start.assert_called_once()
        with patch("duplicate_cleaner.gui.save_session_file") as save:
            start.call_args.args[0]()
        data = save.call_args.args[1]
        self.assertEqual(save.call_args.args[0], destination)
        self.assertEqual(data.groups, tuple(self.window.groups))
        self.assertEqual(data.selected, frozenset(self.window.selected))
        self.assertEqual(data.roots, (str(self.root),))
        self.assertTrue(data.recursive)
        self.assertEqual(data.excluded_folders, (str(self.root / "Backup"),))
        self.assertEqual(data.file_count, 3)
        self.assertEqual(data.total_bytes, 4_200)
        self.assertEqual(data.active_tab, "All")

    def test_loaded_session_can_restore_saved_checks_and_selected_tab(self):
        selected = {self.window.groups[0].files[0].path}
        data = replace(self.saved_session_data(selected, "Selected"),
                       excluded_folders=(str(self.root / "Archive"),))
        self.window.add_excluded_folder_path(self.root / "Backup")
        result = LoadResult(self.root / "work.dupsession", data, saved_selection_count=1)

        def restore(dialog):
            button = next(button for button in dialog.buttons() if button.text().startswith("Restore"))
            button.click()
            return 0

        with patch.object(QMessageBox, "exec", restore):
            self.window.on_session_loaded(result)

        self.assertEqual(self.window.selected, selected)
        self.assertEqual(self.window.type_tabs.tabText(self.window.type_tabs.currentIndex()), "Selected")
        self.assertEqual(len(self.visible_file_items()), 3)
        self.assertIn("1 saved checks restored", self.window.status.text())
        self.assertEqual(self.window.session_path, result.path)
        self.assertTrue(self.window.session_available)
        self.assertEqual(self.window.excluded_folder_paths(), (str(self.root / "Archive"),))

    def test_loaded_session_defaults_to_no_checks_and_leaves_selected_tab(self):
        selected = {self.window.groups[0].files[0].path}
        data = self.saved_session_data(selected, "Selected")
        result = LoadResult(self.root / "work.dupsession", data, saved_selection_count=1)

        def load_without_checks(dialog):
            self.assertTrue(dialog.defaultButton().text().startswith("Load with nothing"))
            dialog.defaultButton().click()
            return 0

        with patch.object(QMessageBox, "exec", load_without_checks):
            self.window.on_session_loaded(result)

        self.assertFalse(self.window.selected)
        self.assertEqual(self.window.type_tabs.tabText(self.window.type_tabs.currentIndex()), "All")
        self.assertIn("all files left unchecked", self.window.status.text())

    def test_cancelling_load_preserves_current_results_and_checks(self):
        item = self.window.tree.topLevelItem(0).child(0)
        item.setCheckState(0, Qt.CheckState.Checked)
        old_groups = list(self.window.groups)
        old_selected = set(self.window.selected)
        replacement = SessionData((), frozenset(), ("C:\\Different",), False, (), 0, 0,
                                  "All", "2026-09-03T10:00:00+07:00")
        result = LoadResult(self.root / "replacement.dupsession", replacement)

        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.on_session_loaded(result)

        self.assertEqual(self.window.groups, old_groups)
        self.assertEqual(self.window.selected, old_selected)
        self.assertEqual(self.window.folders.item(0).text(), str(self.root))
        self.assertIn("Current results were not changed", self.window.status.text())

    def test_session_failure_preserves_current_results_and_checks(self):
        item = self.window.tree.topLevelItem(0).child(0)
        item.setCheckState(0, Qt.CheckState.Checked)
        old_groups = list(self.window.groups)
        old_selected = set(self.window.selected)
        with patch.object(QMessageBox, "critical") as error:
            self.window.on_session_failure("Could not load session", "Invalid session")
        error.assert_called_once()
        self.assertEqual(self.window.groups, old_groups)
        self.assertEqual(self.window.selected, old_selected)

    def test_background_save_then_load_restores_a_real_session_file(self):
        item = self.window.tree.topLevelItem(0).child(0)
        selected_path = item.data(0, Qt.ItemDataRole.UserRole).path
        item.setCheckState(0, Qt.CheckState.Checked)
        destination = self.root / "round-trip.dupsession"

        with patch.object(QFileDialog, "getSaveFileName",
                          return_value=(str(destination), "Duplicate Cleaner sessions")):
            self.window.save_session()
        save_worker = self.window.worker
        save_loop = QEventLoop()
        save_worker.finished.connect(save_loop.quit)
        QTimer.singleShot(10_000, save_loop.quit)
        save_loop.exec()
        self.assertTrue(destination.is_file())
        self.assertIsNone(self.window.worker)

        self.window.set_groups([])

        def restore(dialog):
            next(button for button in dialog.buttons() if button.text().startswith("Restore")).click()
            return 0

        with patch.object(QFileDialog, "getOpenFileName",
                          return_value=(str(destination), "Duplicate Cleaner sessions")), \
             patch.object(QMessageBox, "exec", restore):
            self.window.load_session()
            load_worker = self.window.worker
            load_loop = QEventLoop()
            load_worker.finished.connect(load_loop.quit)
            QTimer.singleShot(10_000, load_loop.quit)
            load_loop.exec()
        self.assertIsNone(self.window.worker)
        self.assertEqual(self.window.selected, {selected_path})
        self.assertEqual(len(self.window.groups), 1)
        self.assertEqual(self.window.session_path, destination)

    def test_nothing_is_selected_automatically(self):
        self.assertFalse(self.window.selected)
        self.assertFalse(self.window.recycle_button.isEnabled())
        self.assertTrue(self.window.save_session_button.isEnabled())
        self.assertTrue(self.window.load_session_button.isEnabled())
        group = self.window.tree.topLevelItem(0)
        for index in range(group.childCount()):
            self.assertEqual(group.child(index).checkState(0), Qt.CheckState.Unchecked)

    def visible_file_items(self):
        return [parent.child(child_index)
                for index in range(self.window.tree.topLevelItemCount())
                for parent in [self.window.tree.topLevelItem(index)] if not parent.isHidden()
                for child_index in range(parent.childCount()) if not parent.child(child_index).isHidden()]

    def select_tab(self, name):
        index = next(index for index in range(self.window.type_tabs.count())
                     if self.window.type_tabs.tabText(index) == name)
        self.window.type_tabs.setCurrentIndex(index)
        return index

    def test_type_tabs_filter_extensions_without_splitting_duplicate_groups(self):
        expected = {
            "Videos": {"movie.MP4", "movie.mkv"},
            "Images": {"photo.JpG", "photo.heic"},
            "Archives": {"backup.ZIP", "backup.tar.gz"},
            "Documents": {"report.DOCX", "report.pdf", "table.xlsx"},
            "Audio": {"song.mp3", "song.FLAC"},
            "Other": {"no-extension", "unknown.xyz", "archive.zip.exe"},
        }
        # Renamed copies deliberately share identical bytes across every file type.
        for names in expected.values():
            for name in names:
                self.file(name, b"renamed copies with the same contents")
        self.window.on_scan(scan([self.root]))
        original = list(self.window.groups)
        expected["Documents"].update({"meeting-notes.txt", "meeting-notes-copy.txt"})
        self.assertEqual(self.window.type_tabs.count(), 8)
        for category in expected:
            with self.subTest(category=category):
                self.select_tab(category)
                items = self.visible_file_items()
                self.assertEqual({item.text(0) for item in items}, expected[category])
                self.assertEqual(self.window.groups, original)
                self.assertFalse(self.window.selected)
                self.assertIn("copies in other tabs", self.window.filter_hint.text())
        self.window.type_tabs.setCurrentIndex(0)
        self.assertEqual(len(self.visible_file_items()), len(self.window.records))
        self.assertTrue(self.window.filter_hint.isHidden())

    def test_selected_tab_shows_complete_affected_groups_and_updates_immediately(self):
        pair_a = self.file("pair-a.zip", b"second duplicate group")
        self.file("pair-b.pdf", b"second duplicate group")
        self.file("untouched-a.jpg", b"untouched duplicate group")
        self.file("untouched-b.png", b"untouched duplicate group")
        self.window.on_scan(scan([self.root]))
        meeting_path = self.root / "Documents" / "meeting-notes.txt"
        meeting_item = next(item for item in self.window.file_items()
                            if item.data(0, Qt.ItemDataRole.UserRole).path == meeting_path)
        pair_item = next(item for item in self.window.file_items()
                         if item.data(0, Qt.ItemDataRole.UserRole).path == pair_a)
        meeting_item.setCheckState(0, Qt.CheckState.Checked)
        pair_item.setCheckState(0, Qt.CheckState.Checked)

        self.select_tab("Selected")
        visible_parents = [self.window.tree.topLevelItem(index)
                           for index in range(self.window.tree.topLevelItemCount())
                           if not self.window.tree.topLevelItem(index).isHidden()]
        self.assertEqual(len(visible_parents), 2)
        self.assertEqual(len(self.visible_file_items()), 5)
        self.assertEqual(sum(item.checkState(0) == Qt.CheckState.Checked
                             for item in self.visible_file_items()), 2)
        self.assertIn("2 duplicate groups containing 2 checked files", self.window.filter_hint.text())
        self.assertIn("Unchecked copies are shown", self.window.filter_hint.text())
        self.assertNotIn("hidden by this tab", self.window.selection_label.text())

        self.window.tree.setCurrentItem(pair_item)
        pair_item.setCheckState(0, Qt.CheckState.Unchecked)
        self.assertEqual(len(self.visible_file_items()), 3)
        self.assertIsNone(self.window.tree.currentItem())
        self.assertIn("1 duplicate group containing 1 checked file", self.window.filter_hint.text())

        self.window.clear_selection()
        self.assertEqual(self.visible_file_items(), [])
        self.assertIn("No files are checked", self.window.filter_hint.text())
        self.assertFalse(self.window.recycle_button.isEnabled())

    def test_empty_tab_clears_hidden_details_and_hover_but_keeps_checks(self):
        item = self.window.tree.topLevelItem(0).child(0)
        item.setCheckState(0, Qt.CheckState.Checked)
        selected = set(self.window.selected)
        self.window.tree.setCurrentItem(item)
        self.window.thumbnails.hover(item.data(0, Qt.ItemDataRole.UserRole))
        self.select_tab("Videos")
        self.assertEqual(self.visible_file_items(), [])
        self.assertIn("No duplicate files in Videos", self.window.filter_hint.text())
        self.assertFalse(self.window.filter_hint.isHidden())
        self.assertIsNone(self.window.tree.currentItem())
        self.assertTrue(self.window.details_panel.isHidden())
        self.assertFalse(self.window.open_button.isEnabled())
        self.assertIsNone(self.window.thumbnails.record)
        self.assertFalse(self.window.thumbnails.delay.isActive())
        self.assertEqual(self.window.selected, selected)
        self.assertIn("1 hidden by this tab", self.window.selection_label.text())
        self.assertTrue(self.window.recycle_button.isEnabled())
        self.window.clear_selection()
        self.window.type_tabs.setCurrentIndex(0)
        self.assertFalse(self.window.selected)
        self.assertEqual(item.checkState(0), Qt.CheckState.Unchecked)
        self.assertNotIn("hidden", self.window.selection_label.text())

    def test_cross_tab_confirmation_includes_hidden_selections_and_full_groups(self):
        self.file("copy.mp4", b"meeting notes\n" * 100)
        self.window.on_scan(scan([self.root]))
        group = self.window.tree.topLevelItem(0)
        self.select_tab("Documents")
        for item in self.visible_file_items():
            item.setCheckState(0, Qt.CheckState.Checked)
        self.select_tab("Videos")
        self.assertEqual(len(self.visible_file_items()), 1)
        self.assertEqual(group.text(1), "1 of 4 files shown")
        self.assertIn("3 hidden by this tab", self.window.selection_label.text())
        self.visible_file_items()[0].setCheckState(0, Qt.CheckState.Checked)
        selected = frozenset(self.window.selected)
        groups = tuple(self.window.groups)

        def accept(dialog):
            self.assertIn("3 selected file(s) hidden by the current tab", dialog.informativeText())
            self.assertIn("All copies selected in 1 group(s)", dialog.informativeText())
            for path in selected:
                self.assertIn(str(path), dialog.detailedText())
            self.assertEqual(dialog.defaultButton(), dialog.button(QMessageBox.StandardButton.Cancel))
            next(button for button in dialog.buttons()
                 if dialog.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole).click()
            return 0

        with patch.object(QMessageBox, "exec", accept), patch.object(self.window, "start_job") as start:
            self.window.confirm_recycle()
        with patch("duplicate_cleaner.gui.recycle_selected") as cleanup:
            start.call_args.args[0]()
        cleanup.assert_called_once_with(groups, selected)
        self.assertEqual(len(selected), 4)

    def test_cleanup_refresh_keeps_tab_and_updates_filtered_results(self):
        video = self.file("copy.mp4", b"meeting notes\n" * 100)
        self.window.on_scan(scan([self.root]))
        video_index = self.select_tab("Videos")
        self.visible_file_items()[0].setCheckState(0, Qt.CheckState.Checked)
        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.on_recycled(RecycleResult(recycled=[video]))
        self.assertEqual(self.window.type_tabs.currentIndex(), video_index)
        self.assertEqual(self.visible_file_items(), [])
        self.assertNotIn(video, self.window.records)
        self.assertIn("No duplicate files in Videos", self.window.filter_hint.text())
        self.assertFalse(self.window.selected)
        self.window.type_tabs.setCurrentIndex(0)
        self.assertEqual(len(self.visible_file_items()), 3)
        self.assertTrue(self.window.filter_hint.isHidden())

    def test_filtering_when_sorted_by_location_does_not_skip_groups(self):
        for index, extension in enumerate(("mp4", "jpg", "zip", "pdf", "mp3", "bin")):
            for copy in range(2):
                self.file(f"group-{index}-{copy}.{extension}", f"contents {index}".encode())
        self.window.on_scan(scan([self.root]))
        self.window.tree.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        for category in ("Videos", "Images", "Archives", "Documents", "Audio", "Other",
                         "All", "Documents", "Videos"):
            self.select_tab(category)
            expected_count = 15 if category == "All" else 5 if category == "Documents" else 2
            self.assertEqual(len(self.visible_file_items()), expected_count)
            self.assertTrue(self.window.tree.isSortingEnabled())

    def test_select_folder_duplicates_is_additive_across_groups_and_tabs(self):
        target_folder = self.root / "Documents"
        video = self.file("Documents/copy.mp4", b"meeting notes\n" * 100)
        archive = self.file("Documents/pair.zip", b"second duplicate group")
        document = self.file("Documents/pair.pdf", b"second duplicate group")
        nested = self.file("Documents/Subfolder/nested.zip", b"nested duplicate group")
        self.file("Elsewhere/nested-copy.zip", b"nested duplicate group")
        self.window.on_scan(scan([self.root]))

        outside = self.root / "Archive" / "meeting-notes.txt"
        outside_item = next(item for item in self.window.file_items()
                            if item.data(0, Qt.ItemDataRole.UserRole).path == outside)
        outside_item.setCheckState(0, Qt.CheckState.Checked)
        self.select_tab("Videos")
        video_item = next(item for item in self.visible_file_items()
                          if item.data(0, Qt.ItemDataRole.UserRole).path == video)
        self.window.tree.setCurrentItem(video_item)
        QTest.mouseClick(self.window.select_folder_button, Qt.MouseButton.LeftButton)

        expected_folder_paths = {path for path in self.window.records if path.parent == target_folder}
        self.assertEqual(expected_folder_paths,
                         {target_folder / "meeting-notes.txt", video, archive, document})
        self.assertEqual(self.window.selected, expected_folder_paths | {outside})
        self.assertNotIn(nested, self.window.selected)
        for item in self.window.file_items():
            path = item.data(0, Qt.ItemDataRole.UserRole).path
            expected_state = Qt.CheckState.Checked if path in self.window.selected else Qt.CheckState.Unchecked
            self.assertEqual(item.checkState(0), expected_state)
        self.assertIn("Selected 4 new", self.window.status.text())
        self.assertIn("4 duplicate files selected", self.window.status.text())
        self.assertIn("3 hidden by this tab", self.window.status.text())
        self.assertIn("4 hidden by this tab", self.window.selection_label.text())

        QTest.mouseClick(self.window.select_folder_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.window.selected, expected_folder_paths | {outside})
        self.assertIn("Selected 0 new", self.window.status.text())

        self.select_tab("Selected")
        self.assertEqual(len([self.window.tree.topLevelItem(index)
                              for index in range(self.window.tree.topLevelItemCount())
                              if not self.window.tree.topLevelItem(index).isHidden()]), 2)
        self.assertEqual(len(self.visible_file_items()), 6)
        self.assertIn("2 duplicate groups containing 5 checked files", self.window.filter_hint.text())

    def test_folder_selection_controls_follow_the_target_file(self):
        self.assertFalse(self.window.select_folder_button.isEnabled())
        group = self.window.tree.topLevelItem(0)
        self.window.tree.setCurrentItem(group)
        self.assertFalse(self.window.select_folder_button.isEnabled())
        item = group.child(0)
        self.window.tree.setCurrentItem(item)
        self.assertTrue(self.window.select_folder_button.isEnabled())

        self.window.show()
        self.app.processEvents()
        position = self.window.tree.visualItemRect(item).center()
        fake_menu = Mock()
        fake_action = Mock()
        fake_menu.addAction.side_effect = [Mock(), fake_action]
        with patch("duplicate_cleaner.gui.QMenu", return_value=fake_menu), \
             patch.object(self.window, "select_folder_duplicates") as select:
            self.window.show_result_menu(position)
            callback = fake_action.triggered.connect.call_args.args[0]
            callback()
        self.assertEqual([call.args[0] for call in fake_menu.addAction.call_args_list],
                         ["Open file location", "Select all duplicates in this folder"])
        fake_action.setEnabled.assert_called_once_with(True)
        fake_menu.exec.assert_called_once()
        select.assert_called_once_with(item)
        self.assertEqual(self.window.tree.currentItem(), item)
        self.assertEqual(self.window.tree.contextMenuPolicy(), Qt.ContextMenuPolicy.CustomContextMenu)

    def test_context_menu_opens_clicked_files_folder_without_changing_checks(self):
        self.window.show()
        self.app.processEvents()
        group = self.window.tree.topLevelItem(0)
        group.child(0).setCheckState(0, Qt.CheckState.Checked)
        self.window.tree.setCurrentItem(group.child(0))
        item = group.child(1)
        record = item.data(0, Qt.ItemDataRole.UserRole)
        selected = set(self.window.selected)

        menu = QMenu(self.window)

        def open_location(position):
            action = next(action for action in menu.actions() if action.text() == "Open file location")
            action.trigger()

        with patch("duplicate_cleaner.gui.QMenu", return_value=menu), \
             patch.object(menu, "exec", side_effect=open_location), \
             patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            self.window.show_result_menu(self.window.tree.visualItemRect(item).center())
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[0].toLocalFile().replace("/", "\\"), str(record.path.parent))
        self.assertEqual(self.window.selected, selected)

    def test_context_menu_ignores_group_and_blank_rows(self):
        self.window.show()
        self.app.processEvents()
        with patch("duplicate_cleaner.gui.QMenu") as menu:
            self.window.show_result_menu(self.window.tree.visualItemRect(self.window.tree.topLevelItem(0)).center())
            self.window.tree.collapseAll()
            self.window.show_result_menu(self.window.tree.viewport().rect().bottomLeft())
        menu.assert_not_called()

    def test_sidebar_icon_restores_panel_width_and_preserves_selection(self):
        self.window.show()
        self.app.processEvents()
        item = self.window.tree.topLevelItem(0).child(0)
        item.setCheckState(0, Qt.CheckState.Checked)
        selected = set(self.window.selected)
        groups = list(self.window.groups)
        sidebar_width = self.window.sidebar_container.width()
        results_width = self.window.workflow_tabs.width()
        self.assertEqual(self.window.sidebar_toggle.text(), "")
        self.assertGreater(self.window.sidebar_toggle.x(), self.window.sidebar.geometry().right())
        for dark in (False, True):
            self.window.dark_mode.setChecked(dark)
            QTest.mouseClick(self.window.sidebar_toggle, Qt.MouseButton.LeftButton)
            QTest.qWait(20)
            self.assertTrue(self.window.sidebar.isHidden())
            self.assertTrue(self.window.sidebar_toggle.isVisible())
            self.assertEqual(self.window.sidebar_toggle.accessibleName(), "Show sidebar")
            self.assertGreater(self.window.workflow_tabs.width(), results_width)
            self.assertTrue(self.window.grab().save(str(self.root.parent / f"gui-sidebar-hidden-{dark}.png")))
            QTest.mouseClick(self.window.sidebar_toggle, Qt.MouseButton.LeftButton)
            QTest.qWait(20)
            self.assertFalse(self.window.sidebar.isHidden())
            self.assertEqual(self.window.sidebar_toggle.accessibleName(), "Hide sidebar")
            self.assertAlmostEqual(self.window.sidebar_container.width(), sidebar_width, delta=2)
        self.assertEqual(self.window.selected, selected)
        self.assertEqual(self.window.groups, groups)
        self.assertEqual(self.window.folders.count(), 1)

    def test_manual_check_updates_count_and_button(self):
        item = self.window.tree.topLevelItem(0).child(1)
        item.setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(len(self.window.selected), 1)
        self.assertTrue(self.window.recycle_button.isEnabled())
        self.assertIn("1 file selected", self.window.selection_label.text())
        self.window.clear_selection()
        self.assertFalse(self.window.selected)
        self.assertFalse(self.window.recycle_button.isEnabled())

    def test_gui_allows_checking_every_copy(self):
        group = self.window.tree.topLevelItem(0)
        group.child(0).setCheckState(0, Qt.CheckState.Checked)
        group.child(1).setCheckState(0, Qt.CheckState.Checked)
        with patch.object(QMessageBox, "warning") as warning:
            group.child(2).setCheckState(0, Qt.CheckState.Checked)
            warning.assert_not_called()
        self.assertEqual(len(self.window.selected), 3)
        self.assertEqual(group.child(2).checkState(0), Qt.CheckState.Checked)
        self.assertTrue(self.window.recycle_button.isEnabled())

    def test_new_results_clear_old_selections(self):
        self.window.tree.topLevelItem(0).child(0).setCheckState(0, Qt.CheckState.Checked)
        self.window.on_scan(scan([self.root]))
        self.assertFalse(self.window.selected)
        self.assertFalse(self.window.recycle_button.isEnabled())

    def test_results_accept_a_single_pass_iterable(self):
        groups = tuple(self.window.groups)
        self.window.set_groups(iter(groups))
        self.assertEqual(self.window.groups, list(groups))
        self.assertEqual(set(self.window.records), {record.path for group in groups for record in group.files})
        self.assertEqual(len(list(self.window.file_items())), len(self.window.records))
        self.assertFalse(self.window.selected)

    def test_empty_folder_modified_column_sorts_chronologically(self):
        from dataclasses import replace
        from duplicate_cleaner.empty_folders import capture_empty_folder

        paths = [self.root / name for name in ("earlier", "later")]
        for path in paths:
            path.mkdir()
        earlier = replace(capture_empty_folder(paths[0]), modified_ns=1_600_000_000_000_000_000)
        later = replace(capture_empty_folder(paths[1]), modified_ns=1_700_000_000_000_000_000)
        self.window.set_empty_folders([later, earlier])
        items = list(self.window.empty_folder_items())
        self.window.empty_folder_tree.sortByColumn(2, Qt.SortOrder.AscendingOrder)
        # Call directly as well: Qt can print and swallow Python comparator errors.
        self.assertIsInstance(items[0].__lt__(items[1]), bool)
        self.assertEqual([item.data(0, Qt.ItemDataRole.UserRole).path
                          for item in self.window.empty_folder_items()], paths)
        self.window.empty_folder_tree.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        self.assertEqual([item.data(0, Qt.ItemDataRole.UserRole).path
                          for item in self.window.empty_folder_items()], paths[::-1])

    def test_cleanup_keeps_remaining_copies_and_updates_savings(self):
        original = self.window.groups[0]
        item = self.window.tree.topLevelItem(0).child(0)
        removed = item.data(0, Qt.ItemDataRole.UserRole).path
        item.setCheckState(0, Qt.CheckState.Checked)
        self.window.tree.setCurrentItem(item)
        self.window.thumbnails.hover(item.data(0, Qt.ItemDataRole.UserRole))
        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.on_recycled(RecycleResult(recycled=[removed]))

        self.assertEqual(len(self.window.groups), 1)
        remaining = self.window.groups[0]
        self.assertEqual(remaining.files, tuple(record for record in original.files if record.path != removed))
        self.assertEqual(remaining.digest, original.digest)
        self.assertNotIn(removed, self.window.records)
        self.assertEqual(self.window.tree.topLevelItem(0).childCount(), 2)
        self.assertIn("1 duplicate group", self.window.summary.text())
        self.assertIn(format_bytes(original.files[0].size), self.window.summary.text())
        self.assertTrue(self.window.empty.isHidden())
        self.assertFalse(self.window.selected)
        self.assertFalse(self.window.recycle_button.isEnabled())
        self.assertTrue(self.window.details_panel.isHidden())
        self.assertIsNone(self.window.thumbnails.record)
        for index in range(2):
            self.assertEqual(self.window.tree.topLevelItem(0).child(index).checkState(0), Qt.CheckState.Unchecked)

    def test_completed_group_disappears_but_untouched_group_remains(self):
        self.file("other-a.txt", b"another group")
        self.file("other-b.txt", b"another group")
        self.window.on_scan(scan([self.root]))
        completed = next(group for group in self.window.groups if len(group.files) == 3)
        untouched = next(group for group in self.window.groups if len(group.files) == 2)
        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.on_recycled(RecycleResult(recycled=[record.path for record in completed.files[:2]]))
        self.assertEqual(self.window.groups, [untouched])
        self.assertEqual(set(self.window.records), {record.path for record in untouched.files})
        self.assertTrue(completed.files[-1].path.exists())

    def test_failed_and_cancelled_cleanups_keep_unrecycled_files(self):
        original = self.window.groups[0]
        first, second = original.files[:2]
        failure = Issue(second.path, "Recycle Bin unavailable")
        results = [
            RecycleResult(issues=[failure]),
            RecycleResult(cancelled=True),
            RecycleResult(recycled=[first.path], issues=[failure]),
            RecycleResult(recycled=[first.path], cancelled=True),
        ]
        for result in results:
            with self.subTest(result=result):
                self.window.set_groups([original])
                self.window.tree.topLevelItem(0).child(0).setCheckState(0, Qt.CheckState.Checked)
                with patch.object(QMessageBox, "exec", return_value=0):
                    self.window.on_recycled(result)
                self.assertEqual(set(self.window.records),
                                 {record.path for record in original.files} - set(result.recycled))
                self.assertEqual(len(self.window.groups), 1)
                self.assertEqual(self.window.issues, result.issues)
                self.assertEqual(self.window.issue_button.isEnabled(), bool(result.issues))
                self.assertFalse(self.window.selected)
                if result.cancelled:
                    self.assertIn("cancelled", self.window.status.text())

    def test_successive_batches_can_keep_a_copy_without_rescanning(self):
        moved = []

        def recycler(path, revalidate):
            revalidate()
            path.rename(self.root / f"recycled-fixture-{len(moved)}")
            moved.append(path)

        for _ in range(2):
            group = self.window.tree.topLevelItem(0)
            group.child(0).setCheckState(0, Qt.CheckState.Checked)
            result = recycle_selected(self.window.groups, self.window.selected, recycler=recycler)
            self.assertEqual(len(result.recycled), 1)
            self.assertFalse(result.issues)
            with patch.object(QMessageBox, "exec", return_value=0):
                self.window.on_recycled(result)
        self.assertEqual(len(moved), 2)
        self.assertFalse(self.window.groups)
        self.assertEqual(self.window.tree.topLevelItemCount(), 0)
        self.assertFalse(self.window.selected)
        self.assertFalse(self.window.recycle_button.isEnabled())
        self.assertIn("No duplicate groups remain", self.window.empty.text())

    def test_recycling_every_copy_removes_group_without_claiming_a_copy_was_kept(self):
        selected = list(self.window.records)

        def recycler(path, revalidate):
            revalidate()
            path.rename(path.with_name(path.name + ".recycled-fixture"))

        result = recycle_selected(self.window.groups, selected, recycler=recycler)
        self.assertEqual(set(result.recycled), set(selected))
        self.assertFalse(result.issues, result.issues)
        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.on_recycled(result)
        self.assertFalse(self.window.groups)
        self.assertFalse(self.window.records)
        self.assertNotIn("was kept", self.window.empty.text())
        self.assertTrue(all(not path.exists() for path in selected))

    def test_changed_file_is_still_rejected_in_a_later_batch(self):
        original = self.window.groups[0]
        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.on_recycled(RecycleResult(recycled=[original.files[0].path]))
        self.assertEqual(len(self.window.groups), 1)
        selected, keeper = self.window.groups[0].files
        keeper.path.write_bytes(b"now a different file")
        recycler = Mock()
        result = recycle_selected(self.window.groups, [selected.path], recycler=recycler)
        self.assertFalse(result.recycled)
        self.assertTrue(result.issues)
        recycler.assert_not_called()
        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.on_recycled(result)
        self.assertEqual(len(self.window.records), 2)

    def test_unexpected_failure_keeps_list_but_warns_it_may_be_stale(self):
        original = list(self.window.groups)
        self.window.tree.topLevelItem(0).child(0).setCheckState(0, Qt.CheckState.Checked)
        with patch.object(QMessageBox, "critical") as error:
            self.window.on_failure("Unexpected cleanup failure")
        error.assert_called_once()
        self.assertEqual(self.window.groups, original)
        self.assertFalse(self.window.selected)
        self.assertIn("out of date", self.window.summary.text())

    def test_duplicate_folder_entry_is_not_added(self):
        self.window.add_folder_path(str(self.root))
        self.assertEqual(self.window.folders.count(), 1)

    def test_selecting_file_shows_details_without_selecting_it_for_recycling(self):
        group = self.window.tree.topLevelItem(0)
        item = group.child(0)
        record = item.data(0, Qt.ItemDataRole.UserRole)
        self.window.tree.setCurrentItem(item)
        details = self.window.details_text.toPlainText()
        self.assertIn(str(record.path), details)
        self.assertIn(f"{record.size:,} bytes", details)
        self.assertIn(self.window.groups[0].digest, details)
        self.assertIn("Unchecked", details)
        self.assertFalse(self.window.details_panel.isHidden())
        self.assertFalse(self.window.selected)
        self.assertFalse(self.window.recycle_button.isEnabled())
        item.setCheckState(0, Qt.CheckState.Checked)
        self.assertIn("Selected for recycling", self.window.details_text.toPlainText())
        self.window.tree.setCurrentItem(group)
        self.assertFalse(self.window.details_panel.isHidden())
        self.assertEqual(len(self.window.comparison_preview.files), 3)

    def test_details_warn_when_the_scanned_file_changed(self):
        item = self.window.tree.topLevelItem(0).child(0)
        record = item.data(0, Qt.ItemDataRole.UserRole)
        record.path.write_bytes(b"changed")
        self.window.tree.setCurrentItem(item)
        self.assertIn("Changed or unavailable", self.window.details_text.toPlainText())

    def test_double_click_opens_file_in_default_app_without_checking_it(self):
        self.window.show()
        self.app.processEvents()
        item = self.window.tree.topLevelItem(0).child(0)
        record = item.data(0, Qt.ItemDataRole.UserRole)
        position = self.window.tree.visualItemRect(item).topLeft()
        position.setX(position.x() + 100)
        position.setY(position.y() + 15)
        with patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            QTest.mouseClick(self.window.tree.viewport(), Qt.MouseButton.LeftButton, pos=position)
            QTest.mouseDClick(self.window.tree.viewport(), Qt.MouseButton.LeftButton, pos=position)
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[0].toLocalFile().replace("/", "\\"), str(record.path))
        self.assertFalse(self.window.selected)

    def test_double_clicking_a_checkbox_does_not_open_file(self):
        self.window.show()
        self.app.processEvents()
        item = self.window.tree.topLevelItem(0).child(0)
        position = self.window.tree.visualItemRect(item).topLeft()
        position.setX(position.x() + 12)
        position.setY(position.y() + 15)
        with patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            QTest.mouseClick(self.window.tree.viewport(), Qt.MouseButton.LeftButton, pos=position)
            self.assertEqual(item.checkState(0), Qt.CheckState.Checked)
            QTest.mouseDClick(self.window.tree.viewport(), Qt.MouseButton.LeftButton, pos=position)
        opened.assert_not_called()

    def test_missing_file_is_not_opened(self):
        item = self.window.tree.topLevelItem(0).child(0)
        item.data(0, Qt.ItemDataRole.UserRole).path.unlink()
        with patch.object(QDesktopServices, "openUrl") as opened, \
             patch.object(QMessageBox, "warning") as warning:
            self.window.open_file(item)
        opened.assert_not_called()
        warning.assert_called_once()

    def test_open_failure_is_reported(self):
        item = self.window.tree.topLevelItem(0).child(0)
        with patch.object(QDesktopServices, "openUrl", return_value=False), \
             patch.object(QMessageBox, "warning") as warning:
            self.window.open_file(item)
        warning.assert_called_once()

    def test_group_double_click_does_not_open_any_file(self):
        with patch.object(QDesktopServices, "openUrl") as opened:
            self.window.tree.itemDoubleClicked.emit(self.window.tree.topLevelItem(0), 0)
        opened.assert_not_called()

    def test_hovering_file_starts_preview_without_checking_it(self):
        self.window.show()
        self.app.processEvents()
        item = self.window.tree.topLevelItem(0).child(0)
        position = self.window.tree.visualItemRect(item).center()
        position.setX(180)
        QTest.mouseMove(self.window.tree.viewport(), position)
        self.assertEqual(self.window.thumbnails.record, item.data(0, Qt.ItemDataRole.UserRole))
        self.assertTrue(self.window.thumbnails.delay.isActive())
        self.assertFalse(self.window.selected)

    def test_refresh_clears_file_details_and_hover(self):
        item = self.window.tree.topLevelItem(0).child(0)
        self.window.tree.setCurrentItem(item)
        self.window.thumbnails.hover(item.data(0, Qt.ItemDataRole.UserRole))
        self.window.set_groups([])
        self.assertFalse(self.window.details_text.toPlainText())
        self.assertTrue(self.window.details_panel.isHidden())
        self.assertIsNone(self.window.thumbnails.record)
        self.assertFalse(self.window.thumbnails.delay.isActive())

    def test_dismissing_confirmation_does_not_start_cleanup(self):
        self.window.tree.topLevelItem(0).child(0).setCheckState(0, Qt.CheckState.Checked)
        with patch.object(QMessageBox, "exec", return_value=0), \
             patch.object(self.window, "start_job") as start:
            self.window.confirm_recycle()
        start.assert_not_called()
        self.assertEqual(len(self.window.selected), 1)

    def test_all_copy_confirmation_warns_and_defaults_to_cancel(self):
        group = self.window.tree.topLevelItem(0)
        for index in range(group.childCount()):
            group.child(index).setCheckState(0, Qt.CheckState.Checked)

        def dismiss(dialog):
            self.assertIn("All copies selected in 1 group(s)", dialog.informativeText())
            self.assertIn("No listed copy will be kept", dialog.informativeText())
            self.assertIn("NONE", dialog.detailedText())
            for path in self.window.selected:
                self.assertIn(str(path), dialog.detailedText())
            self.assertEqual(dialog.defaultButton(), dialog.button(QMessageBox.StandardButton.Cancel))
            return 0

        with patch.object(QMessageBox, "exec", dismiss), patch.object(self.window, "start_job") as start:
            self.window.confirm_recycle()
        start.assert_not_called()
        self.assertEqual(len(self.window.selected), 3)

    def test_confirmed_mixed_selection_passes_all_and_partial_groups_to_cleanup(self):
        self.file("other-a.txt", b"another group")
        self.file("other-b.txt", b"another group")
        self.window.on_scan(scan([self.root]))
        for number in range(self.window.tree.topLevelItemCount()):
            group = self.window.tree.topLevelItem(number)
            count = group.childCount() if group.childCount() == 3 else 1
            for index in range(count):
                group.child(index).setCheckState(0, Qt.CheckState.Checked)
        selected = frozenset(self.window.selected)
        groups = tuple(self.window.groups)

        def accept(dialog):
            self.assertIn("All copies selected in 1 group(s)", dialog.informativeText())
            kept = set(self.window.records) - selected
            for path in kept:
                self.assertIn(str(path), dialog.detailedText())
            for button in dialog.buttons():
                if dialog.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole:
                    button.click()
                    break
            return 0

        with patch.object(QMessageBox, "exec", accept), patch.object(self.window, "start_job") as start:
            self.window.confirm_recycle()
        start.assert_called_once()
        with patch("duplicate_cleaner.gui.recycle_selected") as cleanup:
            start.call_args.args[0]()
        cleanup.assert_called_once_with(groups, selected)
        self.assertEqual(len(selected), 4)

    def test_background_scan_completes_and_restores_controls(self):
        self.window.start_scan()
        self.assertFalse(self.window.scan_button.isEnabled())
        self.assertFalse(self.window.type_tabs.isEnabled())
        self.assertTrue(self.window.cancel_button.isEnabled())
        # Use a real Qt event loop so the Python worker can acquire the GIL.
        loop = QEventLoop()
        self.window.worker.finished.connect(loop.quit)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        timeout.start(10_000)
        loop.exec()
        timeout.stop()
        self.assertIsNone(self.window.worker)
        self.assertEqual(len(self.window.groups), 1)
        self.assertTrue(self.window.scan_button.isEnabled())
        self.assertTrue(self.window.type_tabs.isEnabled())
        self.assertFalse(self.window.selected)

    def test_empty_folders_have_a_separate_unchecked_full_path_tab(self):
        first = self.root / "empty-one"
        second = self.root / "nested" / "empty-two"
        first.mkdir()
        second.mkdir(parents=True)
        duplicate_groups = list(self.window.groups)
        self.window.on_empty_folder_scan(scan_empty_folders([self.root]))

        self.assertEqual(self.window.workflow_tabs.count(), 2)
        self.assertEqual(self.window.workflow_tabs.tabText(0), "Duplicate files")
        self.assertEqual(self.window.workflow_tabs.tabText(1), "Empty folders")
        items = list(self.window.empty_folder_items())
        self.assertEqual({item.text(1) for item in items}, {str(first), str(second)})
        self.assertTrue(all(item.checkState(0) == Qt.CheckState.Unchecked for item in items))
        self.assertFalse(self.window.selected_empty_folders)
        self.assertEqual(self.window.groups, duplicate_groups)
        self.assertFalse(self.window.recycle_empty_folders_button.isEnabled())

    def test_empty_folder_checks_are_independent_from_duplicate_checks(self):
        folder = self.root / "empty"
        folder.mkdir()
        self.window.on_empty_folder_scan(scan_empty_folders([self.root]))
        file_item = self.window.tree.topLevelItem(0).child(0)
        file_item.setCheckState(0, Qt.CheckState.Checked)
        folder_item = next(self.window.empty_folder_items())
        folder_item.setCheckState(0, Qt.CheckState.Checked)

        self.assertEqual(self.window.selected_empty_folders, {folder})
        self.assertEqual(len(self.window.selected), 1)
        self.window.clear_empty_folder_selection()
        self.assertFalse(self.window.selected_empty_folders)
        self.assertEqual(len(self.window.selected), 1)

    def test_empty_folder_confirmation_lists_paths_and_defaults_to_cancel(self):
        folder = self.root / "empty"
        folder.mkdir()
        self.window.on_empty_folder_scan(scan_empty_folders([self.root]))
        next(self.window.empty_folder_items()).setCheckState(0, Qt.CheckState.Checked)

        def dismiss(dialog):
            self.assertIn(str(folder), dialog.detailedText())
            self.assertIn("Nothing is permanently deleted", dialog.informativeText())
            self.assertEqual(dialog.defaultButton(), dialog.button(QMessageBox.StandardButton.Cancel))
            return 0

        with patch.object(QMessageBox, "exec", dismiss), \
             patch.object(self.window, "start_job") as start:
            self.window.confirm_empty_folder_recycle()
        start.assert_not_called()
        self.assertEqual(self.window.selected_empty_folders, {folder})

    def test_empty_folder_cleanup_keeps_unchecked_paths_and_duplicate_results(self):
        selected = self.root / "empty-a"
        kept = self.root / "empty-b"
        selected.mkdir()
        kept.mkdir()
        self.window.on_empty_folder_scan(scan_empty_folders([self.root]))
        duplicate_groups = list(self.window.groups)
        item = next(item for item in self.window.empty_folder_items()
                    if item.data(0, Qt.ItemDataRole.UserRole).path == selected)
        item.setCheckState(0, Qt.CheckState.Checked)

        def recycler(path, revalidate):
            revalidate()
            path.rename(path.with_name(path.name + "-recycled"))

        result = recycle_empty_folders(
            self.window.empty_folders, self.window.selected_empty_folders, recycler=recycler)
        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.on_empty_folders_recycled(result)

        self.assertEqual([record.path for record in self.window.empty_folders], [kept])
        self.assertFalse(self.window.selected_empty_folders)
        self.assertEqual(self.window.groups, duplicate_groups)
        self.assertTrue(kept.exists())

    def test_empty_folder_scan_runs_in_background_without_clearing_duplicates(self):
        folder = self.root / "empty"
        folder.mkdir()
        duplicate_groups = list(self.window.groups)
        self.window.start_empty_folder_scan()
        self.assertFalse(self.window.empty_scan_button.isEnabled())
        self.assertFalse(self.window.empty_folder_tree.isEnabled())
        self.assertTrue(self.window.cancel_button.isEnabled())
        loop = QEventLoop()
        self.window.worker.finished.connect(loop.quit)
        QTimer.singleShot(10_000, loop.quit)
        loop.exec()
        self.assertIsNone(self.window.worker)
        self.assertEqual([record.path for record in self.window.empty_folders], [folder])
        self.assertEqual(self.window.groups, duplicate_groups)
        self.assertTrue(self.window.empty_scan_button.isEnabled())

    def test_gui_renders_with_results(self):
        self.window.show()
        QTest.qWait(40)
        destination = self.root.parent / "gui-preview.png"
        self.assertTrue(self.window.grab().save(str(destination)))
        self.window.resize(940, 720)
        QTest.qWait(20)
        self.assertTrue(self.window.grab().save(str(self.root.parent / "gui-small.png")))
        self.window.tree.setCurrentItem(self.window.tree.topLevelItem(0).child(0))
        QTest.qWait(20)
        self.assertTrue(self.window.grab().save(str(self.root.parent / "gui-details-small.png")))
        self.window.resize(1220, 800)
        QTest.qWait(20)
        self.assertTrue(self.window.grab().save(str(self.root.parent / "gui-details.png")))
        self.select_tab("Documents")
        for dark in (False, True):
            self.window.dark_mode.setChecked(dark)
            self.window.resize(940, 720)
            QTest.qWait(20)
            self.assertEqual(self.window.size().width(), 940)
            self.assertEqual(self.window.size().height(), 720)
            self.assertLess(self.window.folders.geometry().bottom(), self.window.add_button.y())
            self.assertLess(self.window.excluded_folders.geometry().bottom(), self.window.exclude_button.y())
            self.assertLess(self.window.load_session_button.geometry().bottom(), self.window.sidebar.height())
            self.assertTrue(self.window.type_tabs.tabRect(self.window.type_tabs.count() - 1).right()
                            < self.window.type_tabs.width())
            self.assertTrue(self.window.grab().save(str(self.root.parent / f"gui-tabs-dark-{dark}.png")))
        self.window.workflow_tabs.setCurrentIndex(1)
        (self.root / "Empty example folder").mkdir()
        self.window.on_empty_folder_scan(scan_empty_folders([self.root]))
        QTest.qWait(20)
        self.assertTrue(self.window.grab().save(str(self.root.parent / "gui-empty-folders.png")))
