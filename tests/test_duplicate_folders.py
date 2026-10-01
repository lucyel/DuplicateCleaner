import os
from threading import Event, Thread
from unittest.mock import patch

from duplicate_cleaner.duplicate_folders import recycle_duplicate_folders, scan_duplicate_folders
from duplicate_cleaner.files import UnsafeFile, capture
from duplicate_cleaner.models import Cancelled
from duplicate_cleaner.scan_control import ScanControl
from tests.support import FileTestCase


class DuplicateFolderTests(FileTestCase):
    def matching_folders(self):
        self.file("a/nested/first.txt", b"one")
        self.file("a/second.bin", b"two")
        self.file("b/renamed.bin", b"one")
        self.file("b/subdir/changed-name.txt", b"two")
        return self.root / "a", self.root / "b"

    def group_paths(self, result):
        return [{folder.path for folder in group.folders} for group in result.groups]

    def test_renamed_nested_contents_and_overlapping_roots(self):
        a, b = self.matching_folders()
        result = scan_duplicate_folders([self.root, a])
        self.assertFalse(result.issues, result.issues)
        self.assertIn({a, b}, self.group_paths(result))
        self.assertEqual(len(result.folders), len({folder.path for folder in result.folders}))
        group = next(group for group in result.groups if {a, b} == {folder.path for folder in group.folders})
        self.assertEqual([len(folder.files) for folder in group.folders], [2, 2])
        self.assertEqual([folder.total_size for folder in group.folders], [6, 6])

    def test_multiplicity_counts_and_content_are_required(self):
        for folder, contents in (("a", (b"one", b"one", b"two")),
                                 ("b", (b"one", b"two", b"two")),
                                 ("c", (b"one", b"one"))):
            for index, content in enumerate(contents):
                self.file(f"{folder}/{index}.txt", content)
        self.assertFalse(scan_duplicate_folders([self.root]).groups)

    def test_hash_collision_does_not_establish_folder_match(self):
        self.file("a/file", b"aaa")
        self.file("b/file", b"bbb")
        with patch("duplicate_cleaner.duplicate_folders.full_digest", return_value=b"collision"):
            self.assertFalse(scan_duplicate_folders([self.root]).groups)

    def test_single_empty_folder_remains_available(self):
        path = self.root / "empty"
        path.mkdir()
        result = scan_duplicate_folders([self.root])
        self.assertEqual(result.folders[0].path, path)
        self.assertTrue(result.folders[0].empty)
        self.assertEqual(len(result.groups[0].folders), 1)

    def test_exclusion_invalidates_ancestors_but_not_safe_siblings(self):
        a, b = self.matching_folders()
        self.file("c/file", b"one")
        self.file("a/excluded/secret", b"hidden")
        result = scan_duplicate_folders([self.root], excluded_folders=[a / "excluded"])
        self.assertNotIn(a, {folder.path for folder in result.folders})
        self.assertNotIn(self.root, {folder.path for folder in result.folders})
        self.assertTrue(result.issues)
        self.assertIn({a / "nested", self.root / "c"}, self.group_paths(result))

    def test_capture_failure_invalidates_complete_folder(self):
        a, b = self.matching_folders()
        def fail(path):
            if path == a / "second.bin":
                raise UnsafeFile("Unreadable test file")
            return capture(path)
        with patch("duplicate_cleaner.duplicate_folders.capture", side_effect=fail):
            result = scan_duplicate_folders([self.root])
        self.assertNotIn(a, {folder.path for folder in result.folders})
        self.assertTrue(any("Unreadable" in issue.reason for issue in result.issues))

    def test_hardlink_disqualifies_containing_folder(self):
        a, b = self.matching_folders()
        os.link(a / "second.bin", a / "hardlink.bin")
        result = scan_duplicate_folders([self.root])
        self.assertNotIn(a, {folder.path for folder in result.folders})
        self.assertTrue(any("Hard-linked" in issue.reason for issue in result.issues))

    def test_directory_streams_block_folder_comparison(self):
        path = self.root / "empty"
        path.mkdir()
        from duplicate_cleaner.files import named_streams
        with patch("duplicate_cleaner.duplicate_folders.named_streams",
                   side_effect=lambda value: ((":hidden:$DATA", 4),) if value == path else named_streams(value)):
            result = scan_duplicate_folders([self.root])
        self.assertFalse(result.groups)
        self.assertTrue(any("Folder has extra" in issue.reason for issue in result.issues))

    def test_nonrecursive_candidates_still_compare_all_descendants(self):
        a, b = self.matching_folders()
        result = scan_duplicate_folders([a, b], recursive=False)
        self.assertEqual(self.group_paths(result), [{a, b}])
        self.file("b/extra/hidden.txt", b"different")
        self.assertFalse(scan_duplicate_folders([a, b], recursive=False).groups)

    def recycler(self, path, revalidate):
        revalidate()
        # Move only the generated fixture, keeping all bytes reviewable in the test directory.
        path.rename(self.root / (path.name + "-recycled"))

    def test_recycles_whole_tree_and_preserves_keeper(self):
        a, b = self.matching_folders()
        result = recycle_duplicate_folders(scan_duplicate_folders([a, b], False).groups,
                                           [a], recycler=self.recycler)
        self.assertFalse(result.issues, result.issues)
        self.assertEqual(result.recycled, [a])
        self.assertFalse(a.exists())
        self.assertEqual((b / "renamed.bin").read_bytes(), b"one")
        self.assertEqual((self.root / "a-recycled/nested/first.txt").read_bytes(), b"one")

    def test_every_copy_can_be_recycled_with_reference_last(self):
        a, b = self.matching_folders()
        result = recycle_duplicate_folders(scan_duplicate_folders([a, b], False).groups,
                                           [a, b], recycler=self.recycler)
        self.assertFalse(result.issues, result.issues)
        self.assertEqual(result.recycled, [a, b])

    def test_added_removed_modified_and_callback_changes_block_recycling(self):
        for change in ("added", "removed", "modified", "callback"):
            with self.subTest(change=change):
                a, b = self.matching_folders()
                groups = scan_duplicate_folders([a, b], False).groups
                def mutate():
                    if change in ("added", "callback"):
                        self.file("a/new.txt", b"new")
                    elif change == "removed":
                        (a / "second.bin").unlink()
                    else:
                        (a / "second.bin").write_bytes(b"changed")
                calls = []
                def recycler(path, revalidate):
                    calls.append(path)
                    mutate()
                    revalidate()
                    self.fail("Changed folder must never pass callback revalidation")
                if change != "callback":
                    mutate()
                result = recycle_duplicate_folders(groups, [a], recycler=recycler)
                self.assertFalse(result.recycled)
                self.assertTrue(result.issues)
                self.assertTrue(a.exists())
                self.assertEqual(len(calls), int(change == "callback"))
                # Reset only generated fixtures; the next subtest creates fresh snapshots.
                for folder in (a, b):
                    for file in folder.rglob("*"):
                        if file.is_file():
                            file.unlink()
                (a / "new.txt").unlink(missing_ok=True)

    def test_reference_failure_never_recycles_last_copy(self):
        a, b = self.matching_folders()
        groups = scan_duplicate_folders([a, b], False).groups
        def failing(path, revalidate):
            raise OSError("Recycle Bin unavailable")
        result = recycle_duplicate_folders(groups, [a, b], recycler=failing)
        self.assertFalse(result.recycled)
        self.assertEqual(len(result.issues), 2)
        self.assertIn("No independent", result.issues[-1].reason)

    def test_nested_selection_is_rejected(self):
        a, b = self.matching_folders()
        self.file("c/file", b"one")
        groups = scan_duplicate_folders([self.root]).groups
        with self.assertRaisesRegex(ValueError, "parent folder"):
            recycle_duplicate_folders(groups, [a, a / "nested"], recycler=self.recycler)

    def test_empty_folder_cleanup_and_cancellation(self):
        path = self.root / "empty"
        path.mkdir()
        groups = scan_duplicate_folders([self.root]).groups
        cancel = Event()
        cancel.set()
        result = recycle_duplicate_folders(groups, [path], cancel=cancel, recycler=self.recycler)
        self.assertTrue(result.cancelled)
        self.assertTrue(path.exists())
        result = recycle_duplicate_folders(groups, [path], recycler=self.recycler)
        self.assertEqual(result.recycled, [path])

    def test_different_extra_file_streams_block_recycling(self):
        if os.name != "nt":
            self.skipTest("NTFS streams require Windows")
        a, b = self.matching_folders()
        with open(str(a / "second.bin") + ":test-data", "wb") as stream:
            stream.write(b"must not be ignored")
        groups = scan_duplicate_folders([a, b], False).groups
        self.assertEqual(len(groups), 1)
        result = recycle_duplicate_folders(groups, [a], recycler=self.recycler)
        self.assertFalse(result.recycled)
        self.assertTrue(result.issues)


