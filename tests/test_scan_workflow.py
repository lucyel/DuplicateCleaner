from threading import Event
from unittest.mock import patch

from duplicate_cleaner.empty_folders import EmptyFolderScanResult
from duplicate_cleaner.file_types import accepts_file
from duplicate_cleaner.models import Progress, ScanResult, SearchCriteria
from duplicate_cleaner.scan_workflow import SCAN_MODES, run_selected_scans
from duplicate_cleaner.scanner import scan
from duplicate_cleaner.similarity import SimilarResult, scan_similar
from tests.support import FileTestCase
from tests.test_similarity import sample_image


class ScanWorkflowTests(FileTestCase):
    def test_only_selected_modes_receive_the_same_scope(self):
        for chosen in (("duplicates",), ("similarity",), ("empty",), tuple(SCAN_MODES)):
            with self.subTest(modes=chosen), \
                    patch("duplicate_cleaner.scan_workflow.scan", return_value=ScanResult()) as duplicates, \
                    patch("duplicate_cleaner.scan_workflow.scan_similar", return_value=SimilarResult()) as similar, \
                    patch("duplicate_cleaner.scan_workflow.scan_empty_folders", return_value=EmptyFolderScanResult()) as empty:
                result = run_selected_scans([self.root], False, modes=chosen,
                    excluded_folders=[self.root / "exclude"], file_types=("Images", "Videos"), preset="Strict")
                self.assertEqual(set(result.results), set(chosen))
                for mode, scanner in zip(SCAN_MODES, (duplicates, similar, empty)):
                    self.assertEqual(scanner.call_count, int(mode in chosen))
                    if mode in chosen:
                        self.assertEqual(scanner.call_args.args, ((self.root,), False))
                        self.assertEqual(scanner.call_args.kwargs["excluded_folders"], (self.root / "exclude",))
                        if mode != "empty":
                            self.assertEqual(scanner.call_args.kwargs["file_types"], frozenset(("Images", "Videos")))
                        else:
                            self.assertNotIn("file_types", scanner.call_args.kwargs)

    def test_cancellation_keeps_completed_results_and_does_not_start_later_modes(self):
        cancel = Event()
        completed = ScanResult()
        def stop(*args, **kwargs):
            cancel.set()
            return SimilarResult(cancelled=True)
        with patch("duplicate_cleaner.scan_workflow.scan", return_value=completed), \
                patch("duplicate_cleaner.scan_workflow.scan_similar", side_effect=stop), \
                patch("duplicate_cleaner.scan_workflow.scan_empty_folders") as empty:
            result = run_selected_scans([self.root], modes=tuple(SCAN_MODES), cancel=cancel)
        self.assertTrue(result.cancelled)
        self.assertIs(result.results["duplicates"], completed)
        self.assertTrue(result.results["similarity"].cancelled)
        empty.assert_not_called()

    def test_failure_is_reported_and_other_selected_modes_continue(self):
        with patch("duplicate_cleaner.scan_workflow.scan", side_effect=OSError("unavailable")), \
                patch("duplicate_cleaner.scan_workflow.scan_empty_folders", return_value=EmptyFolderScanResult()):
            result = run_selected_scans([self.root], modes=("duplicates", "empty"))
        self.assertIn("unavailable", result.errors["duplicates"])
        self.assertIn("empty", result.results)
        self.assertFalse(result.cancelled)

    def test_invalid_options_do_not_start_scanners(self):
        for options in ({"modes": ()}, {"modes": ("bad",)}, {"file_types": ()}, {"file_types": ("bad",)}):
            with patch("duplicate_cleaner.scan_workflow.scan") as scanner:
                with self.assertRaises(ValueError):
                    run_selected_scans([self.root], **options)
                scanner.assert_not_called()
        # Folder scanning needs neither file types nor duplicate comparison criteria.
        result = run_selected_scans([self.root], modes=("empty",), file_types=(),
                                   criteria=SearchCriteria(contents=False, hashes=False, size=False))
        self.assertFalse(result.errors)

    def test_discovery_filters_types_before_capture_and_hashing(self):
        for extension, content in (("PNG", b"image"), ("mp4", b"video"), ("txt", b"document"), ("zzz", b"other")):
            for name in ("a", "b"):
                self.file(name + "." + extension, content)
        from duplicate_cleaner.files import capture
        for selected, expected in ((None, 8), (("Images",), 2), (("Images", "Videos"), 4), (("Other",), 2)):
            with patch("duplicate_cleaner.scanner.capture", wraps=capture) as captured:
                result = scan([self.root], file_types=selected)
            self.assertEqual(result.file_count, expected)
            self.assertEqual(captured.call_count, expected)
            self.assertTrue(all(accepts_file(call.args[0], selected) for call in captured.call_args_list))
        self.assertFalse(scan([self.root], file_types=()).groups)

    def test_similarity_obeys_common_types_and_exclusions(self):
        image = sample_image()
        for name in ("one.png", "two.PNG"):
            self.assertTrue(image.save(str(self.root / name)))
        self.file("bad.mp4", b"not a video")
        with patch("duplicate_cleaner.similarity.read_video_fingerprint") as video:
            result = scan_similar([self.root], media_kind="All", file_types=("Images",))
        video.assert_not_called()
        self.assertEqual(len(result.groups), 1)
        self.assertEqual((result.image_count, result.video_count), (2, 0))
        self.assertFalse(scan_similar([self.root], media_kind="All", file_types=("Documents",)).groups)

    def test_file_filters_never_make_a_nonempty_folder_empty(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.file("occupied/ignored.txt", b"must remain visible to emptiness checks")
        result = run_selected_scans([self.root], modes=("empty",), file_types=("Images",))
        self.assertEqual([folder.path for folder in result.results["empty"].folders], [empty])

    def test_progress_identifies_mode(self):
        reports = []
        def scanner(*args, progress, **kwargs):
            progress(Progress("Discovering files", 2, 3, "example"))
            return ScanResult()
        with patch("duplicate_cleaner.scan_workflow.scan", side_effect=scanner):
            run_selected_scans([self.root], progress=reports.append)
        self.assertEqual(reports[0].stage, "Duplicate files (1/1) · Discovering files")
        self.assertEqual(reports[0].path, "example")
