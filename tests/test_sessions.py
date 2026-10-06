import json
import copy
from dataclasses import asdict, replace
from threading import Event
from unittest.mock import patch

from duplicate_cleaner.models import DuplicateGroup, Issue, SearchCriteria
from duplicate_cleaner.scanner import scan
from duplicate_cleaner.duplicate_folders import recycle_duplicate_folders, scan_duplicate_folders
from duplicate_cleaner.similarity import scan_similar
from duplicate_cleaner.video_similarity import preview_frame
from tests.test_similarity import sample_image
from tests.test_video_similarity import make_video
from duplicate_cleaner.sessions import (FORMAT_NAME, FORMAT_VERSION, SessionData,
                                        SessionFormatError, load_session, save_session)
from tests.support import FileTestCase


class SessionTests(FileTestCase):
    def multimode_data(self, *, video=False):
        self.file("folder-a/nested/first.txt", b"folder contents")
        self.file("folder-b/renamed.txt", b"folder contents")
        empty = self.root / "empty"
        empty.mkdir()
        media = self.root / "media"
        media.mkdir()
        image = sample_image()
        self.assertTrue(image.save(str(media / "original.png")))
        self.assertTrue(image.scaled(180, 120).save(str(media / "resized.jpg"), quality=60))
        if video:
            original = make_video(media)
            make_video(media, "copy.mkv", original.path, "scale=160:90,fps=24")
        files = scan([self.root])
        folders = scan_duplicate_folders([self.root / "folder-a", self.root / "folder-b", empty], False)
        similar = scan_similar([media], media_kind="All")
        return replace(self.session_data(files.groups), folder_result=folders, similar_result=similar,
            selected_folders=frozenset([self.root / "folder-a", empty]),
            selected_similar=frozenset([group.matches[0][0].record.path for group in similar.groups]),
            active_workflow="similarity")

    def test_all_modes_round_trip_with_checks_and_video_previews(self):
        data = self.multimode_data(video=True)
        path = self.root / "all.dupsession"
        save_session(path, data)
        with patch("duplicate_cleaner.scanner.full_digest", side_effect=AssertionError("Load hashed contents")), \
                patch("duplicate_cleaner.video_similarity.read_video_fingerprint", side_effect=AssertionError("Load decoded video")):
            result = load_session(path)
        self.assertEqual(result.data.folder_result.groups, data.folder_result.groups)
        self.assertEqual(result.data.similar_result.groups, data.similar_result.groups)
        self.assertEqual(result.data.selected_folders, data.selected_folders)
        self.assertEqual(result.data.selected_similar, data.selected_similar)
        self.assertEqual(result.data.active_workflow, "similarity")
        self.assertEqual(result.saved_selection_count, 4)
        video = result.data.similar_result.groups[1].reference
        self.assertEqual(video.storyboard, data.similar_result.groups[1].reference.storyboard)
        self.assertFalse(preview_frame(video, 0).isNull())
        self.assertFalse(result.validation_issues)

    def test_folder_only_and_similar_only_sessions(self):
        data = self.multimode_data()
        for mode in ("folders", "similarity"):
            with self.subTest(mode=mode):
                only = replace(data, groups=(), selected=frozenset(), issues=(), file_count=0, total_bytes=0,
                    duplicate_available=False, active_workflow=mode,
                    folder_result=data.folder_result if mode == "folders" else None,
                    selected_folders=data.selected_folders if mode == "folders" else frozenset(),
                    similar_result=data.similar_result if mode == "similarity" else None,
                    selected_similar=data.selected_similar if mode == "similarity" else frozenset())
                path = self.root / (mode + ".dupsession")
                save_session(path, only)
                loaded = load_session(path).data
                self.assertFalse(loaded.duplicate_available)
                self.assertEqual(loaded.active_workflow, mode)
                self.assertEqual(loaded.folder_result is not None, mode == "folders")
                self.assertEqual(loaded.similar_result is not None, mode == "similarity")

    def test_changed_folder_membership_and_empty_folder_prune_saved_results(self):
        data = self.multimode_data()
        path = self.root / "changed-folders.dupsession"
        save_session(path, data)
        self.file("folder-a/new-child", b"added")
        self.file("empty/no-longer-empty", b"added")
        loaded = load_session(path)
        self.assertFalse(loaded.data.folder_result.groups)
        self.assertFalse(loaded.data.selected_folders)
        self.assertEqual(loaded.dropped_folders, 2)
        self.assertEqual(loaded.dropped_selected, 2)

    def test_changed_similar_reference_drops_group_without_promoting_matches(self):
        data = self.multimode_data()
        path = self.root / "changed-media.dupsession"
        save_session(path, data)
        data.similar_result.groups[0].reference.record.path.write_bytes(b"changed")
        loaded = load_session(path)
        self.assertFalse(loaded.data.similar_result.groups)
        self.assertFalse(loaded.data.selected_similar)
        self.assertEqual(len(loaded.data.similar_result.images), 1)

    def test_loaded_folders_still_recycle_all_copies_with_final_reference_verification(self):
        data = self.multimode_data()
        path = self.root / "cleanup.dupsession"
        save_session(path, data)
        loaded = load_session(path).data.folder_result
        group = next(group for group in loaded.groups if group.folders[0].files)
        selected = [tree.path for tree in group.folders]
        def recycler(path, revalidate):
            revalidate()
            path.rename(self.root / (path.name + "-recycled-fixture"))
        result = recycle_duplicate_folders([group], selected, recycler=recycler)
        self.assertEqual(result.recycled, selected)
        self.assertFalse(result.issues, result.issues)

    def test_invalid_folder_structure_and_digests_are_rejected(self):
        data = self.multimode_data()
        path = self.root / "invalid-folders.dupsession"
        save_session(path, data)
        base = json.loads(path.read_text(encoding="utf-8"))
        for mutate in (lambda tree: tree["files"][0].update(path=str(self.root / "outside.txt")),
                       lambda tree: tree.update(digests=[]),
                       lambda tree: tree["digests"][0].__setitem__(1, "bad digest"),
                       lambda tree: tree["directories"].append(tree["directories"][0]),
                       lambda tree: tree["directories"].pop(0)):
            payload = copy.deepcopy(base)
            tree = next(group[0] for group in payload["folders"]["groups"] if group[0]["files"])
            mutate(tree)
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(SessionFormatError):
                load_session(path)

    def test_invalid_video_previews_and_evidence_are_rejected(self):
        data = self.multimode_data(video=True)
        path = self.root / "invalid-video.dupsession"
        save_session(path, data)
        base = json.loads(path.read_text(encoding="utf-8"))
        for mutate in (lambda group: group["reference"].update(storyboard="not base64"),
                       lambda group: group["reference"].update(storyboard="A" * (128 * 1024 + 1)),
                       lambda group: group["reference"].update(duration=float("nan")),
                       lambda group: group["reference"]["timestamps"].__setitem__(0, float("inf")),
                       lambda group: group["matches"][0]["evidence"]["pairs"][0].__setitem__(0, 24),
                       lambda group: group["matches"][0]["evidence"].update(matched=True)):
            payload = copy.deepcopy(base)
            mutate(next(group for group in payload["similarity"]["groups"] if group["kind"] == "video"))
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(SessionFormatError):
                load_session(path)

    def test_invalid_new_mode_save_does_not_replace_existing_session(self):
        data = self.multimode_data()
        path = self.root / "preserved.dupsession"
        path.write_bytes(b"previous session")
        with self.assertRaises(ValueError):
            save_session(path, replace(data, selected_folders=frozenset([self.root / "unknown"])))
        self.assertEqual(path.read_bytes(), b"previous session")

    def test_cancel_during_folder_validation_returns_no_partial_session(self):
        data = self.multimode_data()
        path = self.root / "cancel-folder-load.dupsession"
        save_session(path, data)
        cancel = Event()
        def validate(tree, reporter):
            cancel.set()
            reporter.check()
        with patch("duplicate_cleaner.sessions.ensure_folder_current", side_effect=validate):
            loaded = load_session(path, cancel=cancel)
        self.assertTrue(loaded.cancelled)
        self.assertIsNone(loaded.data)

    def test_cancel_during_new_mode_save_preserves_existing_session(self):
        data = self.multimode_data()
        path = self.root / "cancel-extra-save.dupsession"
        path.write_bytes(b"previous session")
        cancel = Event()
        from duplicate_cleaner.sessions import _folder_json
        def serialize(*args):
            cancel.set()
            return _folder_json(*args)
        with patch("duplicate_cleaner.sessions._folder_json", side_effect=serialize):
            saved = save_session(path, data, cancel=cancel)
        self.assertTrue(saved.cancelled)
        self.assertEqual(path.read_bytes(), b"previous session")
        self.assertFalse(list(self.root.glob(".cancel-extra-save.dupsession.*.tmp")))

    def test_version_two_session_remains_compatible(self):
        self.file("a"); self.file("b")
        path = self.root / "v2.dupsession"
        save_session(path, self.session_data(scan([self.root]).groups))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["version"] = 2
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = load_session(path).data
        self.assertTrue(loaded.duplicate_available)
        self.assertIsNone(loaded.folder_result)
        self.assertIsNone(loaded.similar_result)

    def test_hash_only_and_byte_only_verification_survive_round_trip(self):
        first = self.file("a", b"aaa")
        self.file("b", b"aaa")
        for criteria in (SearchCriteria(contents=False), SearchCriteria(hashes=False)):
            with self.subTest(criteria=criteria):
                groups = scan([self.root], criteria=criteria).groups
                path = self.root / "mode.dupsession"
                save_session(path, self.session_data(groups, [first]))
                loaded = load_session(path).data.groups[0]
                self.assertEqual(loaded.digest, groups[0].digest)
                self.assertEqual(loaded.contents_verified, criteria.contents)
                self.assertEqual(loaded.metadata_status, groups[0].metadata_status)

    def test_version_one_sessions_still_infer_verification(self):
        self.file("a")
        self.file("b")
        path = self.root / "legacy.dupsession"
        save_session(path, self.session_data(scan([self.root]).groups))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["version"] = 1
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertTrue(load_session(path).data.groups[0].contents_verified)

    def test_nonboolean_verification_flag_is_rejected(self):
        self.file("a")
        self.file("b")
        path = self.root / "invalid.dupsession"
        save_session(path, self.session_data(scan([self.root]).groups))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["groups"][0]["byte_verified"] = "false"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(SessionFormatError, "boolean"):
            load_session(path)

    def test_possible_matches_remain_unverified_after_session_round_trip(self):
        first = self.file("a", b"aaa")
        self.file("b", b"bbb")
        groups = scan([self.root], criteria=SearchCriteria(contents=False, hashes=False)).groups
        path = self.root / "possible.dupsession"
        save_session(path, self.session_data(groups, [first]))
        loaded = load_session(path).data
        self.assertEqual(loaded.selected, frozenset([first]))
        self.assertIsNone(loaded.groups[0].digest)
        self.assertIn("Possible match", loaded.groups[0].metadata_status)

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
        self.assertEqual(payload["version"], FORMAT_VERSION)
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