class ScanControlTests(FileTestCase):
    def test_pause_blocks_progress_resume_continues_and_cancel_wakes(self):
        for cancel_paused in (False, True):
            paused, finished = Event(), Event()
            control = ScanControl(paused.set)
            control.pause()
            errors = []
            def work():
                try:
                    control.checkpoint()
                except Cancelled:
                    errors.append("cancelled")
                finally:
                    finished.set()
            worker = Thread(target=work)
            worker.start()
            try:
                self.assertTrue(paused.wait(2))
                self.assertFalse(finished.is_set())
                if cancel_paused:
                    control.set()
                else:
                    control.resume()
                self.assertTrue(finished.wait(2))
                self.assertEqual(errors, ["cancelled"] if cancel_paused else [])
            finally:
                control.set()
                worker.join(2)

    def test_scan_pauses_at_progress_checkpoint_and_keeps_settings(self):
        self.file("a/file.txt")
        self.file("b/renamed.txt")
        paused = Event()
        control = ScanControl(paused.set)
        results = []
        reports = []
        def report(value):
            reports.append(value)
            if len(reports) == 1:
                control.pause()
        worker = Thread(target=lambda: results.append(scan_duplicate_folders([self.root], cancel=control, progress=report)))
        worker.start()
        try:
            self.assertTrue(paused.wait(2))
            count = len(reports)
            self.assertFalse(control.wait(.1))
            self.assertEqual(len(reports), count)
            control.resume()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(results[0].cancelled)
            self.assertEqual(len(results[0].groups), 1)
        finally:
            control.set()
            worker.join(5)
