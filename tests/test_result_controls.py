import os
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QStyle, QStyleOptionViewItem

from duplicate_cleaner.gui import MainWindow
from duplicate_cleaner.models import RecycleResult, ScanResult
from duplicate_cleaner.scanner import scan
from duplicate_cleaner.sessions import LoadResult, SessionData
from tests.support import FileTestCase


class ResultControlTests(FileTestCase):
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
            str(self.root / "preferences.ini"), QSettings.Format.IniFormat))
        settings.start()
        self.addCleanup(settings.stop)
        self.cat = self.file("Photos/Cat.jpg", b"a" * 1024)
        self.cat_copy = self.file("Backup/Cat-copy.bin", b"a" * 1024)
        self.dog = self.file("Photos/Dog.jpg", b"b" * 2048)
        self.file("Backup/Dog-copy.bin", b"b" * 2048)
        self.file("Empty/zero.a", b"")
        self.file("Empty/zero.b", b"")
        self.window = MainWindow()
        self.window.on_scan(scan([self.root]))
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window.close()
        self.window.settings.sync()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_stream_bytes_are_included_in_filters_rows_and_selection_totals(self):
        if os.name != "nt":
            self.skipTest("Windows NTFS streams")
        for path in (self.cat, self.cat_copy):
            with open(str(path) + ":extra", "wb") as stream:
                stream.write(b"x" * 512)
        self.window.on_scan(scan([self.root]))
        self.window.size_unit.setCurrentText("B")
        self.window.min_size_filter.setText("1536")
        self.window.max_size_filter.setText("1536")
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})
        self.assertIn("1.50 KiB potentially recoverable", self.window.summary.text())
        parent = self.group_item(self.cat)
        child = parent.child(0)
        self.assertEqual(child.text(2), "1.50 KiB")
        child.setCheckState(0, Qt.CheckState.Checked)
        self.assertIn("1.50 KiB", self.window.selection_label.text())

    def test_metadata_badge_stream_details_and_explicit_batch_permission(self):
        if os.name != "nt":
            self.skipTest("Windows NTFS streams")
        with open(str(self.cat) + ":Zone.Identifier", "wb") as stream:
            stream.write(b"[ZoneTransfer]\r\nZoneId=3\r\n")
        self.window.on_scan(scan([self.root]))
        parent, gallery = self.show_gallery()
        self.assertIn("Metadata differs", parent.text(0))
        preview = self.window.comparison_preview
        self.assertIn("Metadata differs", preview.metadata_badge.text())
        preview.metadata_toggle.click()
        self.assertFalse(preview.metadata_details.isHidden())
        details = preview.metadata_details.toPlainText()
        for text in (str(self.cat), str(self.cat_copy), "Zone.Identifier", "not present"):
            self.assertIn(text, details)
        parent.child(0).setCheckState(0, Qt.CheckState.Checked)
        for consent in (False, True):
            with self.subTest(consent=consent):
                def accept(dialog):
                    self.assertIn("Download metadata", dialog.informativeText())
                    self.assertIsNotNone(dialog.checkBox())
                    self.assertFalse(dialog.checkBox().isChecked())
                    self.assertEqual(dialog.defaultButton(), dialog.button(QMessageBox.StandardButton.Cancel))
                    dialog.checkBox().setChecked(consent)
                    next(button for button in dialog.buttons()
                         if dialog.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole).click()
                    return 0

                with patch.object(QMessageBox, "exec", accept), patch.object(self.window, "start_job") as start:
                    self.window.confirm_recycle()
                with patch("duplicate_cleaner.gui.recycle_selected") as cleanup:
                    start.call_args.args[0]()
                self.assertEqual(cleanup.call_args.kwargs.get("allow_zone_differences", False), consent)
                self.assertFalse(cleanup.call_args.kwargs.get("allow_dropbox_differences", False))
        with patch.object(QMessageBox, "exec", return_value=0), patch.object(self.window, "start_job") as start:
            self.window.confirm_recycle()
        start.assert_not_called()

    def test_custom_metadata_difference_has_no_download_override(self):
        if os.name != "nt":
            self.skipTest("Windows NTFS streams")
        with open(str(self.cat) + ":custom", "wb") as stream:
            stream.write(b"unique bytes")
        self.window.on_scan(scan([self.root]))
        parent = self.group_item(self.cat)
        parent.child(0).setCheckState(0, Qt.CheckState.Checked)

        def dismiss(dialog):
            self.assertIn("other streams", dialog.informativeText())
            self.assertIsNone(dialog.checkBox())
            return 0

        with patch.object(QMessageBox, "exec", dismiss), patch.object(self.window, "start_job") as start:
            self.window.confirm_recycle()
        start.assert_not_called()

    def test_app_recycles_selected_copy_when_unchecked_copy_has_dropbox_metadata(self):
        if os.name != "nt":
            self.skipTest("Windows NTFS streams")
        with open(str(self.cat_copy) + ":com.dropbox.attrs", "wb") as stream:
            stream.write(b"keeper-only metadata")
        self.window.on_scan(scan([self.root]))
        item = next(item for item in self.window.file_items()
                    if item.data(0, Qt.ItemDataRole.UserRole).path == self.cat)
        item.setCheckState(0, Qt.CheckState.Checked)

        def accept(dialog):
            self.assertIn("only on a kept comparison copy do not block", dialog.informativeText())
            self.assertIsNone(dialog.checkBox())
            next(button for button in dialog.buttons()
                 if dialog.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole).click()
            return 0

        with patch.object(QMessageBox, "exec", accept), patch.object(self.window, "start_job") as start:
            self.window.confirm_recycle()
        calls = []

        def recycler(path, revalidate):
            revalidate()
            calls.append(path)

        result = start.call_args.args[0](recycler=recycler)
        self.assertEqual(calls, [self.cat])
        self.assertEqual(result.recycled, [self.cat])
        self.assertFalse(result.issues, result.issues)

    def test_dropbox_metadata_consent_is_explicit_scoped_and_reset_for_each_batch(self):
        if os.name != "nt":
            self.skipTest("Windows NTFS streams")
        with open(str(self.cat) + ":com.dropbox.attrs", "wb") as stream:
            stream.write(b"selected file metadata")
        for with_zone in (False, True):
            if with_zone:
                with open(str(self.cat) + ":Zone.Identifier", "wb") as stream:
                    stream.write(b"download metadata")
            self.window.on_scan(scan([self.root]))
            item = next(item for item in self.window.file_items()
                        if item.data(0, Qt.ItemDataRole.UserRole).path == self.cat)
            item.setCheckState(0, Qt.CheckState.Checked)
            for allowed in (True, False):
                with self.subTest(with_zone=with_zone, allowed=allowed):
                    def accept(dialog):
                        self.assertIn("Dropbox metadata", dialog.informativeText())
                        self.assertIn("com.dropbox.attrs", dialog.checkBox().text())
                        self.assertEqual("Zone.Identifier" in dialog.checkBox().text(), with_zone)
                        self.assertFalse(dialog.checkBox().isChecked())
                        self.assertEqual(dialog.defaultButton(), dialog.button(QMessageBox.StandardButton.Cancel))
                        dialog.checkBox().setChecked(allowed)
                        next(button for button in dialog.buttons()
                             if dialog.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole).click()
                        return 0

                    with patch.object(QMessageBox, "exec", accept), patch.object(self.window, "start_job") as start:
                        self.window.confirm_recycle()
                    recycler = Mock()
                    result = start.call_args.args[0](recycler=recycler)
                    self.assertEqual(result.recycled, [self.cat] if allowed else [])
                    self.assertEqual(recycler.call_count, int(allowed))
            with patch.object(QMessageBox, "exec", return_value=0), patch.object(self.window, "start_job") as start:
                self.window.confirm_recycle()
            start.assert_not_called()

    def group_item(self, path):
        return next(item.parent() for item in self.window.file_items()
                    if item.data(0, Qt.ItemDataRole.UserRole).path == path)

    def visible_groups(self):
        return [self.window.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                for i in range(self.window.tree.topLevelItemCount())
                if not self.window.tree.topLevelItem(i).isHidden()]

    def show_gallery(self):
        parent = self.group_item(self.cat)
        parent.setExpanded(False)
        self.window.tree.setCurrentItem(parent)
        self.window.tree.setFocus()
        self.window.update_details()
        return parent, self.window.comparison_preview.gallery

    def test_text_filters_keep_all_copies_and_combine_on_the_same_file(self):
        self.window.filename_filter.setText(" CAT.JPG ")
        self.window.apply_filter_button.click()
        self.assertEqual(len(self.visible_groups()), 1)
        self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})
        self.assertEqual(self.window.summary.text(), "1 duplicate group  ·  1.00 KiB potentially recoverable")
        self.window.folder_path_filter.setText("BACKUP")
        self.window.apply_filter_button.click()
        self.assertEqual(self.visible_groups(), [])
        self.assertEqual(self.window.summary.text(), "0 duplicate groups  ·  0 B potentially recoverable")
        self.window.filename_filter.setText("cat-copy")
        self.window.file_path_filter.setText(str(self.cat_copy).replace("\\", "/").upper())
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})
        self.window.clear_filter_button.click()
        self.assertEqual(len(self.visible_groups()), 3)
        self.assertEqual(self.window.summary.text(), "3 duplicate groups  ·  3.00 KiB potentially recoverable")

    def test_folder_filter_uses_parent_path_not_filename(self):
        self.window.folder_path_filter.setText("Cat.jpg")
        self.window.apply_filter_button.click()
        self.assertEqual(self.visible_groups(), [])
        self.window.folder_path_filter.setText("photos")
        self.window.apply_filter_button.click()
        self.assertEqual(len(self.visible_groups()), 2)

    def test_reverse_filename_filter_updates_groups_summary_and_preserves_checks(self):
        parent, gallery = self.show_gallery()
        gallery.item(0).setCheckState(Qt.CheckState.Checked)
        selected = set(self.window.selected)
        self.window.filename_filter.setText("-CAT")
        self.window.apply_filter_button.click()
        self.assertEqual(len(self.visible_groups()), 2)
        self.assertNotIn(self.cat, self.window.visible_paths)
        self.assertEqual(self.window.selected, selected)
        self.assertEqual(self.window.summary.text(), "2 duplicate groups  ·  2.00 KiB potentially recoverable")
        self.window.clear_filter_button.click()
        self.assertEqual(len(self.visible_groups()), 3)
        self.assertFalse(self.window.filename_filter.text())

    def test_positive_and_negative_path_criteria_match_the_same_file(self):
        self.window.filename_filter.setText("cat-copy")
        self.window.folder_path_filter.setText("-BACKUP")
        self.window.apply_filter_button.click()
        self.assertFalse(self.visible_groups())
        self.window.filename_filter.setText("cat")
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})
        self.window.file_path_filter.setText("-PHOTOS\\CAT.JPG")
        self.window.apply_filter_button.click()
        self.assertFalse(self.visible_groups())

    def test_blank_reverse_filters_are_ignored_and_edits_require_apply(self):
        self.window.filename_filter.setText(" , , ")
        self.window.apply_filter_button.click()
        self.assertFalse(self.window.result_filters)
        self.assertEqual(len(self.visible_groups()), 3)
        self.window.filename_filter.setText("-cat")
        self.window.apply_filter_button.click()
        self.assertEqual(len(self.visible_groups()), 2)
        self.window.filename_filter.setText("cat")
        self.assertEqual(len(self.visible_groups()), 2)
        self.window.min_size_filter.setText("invalid")
        self.window.apply_filter_button.click()
        self.assertEqual(len(self.visible_groups()), 2)
        self.assertEqual(self.window.result_filters["filename"], (("cat", True),))
        self.window.min_size_filter.clear()
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})

    def test_multiple_includes_and_excludes_in_each_text_field(self):
        self.window.filename_filter.setText("CAT, copy, -dog, -zero")
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})
        self.window.filename_filter.setText("cat, dog")
        self.window.apply_filter_button.click()
        self.assertFalse(self.visible_groups())
        self.window.filename_filter.setText("cat, -copy, -jpg")
        self.window.apply_filter_button.click()
        self.assertFalse(self.visible_groups())
        self.window.filename_filter.clear()
        self.window.file_path_filter.setText("PHOTOS, CAT.JPG, -backup, -dog")
        self.window.folder_path_filter.setText("photos, -backup, -empty")
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})
        self.window.folder_path_filter.setText("photos, backup")
        self.window.apply_filter_button.click()
        self.assertFalse(self.visible_groups())

    def test_terms_preserve_spaces_quoted_commas_and_literal_leading_minus(self):
        first = self.file("Summer photos/John's draft, final.txt", b"a new duplicate")
        second = self.file("Backup/-draft.txt", b"a new duplicate")
        self.window.on_scan(scan([self.root]))
        self.window.filename_filter.setText('"draft, final", -copy')
        self.window.folder_path_filter.setText("summer photos")
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.visible_paths, {first, second})
        self.window.folder_path_filter.clear()
        self.window.filename_filter.setText("John's, draft")
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.visible_paths, {first, second})
        self.window.filename_filter.setText('draft, -"draft, final", --draft')
        self.window.apply_filter_button.click()
        self.assertFalse(self.visible_groups())
        self.window.filename_filter.setText("+-draft")
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.visible_paths, {first, second})

    def test_invalid_terms_leave_previous_results_unchanged(self):
        self.window.filename_filter.setText("cat")
        self.window.apply_filter_button.click()
        for value in ("-", "+", '"unfinished'):
            self.window.filename_filter.setText(value)
            self.window.apply_filter_button.click()
            self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})
            self.assertFalse(self.window.filter_error.isHidden())

    def test_size_bounds_are_inclusive_support_decimals_and_zero(self):
        self.window.size_unit.setCurrentText("KiB")
        self.window.min_size_filter.setText("0.5")
        self.window.max_size_filter.setText("1")
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})
        self.window.min_size_filter.setText("1")
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})
        self.window.min_size_filter.setText("0")
        self.window.max_size_filter.setText("0")
        self.window.apply_filter_button.click()
        self.assertEqual(len(self.visible_groups()), 1)
        self.assertEqual(self.visible_groups()[0].files[0].size, 0)

    def test_invalid_sizes_preserve_applied_filter_and_results(self):
        self.window.filename_filter.setText("cat")
        self.window.apply_filter_button.click()
        previous = self.window.result_filters.copy()
        for value in ("-1", "NaN", "1e100", "1 MB"):
            self.window.min_size_filter.setText(value)
            self.window.apply_filter_button.click()
            self.assertEqual(self.window.result_filters, previous)
            self.assertFalse(self.window.filter_error.isHidden())
            self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})
        self.window.min_size_filter.setText("2")
        self.window.max_size_filter.setText("1")
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.result_filters, previous)
        self.window.clear_filter_button.click()
        self.assertTrue(self.window.filter_error.isHidden())

    def test_filters_preserve_hidden_checks_and_clear_hidden_preview(self):
        parent, gallery = self.show_gallery()
        gallery.item(0).setCheckState(Qt.CheckState.Checked)
        selected = set(self.window.selected)
        self.window.filename_filter.setText("no match")
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.selected, selected)
        self.assertFalse(self.window.visible_paths)
        self.assertEqual(gallery.count(), 0)
        self.assertIn("hidden", self.window.selection_label.text())
        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.confirm_recycle()
        dialog = self.window.findChildren(QMessageBox)[-1]
        self.assertIn("hidden by the current tab or filters", dialog.informativeText())
        self.window.clear_filter_button.click()
        self.assertEqual(self.window.selected, selected)

    def test_filters_work_with_type_tabs_and_selected_tab(self):
        self.window.filename_filter.setText("cat-copy")
        self.window.apply_filter_button.click()
        self.window.type_tabs.setCurrentIndex(3)  # Images
        self.assertEqual(self.window.type_tabs.tabText(3), "Images")
        self.assertEqual(self.window.visible_paths, {self.cat})
        self.assertEqual(self.window.summary.text(), "1 duplicate group  ·  1.00 KiB potentially recoverable")
        parent, gallery = self.show_gallery()
        copy_index = next(i for i, record in enumerate(gallery.files) if record.path == self.cat_copy)
        gallery.item(copy_index).setCheckState(Qt.CheckState.Checked)
        self.window.type_tabs.setCurrentIndex(1)  # Selected includes all copies.
        self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})
        self.window.clear_selection()
        self.assertEqual(self.visible_groups(), [])
        self.assertEqual(gallery.count(), 0)

    def test_summary_stays_filtered_after_rescan_and_cleanup(self):
        self.window.filename_filter.setText("cat")
        self.window.apply_filter_button.click()
        self.window.on_scan(scan([self.root]))
        self.assertEqual(self.window.summary.text(), "1 duplicate group  ·  1.00 KiB potentially recoverable")
        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.on_recycled(RecycleResult(recycled=[self.cat_copy]))
        self.assertEqual(self.window.summary.text(), "0 duplicate groups remaining  ·  0 B potentially recoverable")
        self.window.clear_filter_button.click()
        self.assertEqual(self.window.summary.text(), "2 duplicate groups remaining  ·  2.00 KiB potentially recoverable")

    def test_loaded_summary_uses_filters_and_status_messages_survive_filter_changes(self):
        self.window.filename_filter.setText("cat")
        self.window.apply_filter_button.click()
        data = SessionData(tuple(self.window.groups), frozenset(), (str(self.root),),
                           True, (), 6, 6144, "All", "2026-09-13T00:00:00+07:00")

        def accept(dialog):
            dialog.defaultButton().click()
            return 0

        with patch.object(QMessageBox, "exec", accept):
            self.window.on_session_loaded(LoadResult(self.root / "saved.dupsession", data))
        self.assertEqual(self.window.summary.text(), "Loaded 1 duplicate group  ·  1.00 KiB potentially recoverable")
        with patch.object(QMessageBox, "critical"):
            self.window.on_failure("Test failure")
        self.window.clear_filter_button.click()
        self.assertEqual(self.window.summary.text(), "Operation stopped · results may be out of date")
        self.window.on_scan(ScanResult(cancelled=True))
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.summary.text(), "Scan cancelled — no files changed")

    def test_gallery_checkbox_click_syncs_collapsed_tree_and_clear_selection(self):
        self.window.resize(1500, 1100)
        self.window.show()
        parent, gallery = self.show_gallery()
        self.app.processEvents()
        item = gallery.item(0)
        record = item.data(Qt.ItemDataRole.UserRole)
        option = QStyleOptionViewItem()
        option.initFrom(gallery)
        gallery.itemDelegate().initStyleOption(option, gallery.model().index(0, 0))
        option.rect = gallery.visualItemRect(item)
        checkbox = gallery.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator,
                                                 option, gallery)
        QTest.mouseClick(gallery.viewport(), Qt.MouseButton.LeftButton, pos=checkbox.center())
        self.assertEqual(self.window.selected, {record.path})
        self.assertFalse(parent.isExpanded())
        self.assertFalse(parent.icon(0).isNull())
        self.assertNotEqual(parent.background(0).style(), Qt.BrushStyle.NoBrush)
        tree_item = next(child for child in self.window.file_items()
                         if child.data(0, Qt.ItemDataRole.UserRole).path == record.path)
        self.assertEqual(tree_item.checkState(0), Qt.CheckState.Checked)
        tree_item.setCheckState(0, Qt.CheckState.Unchecked)
        self.assertEqual(item.checkState(), Qt.CheckState.Unchecked)
        self.assertTrue(parent.icon(0).isNull())
        self.assertEqual(parent.background(0).style(), Qt.BrushStyle.NoBrush)
        item.setCheckState(Qt.CheckState.Checked)
        item.setText("Thumbnail label refreshed")
        self.assertEqual(self.window.selected, {record.path})
        self.window.clear_selection()
        self.assertEqual(item.checkState(), Qt.CheckState.Unchecked)
        self.assertTrue(parent.icon(0).isNull())

    def test_pair_checkboxes_follow_file_chooser_and_list_selection(self):
        parent = self.group_item(self.cat)
        self.window.tree.setCurrentItem(parent.child(0))
        left, right = self.window.comparison_preview.panes
        left.recycle_check.click()
        self.assertEqual(self.window.selected, {left.record.path})
        left.chooser.setCurrentIndex(right.chooser.currentIndex())
        self.assertFalse(left.recycle_check.isChecked())
        self.assertTrue(right.recycle_check.isChecked())
        right.recycle_check.click()
        self.assertFalse(self.window.selected)

    def test_selecting_all_preview_copies_keeps_recycling_warning(self):
        parent, gallery = self.show_gallery()
        gallery.item(0).setCheckState(Qt.CheckState.Checked)
        partial_color = parent.background(0).color()
        for i in range(gallery.count()):
            gallery.item(i).setCheckState(Qt.CheckState.Checked)
        self.assertNotEqual(parent.background(0).color(), partial_color)
        self.assertIn("All copies selected", parent.toolTip(0))
        gallery.item(0).setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(parent.background(0).color(), partial_color)
        gallery.item(0).setCheckState(Qt.CheckState.Checked)
        self.assertEqual(self.window.selected, {self.cat, self.cat_copy})
        with patch.object(QMessageBox, "exec", return_value=0):
            self.window.confirm_recycle()
        self.assertIn("All copies selected", self.window.findChildren(QMessageBox)[-1].informativeText())
        self.assertTrue(all(parent.child(i).checkState(0) == Qt.CheckState.Checked
                            for i in range(parent.childCount())))

    def test_filter_and_preview_controls_are_disabled_during_work(self):
        self.show_gallery()
        self.window.worker = Mock()
        try:
            self.window.update_actions()
            self.assertFalse(self.window.filter_panel.isEnabled())
            self.assertFalse(self.window.comparison_preview.isEnabled())
            self.window.preview_selection_changed(self.cat, True)
            self.assertFalse(self.window.selected)
        finally:
            self.window.worker = None
            self.window.update_actions()

    def test_controls_render_in_both_themes_at_minimum_window_size(self):
        self.window.show()
        self.window.resize(940, 720)
        self.assertTrue(self.window.filter_panel.isHidden())
        self.window.filter_toggle.click()
        self.window.filename_filter.setText("cat")
        self.window.apply_filter_button.click()
        self.show_gallery()
        for dark in (False, True):
            self.window.dark_mode.setChecked(dark)
            self.app.processEvents()
            self.assertEqual(self.window.width(), 940)
            self.assertEqual(self.window.height(), 720)
            self.assertTrue(self.window.filter_panel.rect().contains(self.window.apply_filter_button.geometry()))
            self.assertTrue(self.window.rect().contains(self.window.clear_filter_button.mapTo(
                self.window, self.window.clear_filter_button.rect().bottomRight())))
            self.assertTrue(self.window.grab().save(str(self.root.parent / f"result-controls-{dark}.png")))
        self.window.resize(1220, 1000)
        self.app.processEvents()
        self.assertTrue(self.window.grab().save(str(self.root.parent / "result-controls-wide.png")))
        visible_paths = set(self.window.visible_paths)
        self.window.filter_toggle.click()
        self.assertTrue(self.window.filter_panel.isHidden())
        self.assertEqual(self.window.visible_paths, visible_paths)
        self.assertEqual(self.window.filter_toggle.accessibleName(), "Show filters")
        self.app.processEvents()
        self.assertTrue(self.window.grab().save(str(self.root.parent / "filters-hidden.png")))
