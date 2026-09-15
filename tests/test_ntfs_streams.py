import json
import os
import subprocess
import sys
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from duplicate_cleaner.cleanup import recycle_selected
from duplicate_cleaner.files import capture, open_checked, open_named_streams
from duplicate_cleaner.scanner import CHUNK_SIZE, Reporter, full_digest, hash_named_streams, identical, scan
from duplicate_cleaner.sessions import SessionData, SessionFormatError, load_session, save_session
from tests.support import FileTestCase


@unittest.skipUnless(os.name == "nt", "Windows NTFS streams")
class NtfsStreamTests(FileTestCase):
    def add_stream(self, path, name="Zone.Identifier", data=b"[ZoneTransfer]\r\nZoneId=3\r\n"):
        with open(str(path) + ":" + name, "wb") as stream:
            stream.write(data)

    def copies(self, count=2):
        paths = [self.file(f"copy-{i}.bin") for i in range(count)]
        for path in paths:
            self.add_stream(path)
        return paths

    def test_matching_streams_are_hashed_compared_and_counted(self):
        paths = self.copies()
        for path in paths:
            self.add_stream(path, "empty", b"")
            self.add_stream(path, "large data", b"x" * (CHUNK_SIZE + 19))
        result = scan([self.root])
        self.assertFalse(result.issues, result.issues)
        self.assertEqual(len(result.groups), 1)
        group = result.groups[0]
        total = len(b"identical content") + len(b"[ZoneTransfer]\r\nZoneId=3\r\n") + CHUNK_SIZE + 19
        self.assertEqual(group.extra_bytes, total)
        self.assertEqual(result.total_bytes, total * 2)
        self.assertEqual(len(group.files[0].streams), 3)
        self.assertEqual(group.metadata_status, "Exact match")
        self.assertEqual(full_digest(capture(paths[0]), Reporter(Event(), None)).hex(), group.digest)

    def test_equal_size_stream_content_differences_are_flagged_in_one_group(self):
        paths = self.copies()
        self.add_stream(paths[1], data=b"[ZoneTransfer]\r\nZoneId=4\r\n")
        result = scan([self.root])
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0].metadata_status, "Metadata differs")
        self.assertEqual(result.groups[0].differing_streams, (":Zone.Identifier:$DATA",))
        self.assertFalse(result.issues, result.issues)

    def test_cleanup_rejects_stream_differences_even_if_saved_hashes_match(self):
        paths = self.copies()
        self.add_stream(paths[1], data=b"[ZoneTransfer]\r\nZoneId=4\r\n")
        with patch("duplicate_cleaner.scanner.hash_named_streams",
                   return_value=((":Zone.Identifier:$DATA", "0" * 64),)):
            result = scan([self.root])
        self.assertFalse(result.issues, result.issues)
        self.assertEqual(result.groups[0].metadata_status, "Exact match")
        recycler = Mock()
        cleaned = recycle_selected(result.groups, paths[:1], recycler=recycler)
        self.assertFalse(cleaned.recycled)
        self.assertIn("explicit confirmation", cleaned.issues[0].reason)
        recycler.assert_not_called()

    def test_stream_name_size_and_presence_differences_are_reported(self):
        for index, (left, right) in enumerate((
                (("empty", b""), None),
                (("one", b"abc"), ("two", b"abc")),
                (("one", b"abc"), ("one", b"abcd")))):
            with self.subTest(left=left, right=right):
                first, second = self.file(f"{index}/first"), self.file(f"{index}/second")
                self.add_stream(first, *left)
                if right is not None:
                    self.add_stream(second, *right)
                self.assertTrue(identical(capture(first), capture(second), Reporter(Event(), None)))
                result = scan([first.parent])
                self.assertEqual(len(result.groups), 1)
                self.assertEqual(result.groups[0].metadata_status, "Metadata differs")
                recycler = Mock()
                cleaned = recycle_selected(result.groups, [first], allow_zone_differences=True, recycler=recycler)
                self.assertFalse(cleaned.recycled)
                self.assertIn("Extra NTFS data differs", cleaned.issues[0].reason)
                recycler.assert_not_called()

    def test_empty_main_content_with_matching_streams_is_verified(self):
        for name in ("first", "second"):
            self.add_stream(self.file(name, b""), "extra", b"stream-only content")
        result = scan([self.root])
        self.assertFalse(result.issues, result.issues)
        self.assertEqual(result.groups[0].extra_bytes, len(b"stream-only content"))

    def test_stream_enumeration_error_fails_closed(self):
        import pywintypes
        self.copies()
        with patch("win32file.FindStreams", side_effect=pywintypes.error(5, "FindStreams", "Access denied")):
            result = scan([self.root])
        self.assertFalse(result.groups)
        self.assertEqual(len(result.issues), 2)

    def test_stream_read_error_fails_closed_and_releases_open_handles(self):
        from duplicate_cleaner import files
        paths = self.copies()
        original = files._open_read

        def deny_named(path, allow_delete):
            if str(path).endswith(":Zone.Identifier:$DATA"):
                raise OSError("Stream cannot be read")
            return original(path, allow_delete)

        with patch.object(files, "_open_read", side_effect=deny_named):
            result = scan([self.root])
        self.assertFalse(result.groups)
        self.assertEqual(len(result.issues), 2)
        for path in paths:
            path.write_bytes(b"handles were closed")

    def test_existing_streams_are_locked_against_writes(self):
        path = self.copies()[0]
        record = capture(path)
        with open_checked(record), open_named_streams(record):
            with self.assertRaises(OSError):
                self.add_stream(path, data=b"must not overwrite")
        self.add_stream(path, data=b"lock released")

    def test_cancel_during_named_stream_hashing_releases_locks(self):
        paths = self.copies()
        for path in paths:
            self.add_stream(path, "large", b"x" * (CHUNK_SIZE + 1))
        cancel = Event()
        read = Reporter.read

        def cancel_on_large_stream(reporter, stream, size):
            data = read(reporter, stream, size)
            if reporter.stage == "Hashing full contents" and len(data) == CHUNK_SIZE:
                cancel.set()
            return data

        with patch.object(Reporter, "read", cancel_on_large_stream):
            result = scan([self.root], cancel=cancel)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.groups)
        for path in paths:
            self.add_stream(path, "large", b"handles released")

    def test_preview_helpers_accept_named_stream_records(self):
        from PySide6.QtGui import QImage
        path = self.root / "image.png"
        picture = QImage(8, 8, QImage.Format.Format_RGB32)
        picture.fill(0xff123456)
        self.assertTrue(picture.save(str(path)))
        self.add_stream(path)
        record = capture(path)
        record = replace(record, stream_hashes=hash_named_streams(record, Reporter(Event(), None)))
        request = json.dumps(asdict(record), default=str).encode("utf-8")
        commands = [[sys.executable, str(Path(__file__).resolve().parents[1] / "run.py")]]
        if os.environ.get("DUPLICATE_CLEANER_EXE"):
            commands.append([str(Path(os.environ["DUPLICATE_CLEANER_EXE"]).resolve())])
        for command in commands:
            for mode in ("--render-preview", "--render-thumbnail"):
                with self.subTest(command=command, mode=mode):
                    result = subprocess.run(command + [mode], input=request, capture_output=True,
                                            timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = result.stdout
                    if mode == "--render-preview":
                        header, payload = payload.split(b"\n", 1)
                        self.assertEqual(json.loads(header).get("kind"), "image", header)
                    rendered = QImage.fromData(payload)
                    self.assertFalse(rendered.isNull())
                    self.assertEqual(rendered.pixelColor(0, 0).name(), "#123456")

    def test_cleanup_preserves_streams_and_holds_locks_until_recycling(self):
        paths = self.copies(3)
        groups = scan([self.root]).groups
        moved = []

        def recycler(path, revalidate):
            for locked in (path, paths[-1]):
                with self.assertRaises(OSError):
                    self.add_stream(locked, data=b"must not overwrite")
            revalidate()
            destination = path.with_suffix(".recycled-fixture")
            path.rename(destination)
            moved.append(destination)

        result = recycle_selected(groups, paths, recycler=recycler)
        self.assertEqual(result.recycled, paths)
        self.assertFalse(result.issues, result.issues)
        for path in moved:
            with open(str(path) + ":Zone.Identifier", "rb") as stream:
                self.assertEqual(stream.read(), b"[ZoneTransfer]\r\nZoneId=3\r\n")

    def test_cleanup_rechecks_stream_bytes_even_with_restored_timestamps(self):
        paths = self.copies()
        groups = scan([self.root]).groups
        original = paths[0].stat()
        self.add_stream(paths[0], data=b"[ZoneTransfer]\r\nZoneId=4\r\n")
        os.utime(paths[0], ns=(original.st_atime_ns, original.st_mtime_ns))
        # Fresh metadata isolates byte verification from timestamp detection.
        groups = [replace(groups[0], files=tuple(capture(path) for path in paths))]
        recycler = Mock()
        result = recycle_selected(groups, [paths[0]], recycler=recycler)
        self.assertFalse(result.recycled)
        self.assertIn("Download metadata", result.issues[0].reason)
        recycler.assert_not_called()

    def test_stream_removed_after_scan_blocks_cleanup(self):
        paths = self.copies()
        groups = scan([self.root]).groups
        os.remove(str(paths[0]) + ":Zone.Identifier")
        recycler = Mock()
        result = recycle_selected(groups, [paths[0]], recycler=recycler)
        self.assertFalse(result.recycled)
        self.assertTrue(result.issues)
        recycler.assert_not_called()

    def test_zone_differences_require_approval_and_leave_keeper_untouched(self):
        cases = ((None, b"zone data"), (b"zone data", None), (b"zone 3", b"zone 4"))
        for index, (selected_zone, kept_zone) in enumerate(cases):
            with self.subTest(index=index):
                selected = self.file(f"case-{index}/selected")
                kept = self.file(f"case-{index}/kept")
                for path, zone in ((selected, selected_zone), (kept, kept_zone)):
                    if zone is not None:
                        self.add_stream(path, data=zone)
                    self.add_stream(path, "other", b"same extra data")
                result = scan([selected.parent])
                self.assertEqual(result.groups[0].metadata_status, "Metadata differs")
                denied = Mock()
                without_override = recycle_selected(result.groups, [selected], recycler=denied)
                if selected_zone is None:
                    self.assertEqual(without_override.recycled, [selected])
                    self.assertFalse(without_override.issues)
                    denied.assert_called_once()
                else:
                    self.assertFalse(without_override.recycled)
                    denied.assert_not_called()
                original = capture(kept)

                def recycler(path, revalidate):
                    revalidate()
                    path.rename(path.with_name("recycled-fixture"))

                cleaned = recycle_selected(result.groups, [selected], allow_zone_differences=True, recycler=recycler)
                self.assertFalse(cleaned.issues, cleaned.issues)
                self.assertEqual(cleaned.recycled, [selected])
                self.assertEqual(capture(kept), original)
                if kept_zone is not None:
                    with open(str(kept) + ":Zone.Identifier", "rb") as stream:
                        self.assertEqual(stream.read(), kept_zone)
                with open(str(kept) + ":other", "rb") as stream:
                    self.assertEqual(stream.read(), b"same extra data")

    def test_batch_recycles_copies_when_kept_file_has_extra_dropbox_data(self):
        paths = [self.file(f"copy-{i}.jpg") for i in range(4)]
        kept = paths[-1]
        for path in paths[1:]:
            self.add_stream(path, "shared", b"shared data")
        self.add_stream(kept, "com.dropbox.attrs", b"keeper-only dropbox metadata")
        self.add_stream(kept, "empty", b"")
        groups = scan([self.root]).groups
        before = capture(kept)

        def recycler(path, revalidate):
            with self.assertRaises(OSError):
                self.add_stream(kept, "com.dropbox.attrs", b"must remain locked")
            with self.assertRaises(OSError):
                kept.unlink()
            revalidate()
            path.rename(path.with_suffix(".recycled-fixture"))

        result = recycle_selected(groups, paths[:-1], recycler=recycler)
        self.assertEqual(result.recycled, paths[:-1])
        self.assertFalse(result.issues, result.issues)
        self.assertEqual(capture(kept), before)
        with open(str(kept) + ":com.dropbox.attrs", "rb") as stream:
            self.assertEqual(stream.read(), b"keeper-only dropbox metadata")
        self.assertEqual(kept.read_bytes(), b"identical content")

    def test_selected_dropbox_metadata_requires_approval_and_is_preserved_when_recycled(self):
        for index, kept_metadata in enumerate((None, b"different metadata")):
            with self.subTest(kept_metadata=kept_metadata):
                selected = self.file(f"{index}/selected.jpg")
                kept = self.file(f"{index}/kept.jpg")
                self.add_stream(selected, "com.dropbox.attrs", b"selected metadata")
                if kept_metadata is not None:
                    self.add_stream(kept, "com.dropbox.attrs", kept_metadata)
                groups = scan([selected.parent]).groups
                denied = Mock()
                result = recycle_selected(groups, [selected], allow_zone_differences=True, recycler=denied)
                self.assertFalse(result.recycled)
                self.assertIn("Dropbox metadata", result.issues[0].reason)
                denied.assert_not_called()
                before = capture(kept)
                destination = selected.with_suffix(".recycled-fixture")

                def recycler(path, revalidate):
                    with self.assertRaises(OSError):
                        self.add_stream(path, "com.dropbox.attrs", b"must stay locked")
                    revalidate()
                    path.rename(destination)

                result = recycle_selected(groups, [selected], allow_dropbox_differences=True, recycler=recycler)
                self.assertEqual(result.recycled, [selected])
                self.assertFalse(result.issues, result.issues)
                self.assertEqual(capture(kept), before)
                with open(str(destination) + ":com.dropbox.attrs", "rb") as stream:
                    self.assertEqual(stream.read(), b"selected metadata")

    def test_dropbox_approval_does_not_allow_zone_or_other_stream_differences(self):
        for index, stream_name in enumerate(("Zone.Identifier", "custom", "com.dropbox.attributes")):
            with self.subTest(stream_name=stream_name):
                selected = self.file(f"{index}/selected")
                kept = self.file(f"{index}/kept")
                self.add_stream(selected, "com.dropbox.attrs", b"Dropbox metadata")
                self.add_stream(selected, stream_name, b"other unique data")
                groups = scan([selected.parent]).groups
                recycler = Mock()
                result = recycle_selected(groups, [selected], allow_dropbox_differences=True,
                                          allow_zone_differences=stream_name != "Zone.Identifier", recycler=recycler)
                self.assertFalse(result.recycled)
                self.assertIn(stream_name, result.issues[0].reason)
                recycler.assert_not_called()

    def test_dropbox_approval_does_not_allow_changed_main_contents(self):
        paths = self.copies()
        self.add_stream(paths[0], "com.dropbox.attrs", b"extra metadata")
        group = scan([self.root]).groups[0]
        paths[0].write_bytes(b"different content")
        group = replace(group, files=tuple(capture(path) for path in paths))
        recycler = Mock()
        result = recycle_selected([group], paths[:1], allow_dropbox_differences=True, recycler=recycler)
        self.assertFalse(result.recycled)
        self.assertIn("Contents no longer match", result.issues[0].reason)
        recycler.assert_not_called()

    def test_all_selected_dropbox_differences_require_approval_before_last_copy(self):
        paths = [self.file(f"copy-{index}") for index in range(3)]
        for index, path in enumerate(paths):
            self.add_stream(path, "com.dropbox.attrs", bytes([index]))
        groups = scan([self.root]).groups
        denied = Mock()
        self.assertFalse(recycle_selected(groups, paths, recycler=denied).recycled)
        denied.assert_not_called()

        def recycler(path, revalidate):
            revalidate()
            path.rename(path.with_suffix(".recycled-fixture"))

        result = recycle_selected(groups, paths, allow_dropbox_differences=True, recycler=recycler)
        self.assertEqual(result.recycled, paths)
        self.assertFalse(result.issues, result.issues)

    def test_extra_keeper_stream_does_not_allow_losing_selected_stream_data(self):
        for index, (selected_data, kept_data) in enumerate(((b"unique", None), (b"unique", b"others"))):
            with self.subTest(kept_data=kept_data):
                selected = self.file(f"{index}/selected")
                kept = self.file(f"{index}/kept")
                self.add_stream(selected, "custom", selected_data)
                if kept_data is not None:
                    self.add_stream(kept, "custom", kept_data)
                self.add_stream(kept, "com.dropbox.attrs", b"additional data")
                recycler = Mock()
                result = recycle_selected(scan([selected.parent]).groups, [selected],
                                          allow_zone_differences=True, recycler=recycler)
                self.assertFalse(result.recycled)
                self.assertIn(":custom:$DATA", result.issues[0].reason)
                recycler.assert_not_called()

    def test_extra_streams_are_not_ignored_when_every_copy_is_selected(self):
        for index in (0, 1):
            with self.subTest(stream_on=index):
                paths = [self.file(f"{index}/copy-{number}") for number in range(2)]
                self.add_stream(paths[index], "com.dropbox.attrs", b"extra data")
                recycler = Mock()
                result = recycle_selected(scan([paths[0].parent]).groups, paths,
                                          allow_zone_differences=True, recycler=recycler)
                self.assertFalse(result.recycled)
                self.assertEqual(len(result.issues), 2)
                recycler.assert_not_called()

    def test_changed_keeper_inventory_still_blocks_before_recycling(self):
        selected, kept = self.file("selected"), self.file("kept")
        self.add_stream(kept, "com.dropbox.attrs", b"extra data")
        groups = scan([self.root]).groups

        def recycler(path, revalidate):
            self.add_stream(kept, "new-stream", b"changed after comparison")
            revalidate()
            self.fail("Recycling must stop when the keeper changes")

        result = recycle_selected(groups, [selected], recycler=recycler)
        self.assertFalse(result.recycled)
        self.assertIn("File changed", result.issues[0].reason)
        self.assertTrue(selected.exists())

    def test_all_selected_zone_differences_need_approval_including_last_copy(self):
        paths = self.copies(3)
        self.add_stream(paths[0], data=b"zone 1")
        self.add_stream(paths[1], data=b"zone 2")
        groups = scan([self.root]).groups
        denied = Mock()
        result = recycle_selected(groups, paths, recycler=denied)
        self.assertFalse(result.recycled)
        self.assertEqual(len(result.issues), 3)
        denied.assert_not_called()

        def recycler(path, revalidate):
            revalidate()
            path.rename(path.with_suffix(".recycled-fixture"))

        result = recycle_selected(groups, paths, allow_zone_differences=True, recycler=recycler)
        self.assertEqual(result.recycled, paths)
        self.assertFalse(result.issues, result.issues)

    def test_zone_approval_does_not_allow_other_stream_content_differences(self):
        paths = self.copies()
        for index, path in enumerate(paths):
            self.add_stream(path, "custom", bytes([index]))
        self.add_stream(paths[0], data=b"different download marker")
        result = scan([self.root])
        recycler = Mock()
        cleaned = recycle_selected(result.groups, paths, allow_zone_differences=True, recycler=recycler)
        self.assertFalse(cleaned.recycled)
        self.assertEqual(len(cleaned.issues), 2)
        self.assertIn(":custom:$DATA", cleaned.issues[0].reason)
        recycler.assert_not_called()

    def test_zone_approval_does_not_allow_different_main_contents(self):
        paths = self.copies()
        group = scan([self.root]).groups[0]
        paths[0].write_bytes(b"different content")
        group = replace(group, files=tuple(capture(path) for path in paths))
        recycler = Mock()
        result = recycle_selected([group], paths[:1], allow_zone_differences=True, recycler=recycler)
        self.assertFalse(result.recycled)
        self.assertIn("Contents no longer match", result.issues[0].reason)
        recycler.assert_not_called()

    def test_savings_use_actual_stream_sizes_and_assume_largest_copy_is_kept(self):
        paths = self.copies()
        self.add_stream(paths[0], data=b"short")
        group = scan([self.root]).groups[0]
        self.assertEqual(group.extra_bytes, len(b"identical content") + len(b"short"))
        self.assertEqual(replace(group, files=tuple(reversed(group.files))).extra_bytes, group.extra_bytes)

    def test_old_stream_session_is_marked_unchecked_and_new_mixed_session_preserves_badge(self):
        paths = self.copies()
        self.add_stream(paths[0], data=b"different download marker")
        groups = scan([self.root]).groups
        path = self.root / "saved.dupsession"
        data = SessionData(tuple(groups), frozenset(paths[:1]), (), True, (), 2, 0, "All")
        save_session(path, data)
        self.assertEqual(load_session(path).data.groups[0].metadata_status, "Metadata differs")
        payload = json.loads(path.read_text())
        for record in payload["groups"][0]["files"]:
            del record["stream_hashes"]
        path.write_text(json.dumps(payload))
        loaded = load_session(path)
        self.assertFalse(loaded.validation_issues)
        self.assertIn("Metadata not checked", loaded.data.groups[0].metadata_status)
        for invalid in ([[":Zone.Identifier:$DATA", "bad"]], [[":other:$DATA", "0" * 64]],
                        [[":Zone.Identifier:$DATA", "0" * 64]] * 2):
            with self.subTest(invalid=invalid):
                payload["groups"][0]["files"][0]["stream_hashes"] = invalid
                path.write_text(json.dumps(payload))
                with self.assertRaises(SessionFormatError):
                    load_session(path)

    def test_session_round_trip_and_changed_stream_layout(self):
        paths = self.copies(3)
        result = scan([self.root])
        data = SessionData(tuple(result.groups), frozenset(paths[:1]), (str(self.root),), True,
                           (), result.file_count, result.total_bytes, "All")
        path = self.root / "saved.dupsession"
        save_session(path, data)
        loaded = load_session(path)
        self.assertEqual(loaded.data.groups, data.groups)
        self.assertEqual(loaded.data.groups[0].files[0].stream_hashes, data.groups[0].files[0].stream_hashes)
        self.assertEqual(loaded.data.selected, data.selected)
        self.add_stream(paths[0], "new", b"unique bytes")
        loaded = load_session(path)
        self.assertEqual(loaded.dropped_files, 1)
        self.assertFalse(loaded.data.selected)

    def test_older_session_without_streams_still_loads(self):
        self.file("a")
        self.file("b")
        result = scan([self.root])
        data = SessionData(tuple(result.groups), frozenset(), (), True, (), 2, result.total_bytes, "All")
        path = self.root / "saved.dupsession"
        save_session(path, data)
        payload = json.loads(path.read_text())
        for record in payload["groups"][0]["files"]:
            del record["streams"]
        path.write_text(json.dumps(payload))
        self.assertEqual(load_session(path).data.groups, data.groups)
        for invalid in ([["../escape", 1]], [[":x:$DATA", -1]], [[":x:$DATA", True]],
                        [[":x:$DATA", 0], [":x:$DATA", 1]], [["::$DATA", 1]], "bad"):
            with self.subTest(invalid=invalid):
                payload["groups"][0]["files"][0]["streams"] = invalid
                path.write_text(json.dumps(payload))
                with self.assertRaises(SessionFormatError):
                    load_session(path)
