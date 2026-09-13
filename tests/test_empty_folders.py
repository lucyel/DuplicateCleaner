import os
from pathlib import Path
from threading import Event
from unittest.mock import Mock

from duplicate_cleaner.empty_folders import recycle_empty_folders, scan_empty_folders
from tests.support import FileTestCase


class EmptyFolderTests(FileTestCase):
    def test_excluded_subtrees_do_not_make_parent_appear_empty(self):
        excluded = self.folder("parent/skip")
        self.folder("parent/skip/deep")
        kept = self.folder("parent/skip-sibling")
        for recursive in (True, False):
            with self.subTest(recursive=recursive):
                result = scan_empty_folders([self.root / "parent", excluded, excluded / "deep"],
                                            recursive=recursive, excluded_folders=[excluded])
                self.assertEqual([record.path for record in result.folders], [kept])
                self.assertEqual(result.folder_count, 2)
                self.assertFalse(result.issues)

    def folder(self, name):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_recursive_scan_finds_only_truly_empty_folders_and_excludes_root(self):
        empty = self.folder("empty")
        child = self.folder("parent/empty-child")
        self.file("not-empty/file.txt", b"")

        result = scan_empty_folders([self.root])

        self.assertEqual({record.path for record in result.folders}, {empty, child})
        self.assertNotIn(self.root, {record.path for record in result.folders})
        self.assertNotIn(self.root / "parent", {record.path for record in result.folders})
        self.assertEqual(result.folder_count, 5)
        self.assertFalse(result.issues)

    def test_non_recursive_scan_checks_direct_children_only(self):
        direct = self.folder("direct-empty")
        nested = self.folder("parent/nested-empty")

        result = scan_empty_folders([self.root], recursive=False)

        self.assertEqual([record.path for record in result.folders], [direct])
        self.assertNotIn(nested, {record.path for record in result.folders})

    def test_explicit_empty_root_is_never_a_deletion_candidate(self):
        explicit = self.folder("explicit")
        result = scan_empty_folders([explicit])
        self.assertFalse(result.folders)
        self.assertEqual(result.folder_count, 1)

    def test_any_entry_including_hidden_file_makes_folder_nonempty(self):
        folder = self.folder("contains-hidden")
        (folder / ".hidden").write_bytes(b"")
        self.assertFalse(scan_empty_folders([self.root]).folders)

    def test_overlapping_roots_do_not_duplicate_results(self):
        parent = self.folder("parent")
        empty = self.folder("parent/empty")
        result = scan_empty_folders([self.root, parent, self.root])
        self.assertEqual([record.path for record in result.folders], [empty])

    def test_pre_cancelled_scan_does_not_expose_partial_results(self):
        self.folder("empty")
        cancel = Event()
        cancel.set()
        result = scan_empty_folders([self.root], cancel=cancel)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.folders)

    def test_link_is_reported_and_not_traversed_when_available(self):
        target = self.folder("target")
        link = self.root / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Creating directory symlinks requires Windows Developer Mode or elevation")
        result = scan_empty_folders([self.root])
        self.assertEqual([record.path for record in result.folders], [target])
        self.assertTrue(any(issue.path == link for issue in result.issues))

    def test_cleanup_recycles_only_selected_folder_after_revalidation(self):
        selected = self.folder("selected")
        kept = self.folder("kept")
        records = scan_empty_folders([self.root]).folders
        destination = self.root / "recycled-fixture"

        def recycler(path, revalidate):
            revalidate()
            path.rename(destination)

        result = recycle_empty_folders(records, [selected], recycler=recycler)

        self.assertEqual(result.recycled, [selected])
        self.assertFalse(result.issues)
        self.assertFalse(selected.exists())
        self.assertTrue(kept.exists())

    def test_cleanup_rejects_folder_that_became_nonempty(self):
        folder = self.folder("empty-at-scan")
        records = scan_empty_folders([self.root]).folders
        (folder / "new-file.txt").write_text("new")
        recycler = Mock()

        result = recycle_empty_folders(records, [folder], recycler=recycler)

        self.assertFalse(result.recycled)
        self.assertIn("no longer empty", result.issues[0].reason)
        recycler.assert_not_called()
        self.assertTrue(folder.exists())

    def test_cleanup_has_no_permanent_delete_fallback(self):
        folder = self.folder("empty")
        records = scan_empty_folders([self.root]).folders
        recycler = Mock(side_effect=OSError("Recycle Bin unavailable"))

        result = recycle_empty_folders(records, [folder], recycler=recycler)

        self.assertFalse(result.recycled)
        self.assertIn("Recycle Bin unavailable", result.issues[0].reason)
        self.assertTrue(folder.exists())

    def test_cleanup_rejects_paths_outside_scan_results(self):
        folder = self.folder("known")
        outsider = self.folder("outside")
        records = [record for record in scan_empty_folders([self.root]).folders
                   if record.path == folder]
        with self.assertRaisesRegex(ValueError, "outside"):
            recycle_empty_folders(records, [outsider], recycler=Mock())

    def test_cleanup_cancellation_keeps_completed_recycle_result(self):
        first = self.folder("a")
        self.folder("b")
        records = scan_empty_folders([self.root]).folders
        cancel = Event()

        def recycler(path: Path, revalidate):
            revalidate()
            path.rename(path.with_name(path.name + "-recycled"))
            cancel.set()

        result = recycle_empty_folders(records, [record.path for record in records],
                                       cancel=cancel, recycler=recycler)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.recycled, [first])
