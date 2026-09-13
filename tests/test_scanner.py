import os
import weakref
from threading import Event
from unittest.mock import patch

from duplicate_cleaner.files import capture, open_checked
from duplicate_cleaner.scanner import scan, SAMPLE_SIZE
from tests.support import FileTestCase


class ScannerTests(FileTestCase):
    def test_excluded_subtrees_are_not_read_even_as_overlapping_roots(self):
        from duplicate_cleaner import scanner

        kept = {self.file("keep/a"), self.file("skip-sibling/b")}
        self.file("skip/hidden")
        self.file("skip/deeper/hidden")
        excluded = self.root / "skip"
        scandir = os.scandir
        visited = []

        def record_visit(path):
            visited.append(path)
            return scandir(path)

        with patch.object(scanner.os, "scandir", side_effect=record_visit):
            result = scan([self.root, excluded, excluded / "deeper"], excluded_folders=[excluded])
        self.assertEqual(result.file_count, 2)
        self.assertEqual({record.path for record in result.groups[0].files}, kept)
        self.assertFalse(result.issues)
        self.assertNotIn(excluded, visited)
        self.assertNotIn(excluded / "deeper", visited)

    def test_exclusions_apply_to_explicit_roots_when_nonrecursive(self):
        self.file("a")
        self.file("skip/b")
        result = scan([self.root, self.root / "skip"], recursive=False,
                      excluded_folders=[self.root / "skip"])
        self.assertEqual(result.file_count, 1)
        self.assertFalse(result.groups)

    def test_discovery_releases_unique_records_before_content_checks(self):
        from duplicate_cleaner import scanner

        for size in range(1, 11):
            self.file(f"unique-{size}", b"x" * size)
        self.file("duplicate-a", b"duplicate contents")
        self.file("duplicate-b", b"duplicate contents")
        unique_records = []

        def remember(path):
            record = capture(path)
            if record.size <= 10:
                unique_records.append(weakref.ref(record))
            return record

        def check_memory(progress):
            if progress.stage == "Comparing samples":
                # The discovery loop may still hold its final local record.
                self.assertLessEqual(sum(ref() is not None for ref in unique_records), 1)

        with patch.object(scanner, "capture", side_effect=remember):
            result = scan([self.root], progress=check_memory)
        self.assertEqual(result.file_count, 12)
        self.assertEqual(len(result.groups), 1)
        self.assertFalse(result.issues)

    def test_renamed_duplicates_across_folders_and_distinct_same_size(self):
        first = self.file("one/a.txt", b"hello")
        second = self.file("two/renamed.bin", b"hello")
        self.file("two/a.txt", b"world")
        self.file("one/unique.txt", b"different length")
        result = scan([self.root / "one", self.root / "two"])
        self.assertEqual(result.file_count, 4)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual({file.path for file in result.groups[0].files}, {first, second})
        self.assertEqual(result.groups[0].extra_bytes, 5)
        self.assertFalse(result.issues)

    def test_multiple_copies_form_one_group(self):
        paths = {self.file(f"copy-{i}") for i in range(5)}
        result = scan([self.root])
        self.assertEqual(len(result.groups), 1)
        self.assertEqual({file.path for file in result.groups[0].files}, paths)

    def test_overlapping_roots_count_each_file_once(self):
        self.file("one/a")
        self.file("one/nested/b")
        result = scan([self.root, self.root / "one", self.root])
        self.assertEqual(result.file_count, 2)
        self.assertEqual(len(result.groups[0].files), 2)

    def test_non_recursive_still_compares_all_explicit_roots(self):
        self.file("a")
        self.file("nested/b")
        self.assertFalse(scan([self.root], recursive=False).groups)
        self.assertEqual(len(scan([self.root, self.root / "nested"], recursive=False).groups), 1)

    def test_empty_files_are_duplicates_without_space_savings(self):
        self.file("a", b"")
        self.file("b", b"")
        result = scan([self.root])
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0].extra_bytes, 0)

    def test_full_hash_catches_difference_outside_samples(self):
        content = bytearray(b"a" * (SAMPLE_SIZE * 10))
        self.file("a", content)
        content[SAMPLE_SIZE * 2] = ord("b")
        self.file("b", content)
        stages = []
        result = scan([self.root], progress=lambda value: stages.append(value.stage))
        self.assertFalse(result.groups)
        self.assertIn("Hashing full contents", stages)

    def test_byte_verification_resolves_forced_hash_collisions(self):
        first = self.file("a", b"first!")
        second = self.file("b", b"first!")
        third = self.file("c", b"second")
        fourth = self.file("d", b"second")
        with patch("duplicate_cleaner.scanner.sample_digest", return_value=b"collision"), \
             patch("duplicate_cleaner.scanner.full_digest", return_value=b"collision"):
            result = scan([self.root])
        actual = {frozenset(file.path for file in group.files) for group in result.groups}
        self.assertEqual(actual, {frozenset((first, second)), frozenset((third, fourth))})

    def test_changed_file_is_not_reported_as_verified(self):
        self.file("a")
        target = self.file("b")

        def change(progress):
            if progress.stage == "Verifying every byte":
                target.write_bytes(b"changed after hashing")

        result = scan([self.root], progress=change)
        self.assertFalse(result.groups)
        self.assertTrue(result.issues)

    def test_missing_folder_is_reported_and_other_folders_continue(self):
        self.file("a")
        self.file("b")
        result = scan([self.root / "missing", self.root])
        self.assertEqual(len(result.groups), 1)
        self.assertTrue(result.issues)

    def test_unreadable_candidate_is_reported(self):
        from duplicate_cleaner import scanner
        self.file("a")
        blocked = self.file("b")
        original = scanner.sample_digest

        def sample(record, reporter):
            if record.path == blocked:
                raise PermissionError("Access denied")
            return original(record, reporter)

        with patch.object(scanner, "sample_digest", side_effect=sample):
            result = scan([self.root])
        self.assertFalse(result.groups)
        self.assertIn("Access denied", result.issues[0].reason)

    def test_cancelled_scan_never_exposes_partial_results(self):
        self.file("a")
        self.file("b")
        cancel = Event()

        def stop(progress):
            if progress.stage == "Verifying every byte":
                cancel.set()

        result = scan([self.root], cancel=cancel, progress=stop)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.groups)

    def test_hard_links_are_not_counted_as_independent_copies(self):
        first = self.file("a")
        os.link(first, self.root / "linked")
        self.file("independent")
        result = scan([self.root])
        self.assertFalse(result.groups)
        self.assertEqual(len(result.issues), 2)

    def test_non_ascii_paths(self):
        self.file("Hình ảnh/ảnh một.bin")
        self.file("Bản sao/ảnh hai.bin")
        self.assertEqual(len(scan([self.root]).groups), 1)

    def test_large_files_are_compared_in_chunks(self):
        self.file("a", b"0123456789" * 350_000)
        self.file("b", b"0123456789" * 350_000)
        result = scan([self.root])
        self.assertEqual(result.groups[0].extra_bytes, 3_500_000)

    def test_symlinks_are_skipped_when_creation_is_available(self):
        original = self.file("a")
        self.file("b")
        try:
            (self.root / "link").symlink_to(original)
        except OSError:
            self.skipTest("Creating symlinks requires Windows Developer Mode or elevated permissions")
        result = scan([self.root])
        self.assertEqual(result.file_count, 2)
        self.assertTrue(result.issues)

    def test_named_streams_are_skipped_on_windows(self):
        if os.name != "nt":
            self.skipTest("Windows NTFS test")
        original = self.file("a")
        self.file("b")
        with open(str(original) + ":extra", "wb") as stream:
            stream.write(b"not part of the main content")
        result = scan([self.root])
        self.assertFalse(result.groups)
        self.assertTrue(any("data streams" in issue.reason for issue in result.issues))

    def test_windows_reader_blocks_concurrent_writes(self):
        if os.name != "nt":
            self.skipTest("Windows sharing test")
        path = self.file("a")
        with open_checked(capture(path)):
            with self.assertRaises(OSError):
                path.write_bytes(b"must not overwrite")
        self.assertEqual(path.read_bytes(), b"identical content")
