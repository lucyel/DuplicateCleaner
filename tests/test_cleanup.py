import os
from contextlib import contextmanager
from threading import Event
from unittest.mock import Mock, patch

from duplicate_cleaner.cleanup import recycle_selected
from duplicate_cleaner.files import open_checked
from duplicate_cleaner.models import SearchCriteria
from duplicate_cleaner.scanner import compare_streams, scan
from tests.support import FileTestCase


class CleanupTests(FileTestCase):
    def test_criteria_only_results_still_require_matching_contents_before_recycling(self):
        first = self.file("a", b"aaa")
        second = self.file("b", b"bbb")
        groups = scan([self.root], criteria=SearchCriteria(contents=False, hashes=False)).groups
        for selected in ((first,), (first, second)):
            with self.subTest(selected=selected):
                recycler = Mock()
                result = recycle_selected(groups, selected, recycler=recycler)
                recycler.assert_not_called()
                self.assertFalse(result.recycled)
                self.assertEqual(len(result.issues), len(selected))

    def test_criteria_only_identical_files_can_be_reverified_for_recycling(self):
        first = self.file("a", b"same")
        self.file("b", b"same")
        groups = scan([self.root], criteria=SearchCriteria(contents=False, hashes=False)).groups
        calls = []
        def recycler(path, revalidate):
            revalidate()
            calls.append(path)
        result = recycle_selected(groups, [first], recycler=recycler)
        self.assertEqual(calls, [first])
        self.assertEqual(result.recycled, [first])
        self.assertFalse(result.issues)

    def test_reference_close_failure_does_not_duplicate_completed_reports(self):
        paths, groups = self.copies(3)
        reference = groups[0].files[2]
        targets = [record.path for record in groups[0].files[:2]]

        @contextmanager
        def failing_close(record, allow_delete=False):
            with open_checked(record, allow_delete=allow_delete) as stream:
                yield stream
            if record == reference:
                raise OSError("Reference close failed")

        def recycler(path, revalidate):
            revalidate()
            if path == targets[1]:
                raise OSError("Recycle failed")

        with patch("duplicate_cleaner.cleanup.open_checked", side_effect=failing_close):
            result = recycle_selected(groups, targets, recycler=recycler)
        self.assertEqual(result.recycled, targets[:1])
        self.assertEqual([issue.path for issue in result.issues], targets[1:])
        self.assertEqual(result.issues[0].reason, "Recycle failed")
        self.assertTrue(all(path.exists() for path in paths))

    def copies(self, count=2):
        paths = [self.file(f"copy-{index}") for index in range(count)]
        return paths, scan([self.root]).groups

    def test_recycles_only_explicit_selection_and_checks_callback(self):
        paths, groups = self.copies(3)
        calls = []

        def recycler(path, revalidate):
            revalidate()
            calls.append(path)

        result = recycle_selected(groups, [paths[1]], recycler=recycler)
        self.assertEqual(calls, [paths[1]])
        self.assertEqual(result.recycled, calls)
        self.assertFalse(result.issues)

    def test_no_selection_does_nothing(self):
        paths, groups = self.copies()
        recycler = Mock()
        self.assertFalse(recycle_selected(groups, [], recycler=recycler).recycled)
        recycler.assert_not_called()

    def test_every_selected_copy_can_be_recycled_after_byte_comparison(self):
        paths, groups = self.copies(3)

        def recycler(path, revalidate):
            if os.name == "nt":
                with self.assertRaises(OSError):
                    paths[-1].write_bytes(b"must remain locked until recycled")
            revalidate()
            path.rename(path.with_name(path.name + ".recycled-fixture"))

        with patch("duplicate_cleaner.cleanup.compare_streams", wraps=compare_streams) as compare:
            result = recycle_selected(groups, paths, recycler=recycler)
        self.assertEqual(result.recycled, paths)
        self.assertFalse(result.issues, result.issues)
        self.assertEqual(compare.call_count, 2)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_all_copy_selection_skips_group_when_reference_changed(self):
        paths, groups = self.copies(3)
        paths[-1].write_bytes(b"unique content")
        recycler = Mock()
        result = recycle_selected(groups, paths, recycler=recycler)
        self.assertFalse(result.recycled)
        self.assertEqual({issue.path for issue in result.issues}, set(paths))
        recycler.assert_not_called()

    def test_last_selected_copy_needs_another_verified_match(self):
        paths, groups = self.copies()
        paths[0].write_bytes(b"no longer matches")
        recycler = Mock()
        result = recycle_selected(groups, paths, recycler=recycler)
        self.assertFalse(result.recycled)
        self.assertEqual({issue.path for issue in result.issues}, set(paths))
        recycler.assert_not_called()

    def test_all_copy_selection_does_not_treat_matching_hash_as_byte_verification(self):
        paths, groups = self.copies()
        recycler = Mock()
        with patch("duplicate_cleaner.cleanup.compare_streams", return_value=False) as compare:
            result = recycle_selected(groups, paths, recycler=recycler)
        compare.assert_called_once()
        self.assertFalse(result.recycled)
        self.assertEqual(len(result.issues), 2)
        recycler.assert_not_called()

    def test_all_copy_recycle_failure_keeps_failed_file_without_blocking_verified_copies(self):
        paths, groups = self.copies(3)

        def recycler(path, revalidate):
            revalidate()
            if path == paths[1]:
                raise OSError("Recycle Bin unavailable for this copy")
            path.rename(path.with_name(path.name + ".recycled-fixture"))

        result = recycle_selected(groups, paths, recycler=recycler)
        self.assertEqual(result.recycled, [paths[0], paths[2]])
        self.assertEqual([issue.path for issue in result.issues], [paths[1]])
        self.assertEqual(paths[1].read_bytes(), b"identical content")

    def test_all_copy_selection_can_cancel_before_recycling_the_reference(self):
        paths, groups = self.copies(3)
        cancel = Event()

        def recycler(path, revalidate):
            revalidate()
            path.rename(path.with_name(path.name + ".recycled-fixture"))
            if path == paths[-2]:
                cancel.set()

        result = recycle_selected(groups, paths, cancel=cancel, recycler=recycler)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.recycled, paths[:-1])
        self.assertEqual(paths[-1].read_bytes(), b"identical content")

    def test_reference_gets_a_final_safety_check_before_recycling(self):
        if os.name != "nt":
            self.skipTest("Windows NTFS test")
        paths, groups = self.copies()

        def recycler(path, revalidate):
            if path == paths[-1]:
                with open(str(path) + ":unverified-data", "wb") as stream:
                    stream.write(b"unique data")
            revalidate()
            path.rename(path.with_name(path.name + ".recycled-fixture"))

        result = recycle_selected(groups, paths, recycler=recycler)
        self.assertEqual(result.recycled, paths[:1])
        self.assertEqual([issue.path for issue in result.issues], paths[1:])
        self.assertIn("File changed", result.issues[0].reason)
        self.assertTrue(paths[-1].exists())

    def test_unknown_selection_is_rejected_before_any_action(self):
        paths, groups = self.copies()
        recycler = Mock()
        with self.assertRaises(ValueError):
            recycle_selected(groups, [paths[0], self.root / "unknown"], recycler=recycler)
        recycler.assert_not_called()

    def test_duplicated_group_is_rejected(self):
        paths, groups = self.copies()
        with self.assertRaises(ValueError):
            recycle_selected(groups + groups, [paths[0]], recycler=Mock())

    def test_changed_selected_file_is_skipped(self):
        paths, groups = self.copies()
        paths[0].write_bytes(b"this is unique now")
        recycler = Mock()
        result = recycle_selected(groups, [paths[0]], recycler=recycler)
        self.assertEqual(len(result.issues), 1)
        recycler.assert_not_called()

    def test_changed_remaining_copy_blocks_cleanup(self):
        paths, groups = self.copies()
        paths[1].write_bytes(b"no longer the duplicate")
        recycler = Mock()
        result = recycle_selected(groups, [paths[0]], recycler=recycler)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("Remaining copy", result.issues[0].reason)
        recycler.assert_not_called()

    def test_recheck_reads_contents_even_when_size_and_mtime_are_restored(self):
        paths, groups = self.copies()
        original = paths[0].stat()
        paths[0].write_bytes(b"different content")
        os.utime(paths[0], ns=(original.st_atime_ns, original.st_mtime_ns))
        recycler = Mock()
        result = recycle_selected(groups, [paths[0]], recycler=recycler)
        self.assertFalse(result.recycled)
        self.assertEqual(len(result.issues), 1)
        recycler.assert_not_called()

    def test_extra_stream_added_after_scan_blocks_cleanup(self):
        if os.name != "nt":
            self.skipTest("Windows NTFS test")
        paths, groups = self.copies()
        with open(str(paths[0]) + ":new-data", "wb") as stream:
            stream.write(b"unique data")
        recycler = Mock()
        result = recycle_selected(groups, [paths[0]], recycler=recycler)
        self.assertFalse(result.recycled)
        self.assertTrue(result.issues)
        recycler.assert_not_called()

    def test_missing_remaining_copy_blocks_cleanup(self):
        paths, groups = self.copies()
        paths[1].unlink()
        recycler = Mock()
        result = recycle_selected(groups, [paths[0]], recycler=recycler)
        self.assertFalse(result.recycled)
        self.assertEqual(len(result.issues), 1)
        recycler.assert_not_called()

    def test_recycle_failure_never_falls_back_to_deletion(self):
        paths, groups = self.copies()
        recycler = Mock(side_effect=OSError("Recycle Bin unavailable"))
        result = recycle_selected(groups, [paths[0]], recycler=recycler)
        self.assertFalse(result.recycled)
        self.assertEqual(paths[0].read_bytes(), paths[1].read_bytes())
        self.assertIn("Recycle Bin unavailable", result.issues[0].reason)
        recycler.assert_called_once()

    def test_cancel_before_cleanup_does_nothing(self):
        paths, groups = self.copies()
        cancel = Event()
        cancel.set()
        recycler = Mock()
        result = recycle_selected(groups, [paths[0]], cancel=cancel, recycler=recycler)
        self.assertTrue(result.cancelled)
        recycler.assert_not_called()

    def test_cancel_between_files_preserves_completed_results(self):
        paths, groups = self.copies(3)
        cancel = Event()

        def recycler(path, revalidate):
            revalidate()
            cancel.set()

        result = recycle_selected(groups, paths[:2], cancel=cancel, recycler=recycler)
        self.assertTrue(result.cancelled)
        self.assertEqual(len(result.recycled), 1)

    def test_keeper_is_locked_during_cleanup(self):
        if os.name != "nt":
            self.skipTest("Windows sharing test")
        paths, groups = self.copies()

        def recycler(path, revalidate):
            with self.assertRaises(OSError):
                paths[1].write_bytes(b"changed")
            with self.assertRaises(OSError):
                paths[1].unlink()
            revalidate()

        result = recycle_selected(groups, [paths[0]], recycler=recycler)
        self.assertEqual(len(result.recycled), 1)
        self.assertFalse(result.issues)
