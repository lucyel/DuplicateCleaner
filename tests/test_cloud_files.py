import os
from contextlib import contextmanager
from pathlib import Path
from unittest import skipUnless
from unittest.mock import Mock, patch

from duplicate_cleaner import files
from duplicate_cleaner.cleanup import recycle_selected
from duplicate_cleaner.empty_folders import capture_empty_folder
from duplicate_cleaner.preview import render_preview
from duplicate_cleaner.scanner import scan
from duplicate_cleaner.sessions import SessionData, load_session, save_session
from duplicate_cleaner.similarity import fingerprint_image, scan_similar
from tests.support import FileTestCase
from tests.test_similarity import sample_image


CLOUD = 0x9000201A
LOCAL = 0x420


class CloudFileTests(FileTestCase):
    @contextmanager
    def cloud_state(self, path, attributes=LOCAL, tag=CLOUD):
        original = files._path_attributes
        with patch.object(files, "_path_attributes", side_effect=lambda current:
                          (attributes, tag) if current == path else original(current)):
            yield

    def test_all_known_cloud_tags_allow_local_files_only(self):
        for variant in range(16):
            tag = 0x9000001A | variant << 12
            files.check_attributes(self.root, LOCAL, tag, allow_cloud=True)
            with self.assertRaisesRegex(files.UnsafeFile, "empty-folder"):
                files.check_attributes(self.root, LOCAL, tag)

    def test_links_and_unknown_reparse_tags_remain_blocked(self):
        for tag in (0xA000000C, 0xA0000003, 0, 0x80000018, 0x9001001A):
            with self.subTest(tag=tag), self.assertRaises(files.UnsafeFile):
                files.check_attributes(self.root, LOCAL, tag, allow_cloud=True)

    def test_offline_partial_and_recall_files_are_blocked_even_when_pinned(self):
        for missing in (0x1000, 0x40000, 0x400000):
            for pinned in (0, 0x80000):
                for attributes, tag in ((LOCAL, CLOUD), (0x20, 0)):
                    with self.subTest(missing=missing, pinned=pinned, tag=tag):
                        with self.assertRaisesRegex(files.UnsafeFile, "not fully available locally"):
                            files.check_attributes(self.root, attributes | missing | pinned,
                                                   tag, allow_cloud=True)

    @skipUnless(os.name == "nt", "Windows directory enumeration")
    def test_native_enumeration_detects_cloud_state_hidden_by_stat(self):
        import win32file
        path = self.file("cloud.txt")
        self.assertFalse(path.lstat().st_file_attributes & 0x400)
        original = win32file.FindFilesW
        state = LOCAL

        def enumerate_path(current):
            entries = original(current)
            if Path(current) == path:
                entry = list(entries[0])
                entry[0], entry[6] = state, CLOUD
                return [tuple(entry)]
            return entries

        with patch.object(win32file, "FindFilesW", side_effect=enumerate_path):
            self.assertEqual(files.capture(path).path, path)
            state |= 0x400000
            with self.assertRaisesRegex(files.UnsafeFile, "not fully available locally"):
                files.capture(path)

    @contextmanager
    def discovery_metadata(self, path, attributes):
        original = os.scandir

        class Entry:
            def __init__(self, entry):
                self.entry = entry

            def __getattr__(self, name):
                return getattr(self.entry, name)

            def stat(self, **kwargs):
                info = self.entry.stat(**kwargs)

                class Info:
                    st_file_attributes = attributes
                    st_reparse_tag = CLOUD

                    def __getattr__(self, name):
                        return getattr(info, name)

                return Info()

        @contextmanager
        def entries(folder):
            with original(folder) as found:
                yield (Entry(entry) if Path(entry.path) == path else entry for entry in found)

        with patch("os.scandir", side_effect=entries):
            yield

    def test_duplicate_discovery_accepts_local_cloud_entry(self):
        first = self.file("cloud.txt")
        second = self.file("ordinary.txt")
        with self.discovery_metadata(first, LOCAL), self.cloud_state(first):
            result = scan([self.root])
        self.assertFalse(result.issues)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual({record.path for record in result.groups[0].files}, {first, second})

    def test_both_scanners_skip_partial_entry_before_opening_contents(self):
        path = self.file("cloud.png")
        for scanner in (scan, scan_similar):
            with self.subTest(scanner=scanner.__name__):
                with self.discovery_metadata(path, LOCAL | 0x400000):
                    with patch.object(files, "_open_read") as opener:
                        result = scanner([self.root])
                opener.assert_not_called()
                self.assertFalse(result.groups)
                self.assertEqual([issue.path for issue in result.issues], [path])
                self.assertIn("not fully available locally", result.issues[0].reason)

    def test_similarity_discovery_accepts_local_cloud_entry(self):
        path = self.root / "cloud.png"
        self.assertTrue(sample_image().save(str(path)))
        self.file("copy.png", path.read_bytes())
        with self.discovery_metadata(path, LOCAL), self.cloud_state(path):
            # Keep decoding in-process so the simulated metadata applies there too.
            with patch("duplicate_cleaner.similarity.read_fingerprint",
                       side_effect=lambda record, cancel: fingerprint_image(record)):
                result = scan_similar([self.root])
        self.assertFalse(result.issues)
        self.assertEqual(result.compared_count, 2)
        self.assertEqual(len(result.groups), 1)

    def test_open_rechecks_cloud_state_after_discovery(self):
        path = self.file("cloud.txt")
        with self.cloud_state(path):
            record = files.capture(path)
            with files.open_checked(record) as stream:
                self.assertEqual(stream.read(), b"identical content")
        with self.cloud_state(path, LOCAL | 0x1000), patch.object(files, "_open_read") as opener:
            with self.assertRaisesRegex(files.UnsafeFile, "not fully available locally"):
                with files.open_checked(record):
                    self.fail("Unavailable cloud file was opened")
            opener.assert_not_called()

    def test_preview_accepts_local_cloud_and_rejects_later_dehydration(self):
        path = self.root / "cloud.png"
        self.assertTrue(sample_image().save(str(path)))
        with self.cloud_state(path):
            record = files.capture(path)
            metadata, image = render_preview(record)
            self.assertEqual(metadata["kind"], "image")
            self.assertTrue(image.startswith(b"\x89PNG"))
        with self.cloud_state(path, LOCAL | 0x400000):
            with self.assertRaisesRegex(files.UnsafeFile, "not fully available locally"):
                render_preview(record)

    def test_session_revalidates_cloud_availability(self):
        path = self.file("cloud.txt")
        self.file("copy.txt")
        session = self.root / "saved.dupsession"
        with self.cloud_state(path):
            result = scan([self.root])
            data = SessionData(tuple(result.groups), frozenset(), (str(self.root),), True,
                               (), result.file_count, result.total_bytes, "All")
            save_session(session, data)
            self.assertEqual(len(load_session(session).data.groups), 1)
        with self.cloud_state(path, LOCAL | 0x400000):
            loaded = load_session(session)
        self.assertEqual(loaded.dropped_files, 1)
        self.assertFalse(loaded.data.groups)
        self.assertIn("not fully available locally", loaded.validation_issues[0].reason)

    def test_recycle_rejects_dehydrated_file_without_calling_recycler(self):
        path = self.file("cloud.txt")
        self.file("copy.txt")
        groups = scan([self.root]).groups
        with self.cloud_state(path, LOCAL | 0x400000):
            recycler = Mock()
            result = recycle_selected(groups, [path], recycler=recycler)
        recycler.assert_not_called()
        self.assertFalse(result.recycled)
        self.assertTrue(result.issues)
        self.assertTrue(path.exists())

    def test_recycle_rechecks_state_in_callback(self):
        path = self.file("cloud.txt")
        self.file("copy.txt")
        groups = scan([self.root]).groups

        def recycler(current, revalidate):
            with self.cloud_state(path, LOCAL | 0x400000):
                revalidate()
            self.fail("Recycle must stop if the file is no longer local")

        with self.cloud_state(path):
            result = recycle_selected(groups, [path], recycler=recycler)
        self.assertFalse(result.recycled)
        self.assertTrue(result.issues)
        self.assertTrue(path.exists())

    def test_local_cloud_directories_allow_file_scans_but_not_empty_cleanup(self):
        self.file("first")
        self.file("second")
        with self.cloud_state(self.root, LOCAL | 0x10):
            self.assertEqual(len(scan([self.root]).groups), 1)
            with self.assertRaisesRegex(files.UnsafeFile, "empty-folder"):
                capture_empty_folder(self.root)

    def test_unsafe_parent_is_rejected_before_querying_child(self):
        path = self.file("first")
        original = files._path_attributes
        queried = []

        def attributes(current):
            queried.append(current)
            return (0x410, 0xA0000003) if current == self.root else original(current)

        with patch.object(files, "_path_attributes", side_effect=attributes):
            with self.assertRaisesRegex(files.UnsafeFile, "junction"):
                files.capture(path)
        self.assertNotIn(path, queried)
