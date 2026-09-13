import os
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QStyle, QStyleOptionViewItem

from duplicate_cleaner.gui import MainWindow
from duplicate_cleaner.scanner import scan
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
        self.window.folder_path_filter.setText("BACKUP")
        self.window.apply_filter_button.click()
        self.assertEqual(self.visible_groups(), [])
        self.window.filename_filter.setText("cat-copy")
        self.window.file_path_filter.setText(str(self.cat_copy).replace("\\", "/").upper())
        self.window.apply_filter_button.click()
        self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})
        self.window.clear_filter_button.click()
        self.assertEqual(len(self.visible_groups()), 3)

    def test_folder_filter_uses_parent_path_not_filename(self):
        self.window.folder_path_filter.setText("Cat.jpg")
        self.window.apply_filter_button.click()
        self.assertEqual(self.visible_groups(), [])
        self.window.folder_path_filter.setText("photos")
        self.window.apply_filter_button.click()
        self.assertEqual(len(self.visible_groups()), 2)

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
        parent, gallery = self.show_gallery()
        copy_index = next(i for i, record in enumerate(gallery.files) if record.path == self.cat_copy)
        gallery.item(copy_index).setCheckState(Qt.CheckState.Checked)
        self.window.type_tabs.setCurrentIndex(1)  # Selected includes all copies.
        self.assertEqual(self.window.visible_paths, {self.cat, self.cat_copy})
        self.window.clear_selection()
        self.assertEqual(self.visible_groups(), [])
        self.assertEqual(gallery.count(), 0)

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
        tree_item = next(child for child in self.window.file_items()
                         if child.data(0, Qt.ItemDataRole.UserRole).path == record.path)
        self.assertEqual(tree_item.checkState(0), Qt.CheckState.Checked)
        tree_item.setCheckState(0, Qt.CheckState.Unchecked)
        self.assertEqual(item.checkState(), Qt.CheckState.Unchecked)
        item.setCheckState(Qt.CheckState.Checked)
        item.setText("Thumbnail label refreshed")
        self.assertEqual(self.window.selected, {record.path})
        self.window.clear_selection()
        self.assertEqual(item.checkState(), Qt.CheckState.Unchecked)

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
        for i in range(gallery.count()):
            gallery.item(i).setCheckState(Qt.CheckState.Checked)
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
