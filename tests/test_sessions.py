import json
from dataclasses import asdict, replace
from threading import Event

from duplicate_cleaner.models import DuplicateGroup, Issue
from duplicate_cleaner.scanner import scan
from duplicate_cleaner.sessions import (FORMAT_NAME, FORMAT_VERSION, SessionData,
                                        SessionFormatError, load_session, save_session)
from tests.support import FileTestCase


class SessionTests(FileTestCase):
    def test_exclusions_round_trip_and_older_sessions_default_to_none(self):
        path = self.root / "exclusions.dupsession"
        excluded = (str(self.root / "skip"),)
        save_session(path, replace(self.session_data([]), excluded_folders=excluded))
        self.assertEqual(load_session(path).data.excluded_folders, excluded)
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["excluded_folders"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(load_session(path).data.excluded_folders, ())

    def test_invalid_exclusions_are_rejected(self):
        path = self.root / "invalid-exclusions.dupsession"
        save_session(path, self.session_data([]))
        payload = json.loads(path.read_text(encoding="utf-8"))
        for value in ("not a list", ["relative"], [str(self.root)], [str(self.root.parent)]):
            with self.subTest(value=value):
                payload["excluded_folders"] = value
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(SessionFormatError):
                    load_session(path)

    def session_data(self, groups, selected=()):
        return SessionData(
            tuple(groups), frozenset(selected), (str(self.root),), True,
            (Issue(self.root / "unreadable.txt", "Access denied during scan"),),
            12, 34_567, "Selected",
        )

    def test_round_trip_preserves_results_selection_and_scan_context(self):
        for name in ("one.txt", "two.txt", "three.txt"):
            self.file(name, b"same session contents")
        group = scan([self.root]).groups[0]
        selected = {group.files[0].path, group.files[2].path}
        path = self.root / "saved.dupsession"

        saved = save_session(path, self.session_data([group], selected))
        payload = json.loads(path.read_text(encoding="utf-8"))
        expected_files = []
        for record in group.files:
            fields = asdict(record)
            fields["path"] = str(record.path)
            fields["selected"] = record.path in selected
            fields["streams"] = [list(pair) for pair in record.streams]
            fields["stream_hashes"] = [list(pair) for pair in record.stream_hashes]
            expected_files.append(fields)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["groups"], [{"digest": group.digest, "files": expected_files}])
        loaded = load_session(path)

        self.assertFalse(saved.cancelled)
        self.assertTrue(saved.saved_at)
        self.assertFalse(loaded.cancelled)
        self.assertEqual(loaded.data.groups, (group,))
        self.assertEqual(loaded.data.selected, frozenset(selected))
        self.assertEqual(loaded.data.roots, (str(self.root),))
        self.assertTrue(loaded.data.recursive)
        self.assertEqual(loaded.data.file_count, 12)
        self.assertEqual(loaded.data.total_bytes, 34_567)
        self.assertEqual(loaded.data.active_tab, "Selected")
        self.assertEqual(loaded.saved_selection_count, 2)
        self.assertEqual(loaded.dropped_selected, 0)
        self.assertEqual(loaded.data.issues[0].reason, "Access denied during scan")

    def test_changed_file_is_removed_but_two_unchanged_copies_remain(self):
        for name in ("one.bin", "two.bin", "three.bin"):
            self.file(name, b"same session contents")
        group = scan([self.root]).groups[0]
        changed = group.files[0].path
        path = self.root / "changed.dupsession"
        save_session(path, self.session_data([group], [changed]))
        changed.write_bytes(b"different contents")

        loaded = load_session(path)

        self.assertEqual(loaded.dropped_files, 1)
        self.assertEqual(loaded.dropped_groups, 0)
        self.assertEqual(len(loaded.data.groups), 1)
        self.assertEqual({record.path for record in loaded.data.groups[0].files},
                         {record.path for record in group.files if record.path != changed})
        self.assertFalse(loaded.data.selected)
        self.assertEqual(loaded.dropped_selected, 1)
        self.assertTrue(any(issue.path == changed for issue in loaded.validation_issues))

    def test_group_is_removed_when_fewer_than_two_files_are_current(self):
        for name in ("one.bin", "two.bin"):
            self.file(name, b"same session contents")
        group = scan([self.root]).groups[0]
        path = self.root / "incomplete.dupsession"
        save_session(path, self.session_data([group], [group.files[0].path]))
        group.files[1].path.unlink()

        loaded = load_session(path)

        self.assertFalse(loaded.data.groups)
        self.assertFalse(loaded.data.selected)
        self.assertEqual(loaded.dropped_files, 1)
        self.assertEqual(loaded.dropped_groups, 1)
        self.assertEqual(loaded.dropped_selected, 1)
        self.assertEqual(len(loaded.validation_issues), 2)

    def test_cancelled_save_does_not_replace_an_existing_session(self):
        for name in ("one.bin", "two.bin"):
            self.file(name, b"same session contents")
        group = scan([self.root]).groups[0]
        path = self.root / "existing.dupsession"
        path.write_bytes(b"existing session remains")
        cancel = Event()
        cancel.set()

        result = save_session(path, self.session_data([group]), cancel=cancel)

        self.assertTrue(result.cancelled)
        self.assertEqual(path.read_bytes(), b"existing session remains")
        self.assertEqual(list(self.root.glob(".existing.dupsession.*.tmp")), [])

    def test_cancelled_load_returns_no_partial_session(self):
        for name in ("one.bin", "two.bin"):
            self.file(name, b"same session contents")
        group = scan([self.root]).groups[0]
        path = self.root / "cancelled-load.dupsession"
        save_session(path, self.session_data([group]))
        cancel = Event()
        cancel.set()

        result = load_session(path, cancel=cancel)

        self.assertTrue(result.cancelled)
        self.assertIsNone(result.data)

    def test_invalid_and_unsupported_sessions_are_rejected(self):
        path = self.root / "invalid.dupsession"
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            load_session(path)

        path.write_text(json.dumps({"format": FORMAT_NAME, "version": FORMAT_VERSION + 1}),
                        encoding="utf-8")
        with self.assertRaises(SessionFormatError):
            load_session(path)

    def test_session_rejects_repeated_file_identity(self):
        for name in ("one.bin", "two.bin"):
            self.file(name, b"same session contents")
        group = scan([self.root]).groups[0]
        repeated = DuplicateGroup((group.files[0], replace(group.files[0], path=group.files[1].path)),
                                  group.digest)
        with self.assertRaises(ValueError):
            save_session(self.root / "unsafe.dupsession", self.session_data([repeated]))
