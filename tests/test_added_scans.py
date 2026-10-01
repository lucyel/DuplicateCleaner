import os
from dataclasses import replace
from threading import Event
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from duplicate_cleaner import duplicate_folders, scanner, similarity
from duplicate_cleaner.models import SearchCriteria
from tests.support import FileTestCase
from tests.test_similarity import sample_image
from tests.test_video_similarity import make_video


class AddedScanTests(FileTestCase):
    def file_groups(self, result):
        return {frozenset(record.path for record in group.files) for group in result.groups}

    def fixture(self):
        self.file("old/unique.txt", b"match")
        self.file("old/copy-a.bin", b"olddup")
        self.file("old/copy-b.bin", b"olddup")
        self.file("old/alone.txt", b"alone")
        self.file("new/renamed.txt", b"match")
        self.file("new/third.bin", b"olddup")
        self.file("new/new-a", b"newdup")
        self.file("new/new-b", b"newdup")
        return self.root / "old", self.root / "new"

    def test_new_files_match_old_unique_files_old_groups_and_each_other(self):
        old, new = self.fixture()
        previous = scanner.scan([old])
        old_index = dict(previous.index)
        original_scandir = os.scandir
        def discover(path):
            self.assertFalse(path == old or old in path.parents, "An old location was rediscovered")
            return original_scandir(path)
        with patch("duplicate_cleaner.scanner.os.scandir", side_effect=discover), \
                patch("duplicate_cleaner.scanner.full_digest", wraps=scanner.full_digest) as hashes:
            result = scanner.scan([new], previous=previous)
        self.assertEqual(self.file_groups(result), self.file_groups(scanner.scan([old, new])))
        self.assertEqual(len(result.groups), 3)
        self.assertEqual(result.file_count, 8)
        self.assertEqual(len(result.index), 8)
        self.assertFalse(result.issues, result.issues)
        hashed = {call.args[0].path for call in hashes.call_args_list}
        self.assertNotIn(old / "copy-a.bin", hashed)
        self.assertNotIn(old / "copy-b.bin", hashed)
        self.assertIn(old / "unique.txt", hashed)  # First potential match requires a lazy hash.
        self.assertEqual(previous.index, old_index)
        self.assertIsNone(previous.index[old / "unique.txt"].digest)
        self.assertEqual(len(previous.groups), 1)

    def test_multiple_additions_keep_unmatched_old_and_new_records(self):
        old, new = self.fixture()
        self.file("third/later.txt", b"alone")
        first = scanner.scan([old])
        second = scanner.scan([new], previous=first)
        third = scanner.scan([self.root / "third"], previous=second)
        self.assertIn(frozenset((old / "alone.txt", self.root / "third/later.txt")), self.file_groups(third))
        self.assertEqual(third.file_count, 9)

    def test_byte_only_reuses_prior_byte_verified_groups(self):
        old, new = self.fixture()
        criteria = SearchCriteria(hashes=False)
        previous = scanner.scan([old], criteria=criteria)
        actual = scanner.identical
        def compare(first, second, reporter):
            self.assertFalse(first.path.parent == old and second.path.parent == old,
                             "Existing byte-verified copies were compared again")
            return actual(first, second, reporter)
        with patch("duplicate_cleaner.scanner.identical", side_effect=compare):
            result = scanner.scan([new], criteria=criteria, previous=previous)
        self.assertEqual(self.file_groups(result), self.file_groups(scanner.scan([old, new], criteria=criteria)))

    def test_metadata_and_hash_criteria_keep_their_contract(self):
        old, new = self.fixture()
        for criteria in (SearchCriteria(contents=False),
                         SearchCriteria(contents=False, hashes=False, size_tolerance=2),
                         SearchCriteria(filename=True, ignore_copy=True),
                         SearchCriteria(contents=False, hashes=False, size=False, extension=True),
                         SearchCriteria(contents=False, hashes=False, similar_names=True, text_tolerance=4)):
            with self.subTest(criteria=criteria):
                result = scanner.scan([new], criteria=criteria, previous=scanner.scan([old], criteria=criteria))
                self.assertFalse(result.issues, result.issues)
                for group in result.groups:
                    self.assertEqual(group.contents_verified, criteria.contents)
                # Tolerance grouping can have multiple valid partitions; every pair must comply.
                from duplicate_cleaner.matching import compatible
                for group in result.groups:
                    self.assertTrue(all(compatible(a, b, criteria) for a in group.files for b in group.files))

    def test_similar_name_survivors_can_regroup_after_cleanup_then_addition(self):
        for name in ("aaa.txt", "aab.txt", "abb.txt"):
            self.file("old/" + name, b"identical")
        old = self.root / "old"
        criteria = SearchCriteria(similar_names=True, text_tolerance=1)
        previous = scanner.scan([old], criteria=criteria)
        removed = old / "aaa.txt"
        self.assertEqual(self.file_groups(previous), {frozenset((removed, old / "aab.txt"))})
        removed.rename(self.root / "recycled-fixture.txt")
        previous = replace(previous, groups=[], index={path: item for path, item in previous.index.items()
            if path != removed}, file_count=2, total_bytes=18)
        self.file("new/zzz.txt", b"identical")
        result = scanner.scan([self.root / "new"], previous=previous, criteria=criteria)
        self.assertEqual(self.file_groups(result), {frozenset((old / "aab.txt", old / "abb.txt"))})

    def test_cancelled_addition_does_not_mutate_the_baseline(self):
        old, new = self.fixture()
        previous = scanner.scan([old])
        before = dict(previous.index)
        cancel = Event()
        def progress(value):
            if value.stage == "Hashing full contents":
                cancel.set()
        result = scanner.scan([new], previous=previous, cancel=cancel, progress=progress)
        self.assertTrue(result.cancelled)
        self.assertEqual(previous.index, before)
        self.assertEqual(len(previous.groups), 1)

    def test_changed_settings_reject_all_cached_modes(self):
        old, new = self.fixture()
        previous = scanner.scan([old])
        with self.assertRaisesRegex(ValueError, "settings changed"):
            scanner.scan([new], previous=previous, criteria=SearchCriteria(filename=True))
        previous_folders = duplicate_folders.scan_duplicate_folders([old])
        with self.assertRaisesRegex(ValueError, "settings changed"):
            duplicate_folders.scan_duplicate_folders([new], False, previous=previous_folders)
        previous_similar = similarity.scan_similar([old])
        with self.assertRaisesRegex(ValueError, "settings changed"):
            similarity.scan_similar([new], previous=previous_similar, preset="Broad")

    def test_recursive_covered_roots_are_not_reopened(self):
        old, new = self.fixture()
        previous = scanner.scan([old])
        with patch("duplicate_cleaner.scanner.os.scandir", side_effect=AssertionError("Already scanned")):
            result = scanner.scan([old], previous=previous)
        self.assertEqual(result.index, previous.index)
        self.assertEqual(result.groups, previous.groups)

    def test_stale_old_match_is_skipped_without_rehashing_original_roots(self):
        old, new = self.fixture()
        previous = scanner.scan([old])
        (old / "copy-a.bin").write_bytes(b"changed")
        result = scanner.scan([new], previous=previous)
        self.assertNotIn(old / "copy-a.bin", result.index)
        self.assertTrue(result.issues)
        self.assertTrue(all(record.path != old / "copy-a.bin" for group in result.groups for record in group.files))

    def test_new_folder_matches_old_unique_folder_without_rehashing_old_files(self):
        self.file("old/unique/first.txt", b"match")
        self.file("old/unmatched/other.txt", b"something different")
        self.file("new/renamed.txt", b"match")
        old, new = self.root / "old", self.root / "new"
        previous = duplicate_folders.scan_duplicate_folders([old])
        before = dict(previous.index)
        actual_digest, actual_scandir = duplicate_folders.full_digest, os.scandir
        def digest(record, reporter):
            self.assertFalse(old in record.path.parents, "Old folder contents were rehashed")
            return actual_digest(record, reporter)
        def discover(path):
            self.assertNotEqual(path, old, "The entire old root was rediscovered")
            return actual_scandir(path)
        with patch("duplicate_cleaner.duplicate_folders.full_digest", side_effect=digest), \
                patch("duplicate_cleaner.duplicate_folders.os.scandir", side_effect=discover):
            result = duplicate_folders.scan_duplicate_folders([new], previous=previous)
        self.assertFalse(result.issues, result.issues)
        self.assertIn({old / "unique", new}, [{folder.path for folder in group.folders} for group in result.groups])
        self.assertEqual(previous.index, before)

    def test_folder_content_classes_remain_distinct_under_hash_collisions(self):
        self.file("old/one/file", b"aaa")
        self.file("old/two/file", b"bbb")
        self.file("new/file", b"bbb")
        old, new = self.root / "old", self.root / "new"
        with patch("duplicate_cleaner.duplicate_folders.full_digest", return_value=b"collision"):
            previous = duplicate_folders.scan_duplicate_folders([old])
            result = duplicate_folders.scan_duplicate_folders([new], previous=previous)
        self.assertIn({old / "two", new}, [{folder.path for folder in group.folders} for group in result.groups])
        self.assertFalse(any(old / "one" in {folder.path for folder in group.folders} for group in result.groups))

    def test_empty_folders_and_nonrecursive_subtrees_remain_available(self):
        self.file("old/nested/file", b"match")
        self.file("new/file", b"match")
        empty = self.root / "empty"
        empty.mkdir()
        old = self.root / "old"
        previous = duplicate_folders.scan_duplicate_folders([old], False)
        result = duplicate_folders.scan_duplicate_folders([self.root / "new", empty], False, previous=previous)
        self.assertTrue(any(folder.path == empty and folder.empty for folder in result.folders))
        self.assertIn({old, self.root / "new"}, [{folder.path for folder in group.folders} for group in result.groups])
        added_child = duplicate_folders.scan_duplicate_folders([old / "nested"], False, previous=result)
        self.assertIn(old / "nested", added_child.index)

    def test_added_images_reuse_all_prior_fingerprints_including_unmatched(self):
        old, new = self.root / "old", self.root / "new"
        old.mkdir()
        new.mkdir()
        image = sample_image()
        self.assertTrue(image.save(str(old / "first.png")))
        self.assertTrue(image.scaled(180, 120).save(str(new / "copy.jpg")))
        previous = similarity.scan_similar([old])
        self.assertFalse(previous.groups)
        self.assertEqual(len(previous.images), 1)
        actual = similarity.read_fingerprint
        def decode(record, cancel):
            self.assertNotEqual(record.path.parent, old, "Old image decoded again")
            return actual(record, cancel)
        with patch("duplicate_cleaner.similarity.read_fingerprint", side_effect=decode):
            result = similarity.scan_similar([new], previous=previous)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(len(result.images), 2)
        self.assertEqual(result.compared_count, 2)

    def test_added_videos_do_not_decode_prior_videos_again(self):
        old, new = self.root / "old", self.root / "new"
        old.mkdir()
        new.mkdir()
        original = make_video(old)
        make_video(new, "copy.mp4", original.path, "scale=160:90")
        previous = similarity.scan_similar([old], media_kind="Videos")
        self.assertEqual(len(previous.videos), 1)
        actual = similarity.read_video_fingerprint
        def decode(record, cancel, progress):
            self.assertNotEqual(record.path.parent, old, "Old video decoded again")
            return actual(record, cancel, progress)
        with patch("duplicate_cleaner.similarity.read_video_fingerprint", side_effect=decode):
            result = similarity.scan_similar([new], media_kind="Videos", previous=previous)
        self.assertEqual(result.videos_compared, 2)
        self.assertEqual(len(result.groups), 1)
