import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from duplicate_cleaner.cleanup import recycle_selected
from duplicate_cleaner.empty_folders import recycle_empty_folders, scan_empty_folders
from duplicate_cleaner.scanner import scan
from duplicate_cleaner.windows_trash import recycle_file
from tests.support import FileTestCase


@unittest.skipUnless(os.name == "nt" and os.environ.get("DUPLICATE_CLEANER_RECYCLE_TEST") == "1",
                     "Set DUPLICATE_CLEANER_RECYCLE_TEST=1 for real Windows Shell tests on disposable files")
class WindowsRecycleTests(FileTestCase):
    def test_real_cleanup_recycles_when_keeper_has_additional_dropbox_stream(self):
        selected = self.file("selected-disposable.jpg")
        kept = self.file("keeper-with-dropbox-metadata.jpg")
        with open(str(kept) + ":com.dropbox.attrs", "wb") as stream:
            stream.write(b"additional metadata")
        result = recycle_selected(scan([self.root]).groups, [selected])
        self.assertFalse(result.issues, result.issues)
        self.assertEqual(result.recycled, [selected])
        self.assertFalse(selected.exists())
        with open(str(kept) + ":com.dropbox.attrs", "rb") as stream:
            self.assertEqual(stream.read(), b"additional metadata")

    def test_real_cleanup_recycles_files_with_named_streams(self):
        for all_selected in (False, True):
            with self.subTest(all_selected=all_selected):
                folder = self.root / str(all_selected)
                paths = [self.file(f"{all_selected}/copy-{index}.txt") for index in range(2)]
                for path in paths:
                    with open(str(path) + ":Zone.Identifier", "wb") as stream:
                        stream.write(b"[ZoneTransfer]\r\nZoneId=3\r\n")
                groups = scan([folder]).groups
                targets = paths if all_selected else paths[:1]
                result = recycle_selected(groups, targets)
                self.assertFalse(result.issues, result.issues)
                self.assertEqual(result.recycled, targets)
                self.assertTrue(all(not path.exists() for path in targets))
                if not all_selected:
                    with open(str(paths[-1]) + ":Zone.Identifier", "rb") as stream:
                        self.assertEqual(stream.read(), b"[ZoneTransfer]\r\nZoneId=3\r\n")

    def test_real_cleanup_recycles_a_disposable_empty_folder(self):
        target = self.root / "DuplicateCleaner-disposable-empty-folder"
        target.mkdir()
        result = recycle_empty_folders(scan_empty_folders([self.root]).folders, [target])
        self.assertFalse(result.issues, result.issues)
        self.assertEqual(result.recycled, [target])
        self.assertFalse(target.exists())

    def test_shell_callback_veto_stops_the_operation(self):
        target = self.file("callback-veto-must-remain.txt")

        def veto():
            raise OSError("Test veto: do not recycle")

        with self.assertRaisesRegex(OSError, "Test veto"):
            recycle_file(target, veto)
        self.assertEqual(target.read_bytes(), b"identical content")

    def test_shell_permanent_delete_branch_is_blocked(self):
        from win32com.shell import shellcon
        target = self.file("permanent-delete-veto-must-remain.txt")
        # Make a real Shell callback fail the recycle-mode test. This must
        # cancel the native operation, not just report an error afterwards.
        with patch.object(shellcon, "TSF_DELETE_RECYCLE_IF_POSSIBLE", 0):
            with self.assertRaisesRegex(OSError, "permanent deletion was blocked"):
                recycle_file(target, lambda: None)
        self.assertEqual(target.read_bytes(), b"identical content")

    def test_real_cleanup_recycles_only_the_selected_disposable_copy(self):
        selected = self.file("DuplicateCleaner-disposable-recycle-test.txt")
        kept = self.file("keeper-must-remain.txt")
        result = recycle_selected(scan([self.root]).groups, [selected])
        self.assertFalse(result.issues, result.issues)
        self.assertEqual(result.recycled, [selected])
        self.assertFalse(selected.exists())
        self.assertEqual(kept.read_bytes(), b"identical content")

    def test_recycling_works_from_a_background_thread(self):
        selected = self.file("DuplicateCleaner-background-recycle-test.txt")
        kept = self.file("background-keeper-must-remain.txt")
        groups = scan([self.root]).groups
        with ThreadPoolExecutor(max_workers=1) as worker:
            result = worker.submit(recycle_selected, groups, [selected]).result(timeout=10)
        self.assertFalse(result.issues, result.issues)
        self.assertEqual(result.recycled, [selected])
        self.assertFalse(selected.exists())
        self.assertTrue(kept.exists())

    def test_real_cleanup_recycles_every_selected_copy_from_a_background_thread(self):
        paths = [self.file(f"DuplicateCleaner-all-copies-disposable-{index}.txt") for index in range(3)]
        groups = scan([self.root]).groups
        with ThreadPoolExecutor(max_workers=1) as worker:
            result = worker.submit(recycle_selected, groups, paths).result(timeout=30)
        self.assertFalse(result.issues, result.issues)
        self.assertEqual(set(result.recycled), set(paths))
        self.assertTrue(all(not path.exists() for path in paths))
