from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from duplicate_cleaner.cleanup import recycle_selected
from duplicate_cleaner.files import capture
from duplicate_cleaner.matching import candidate_key, filename_key, folder_key, similar_text
from duplicate_cleaner.models import SearchCriteria
from duplicate_cleaner.scanner import scan
from tests.support import FileTestCase


class MatchingTests(FileTestCase):
    def criteria(self, **options):
        return replace(SearchCriteria(contents=False, hashes=False, size=False), **options)

    def test_name_case_and_copy_markers(self):
        criteria = self.criteria(filename=True, ignore_copy=True)
        first = self.file("one/Photo.jpg")
        second = self.file("two/Copy of photo.jpg")
        third = self.file("three/photo - Copy (2).jpg")
        self.file("four/copyright.jpg")
        result = scan([self.root], criteria=criteria)
        self.assertEqual({r.path for r in result.groups[0].files}, {first, second, third})
        self.assertEqual(filename_key(self.root / "copyright.jpg", criteria), "copyright.jpg")
        sensitive = scan([self.root], criteria=replace(criteria, case_sensitive=True))
        self.assertEqual({r.path for r in sensitive.groups[0].files}, {second, third})

    def test_bounded_edit_distance(self):
        for left, right, limit, expected in (
                ("photo1.jpg", "photo2.jpg", 1, True), ("photo.jpg", "photo-copy.jpg", 3, False),
                ("abc", "ab", 1, True), ("abc", "axc", 0, False), ("", "ab", 2, True),
                ("ab", "", 2, True), ("same", "same", 0, True), ("abc", "xyz", 2, False)):
            with self.subTest(left=left, right=right, limit=limit):
                self.assertEqual(similar_text(left, right, limit), expected)

    def test_similar_names_do_not_chain_beyond_tolerance(self):
        paths = [self.file(name) for name in ("aa.txt", "ab.txt", "bb.txt")]
        result = scan([self.root], criteria=self.criteria(similar_names=True, text_tolerance=1))
        self.assertEqual(len(result.groups), 1)
        self.assertEqual({r.path for r in result.groups[0].files}, set(paths[:2]))
        self.assertFalse(result.groups[0].contents_verified)

    def test_size_tolerance_is_inclusive_and_pairwise(self):
        paths = [self.file(name, b"x" * size) for name, size in (("a", 100), ("b", 110), ("c", 120))]
        criteria = self.criteria(size=True, size_tolerance=10)
        result = scan([self.root], criteria=criteria)
        self.assertEqual({r.path for r in result.groups[0].files}, set(paths[:2]))
        self.assertFalse(scan([self.root], criteria=replace(criteria, size_tolerance=0)).groups)
        self.assertFalse(scan([self.root], criteria=replace(criteria, hashes=True)).groups)
        self.assertFalse(scan([self.root], criteria=replace(criteria, contents=True)).groups)

    def test_created_and_modified_dates_can_ignore_time(self):
        record = capture(self.file("a"))
        morning = int(datetime(2026, 1, 5, 8).timestamp()) * 1_000_000_000
        evening = int(datetime(2026, 1, 5, 22).timestamp()) * 1_000_000_000
        next_day = int(datetime(2026, 1, 6, 8).timestamp()) * 1_000_000_000
        for field in ("created", "modified"):
            criteria = self.criteria(**{field: True})
            def key(stamp, criteria):
                return candidate_key(replace(record, modified_ns=stamp), self.root,
                                     SimpleNamespace(st_birthtime_ns=stamp), criteria)
            self.assertNotEqual(key(morning, criteria), key(evening, criteria))
            criteria = replace(criteria, **{field + "_date_only": True})
            self.assertEqual(key(morning, criteria), key(evening, criteria))
            self.assertNotEqual(key(morning, criteria), key(next_day, criteria))

    def test_same_drive_uses_volume_identity(self):
        record = capture(self.file("a"))
        criteria = self.criteria(same_drive=True)
        self.assertNotEqual(candidate_key(record, self.root, None, criteria),
                            candidate_key(replace(record, device=record.device + 1), self.root, None, criteria))

    def test_folder_names_full_paths_relative_paths_and_depth(self):
        a = self.root / "root-a" / "Photos" / "2025" / "a.jpg"
        b = self.root / "root-b" / "Photos" / "2025" / "b.jpg"
        c = self.root / "root-b" / "Photos" / "2026" / "c.jpg"
        criteria = self.criteria(folder=True)
        self.assertEqual(folder_key(a, a.parents[2], criteria), folder_key(b, b.parents[2], criteria))
        self.assertNotEqual(folder_key(a, a.parents[2], replace(criteria, full_folder=True)),
                            folder_key(b, b.parents[2], replace(criteria, full_folder=True)))
        relative = replace(criteria, from_search_root=True)
        self.assertEqual(folder_key(a, a.parents[2], relative), folder_key(b, b.parents[2], relative))
        self.assertNotEqual(folder_key(a, a.parents[2], relative), folder_key(c, c.parents[2], relative))
        depth = replace(relative, folder_depth_enabled=True, folder_depth=1)
        self.assertEqual(folder_key(a, a.parents[2], depth), folder_key(c, c.parents[2], depth))
        top = replace(criteria, folder_depth_enabled=True, folder_depth=len(self.root.parts))
        self.assertNotEqual(folder_key(a, a.parents[2], top), folder_key(b, b.parents[2], top))

    def test_overlapping_roots_choose_the_most_specific_root(self):
        first = self.file("a/nested/Photos/one")
        second = self.file("b/Photos/two")
        roots = [self.root / "a", self.root / "a/nested", self.root / "b"]
        for selected_roots in (roots, list(reversed(roots))):
            result = scan(selected_roots, criteria=self.criteria(folder=True, from_search_root=True))
            self.assertEqual({r.path for r in result.groups[0].files}, {first, second})

    def test_same_folder_suppression_keeps_cross_folder_groups_intact(self):
        first = self.file("one/a")
        second = self.file("one/b")
        criteria = SearchCriteria(ignore_same_folder=True)
        self.assertFalse(scan([self.root], criteria=criteria).groups)
        third = self.file("two/c")
        result = scan([self.root], criteria=criteria)
        self.assertEqual({r.path for r in result.groups[0].files}, {first, second, third})

    def test_hash_only_never_claims_byte_verification_and_cleanup_still_compares(self):
        first = self.file("a", b"aaa")
        self.file("b", b"bbb")
        with patch("duplicate_cleaner.scanner.sample_digest", return_value=b"x" * 32), \
                patch("duplicate_cleaner.scanner.full_digest", return_value=b"x" * 32), \
                patch("duplicate_cleaner.scanner.identical") as identical:
            result = scan([self.root], criteria=SearchCriteria(contents=False))
        identical.assert_not_called()
        self.assertIn("Hash match", result.groups[0].metadata_status)
        self.assertFalse(result.groups[0].contents_verified)
        with patch("duplicate_cleaner.cleanup.recycle_file") as recycler:
            cleanup = recycle_selected(result.groups, [first], recycler=recycler)
        recycler.assert_not_called()
        self.assertFalse(cleanup.recycled)

    def test_byte_only_does_not_calculate_hashes(self):
        paths = {self.file("a", b"aaa"), self.file("b", b"aaa")}
        self.file("c", b"bbb")
        with patch("duplicate_cleaner.scanner.sample_digest") as sample, \
                patch("duplicate_cleaner.scanner.full_digest") as full:
            result = scan([self.root], criteria=SearchCriteria(hashes=False))
        sample.assert_not_called()
        full.assert_not_called()
        self.assertEqual({r.path for r in result.groups[0].files}, paths)
        self.assertIsNone(result.groups[0].digest)
        self.assertTrue(result.groups[0].contents_verified)
