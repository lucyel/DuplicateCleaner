import os
import random
import subprocess
import sys
from pathlib import Path
from dataclasses import replace
from threading import Event, Timer
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from duplicate_cleaner.files import capture
from duplicate_cleaner.models import Cancelled
from duplicate_cleaner.similarity import scan_similar
from duplicate_cleaner.video_similarity import (
    VideoFingerprint, compare_videos, decoder_path, group_videos,
    preview_frame, read_video_fingerprint, run_decoder,
)
from tests.support import FileTestCase


def make_video(root, name="original.mp4", source=None, filters=None, codec="libx264"):
    path = root / name
    arguments = [str(decoder_path("ffmpeg")), "-v", "error", "-nostdin", "-y"]
    if source is None:
        arguments += ["-f", "lavfi", "-i", "testsrc2=duration=6:size=320x180:rate=12"]
    else:
        arguments += ["-i", str(source)]
    if filters:
        arguments += ["-vf", filters]
    arguments += ["-an", "-c:v", codec, "-threads", "1", "-crf", "30", str(path)]
    result = subprocess.run(arguments, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return capture(path)


class VideoSimilarityTests(FileTestCase):
    def test_reencoded_resized_frame_rate_changed_video_matches(self):
        original = make_video(self.root)
        copy = make_video(self.root, "bản sao smaller.mkv", original.path, "scale=160:90,fps=24,eq=brightness=0.025")
        a, b = [read_video_fingerprint(record, Event()) for record in (original, copy)]
        evidence = compare_videos(a, b, "Balanced")
        self.assertIsNotNone(evidence)
        self.assertGreaterEqual(evidence.matched, 10)
        self.assertEqual(len(a.signatures), 24)
        self.assertLessEqual(len(a.storyboard), 96 * 1024)
        self.assertFalse(preview_frame(a, 12).isNull())
        result = scan_similar([self.root], media_kind="Videos")
        self.assertEqual(result.videos_compared, 2, result.issues)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.image_count, 0)

    def test_shared_intro_does_not_match_and_blank_video_is_skipped(self):
        original = make_video(self.root)
        changed = make_video(self.root, "different.mp4", original.path, "hflip=enable='gte(t,0.8)'")
        blank = make_video(self.root, "blank.mp4", original.path, "scale=240:180,drawbox=color=red:t=fill")
        a, b = [read_video_fingerprint(record, Event()) for record in (original, changed)]
        self.assertIsNone(compare_videos(a, b, "Broad"))
        with self.assertRaisesRegex(OSError, "informative"):
            read_video_fingerprint(blank, Event())

    def test_mov_avi_and_webm_decode_and_match_the_mp4(self):
        original = make_video(self.root)
        reference = read_video_fingerprint(original, Event())
        for extension, codec in (("mov", "libx264"), ("avi", "libx264"), ("webm", "libvpx-vp9")):
            with self.subTest(extension=extension):
                copy = make_video(self.root, "converted." + extension, original.path, codec=codec)
                candidate = read_video_fingerprint(copy, Event())
                self.assertIsNotNone(compare_videos(reference, candidate, "Balanced"))

    def test_changed_video_preview_is_rejected(self):
        original = make_video(self.root)
        fingerprint = read_video_fingerprint(original, Event())
        original.path.write_bytes(b"changed after scanning")
        with self.assertRaisesRegex(OSError, "changed"):
            preview_frame(fingerprint, 12)

    def test_changed_corrupt_and_excluded_videos_are_handled(self):
        original = make_video(self.root)
        self.file("broken.mp4", b"not a video")
        hidden = self.root / "excluded"
        hidden.mkdir()
        make_video(hidden)
        result = scan_similar([self.root, hidden], excluded_folders=[hidden], media_kind="All")
        self.assertEqual(result.video_count, 2)
        self.assertEqual(result.videos_compared, 1)
        self.assertEqual(len(result.issues), 1)
        original.path.write_bytes(b"changed")
        with self.assertRaises(OSError):
            read_video_fingerprint(original, Event())

    def test_media_selection_preserves_image_only_behavior(self):
        self.file("broken.mp4", b"bad video")
        self.file("broken.png", b"bad image")
        images = scan_similar([self.root], media_kind="Images")
        videos = scan_similar([self.root], media_kind="Videos")
        self.assertEqual((images.image_count, images.video_count, images.ignored_count), (1, 0, 1))
        self.assertEqual((videos.image_count, videos.video_count, videos.ignored_count), (0, 1, 1))
        with self.assertRaises(ValueError):
            scan_similar([self.root], media_kind="invalid")

    def fingerprints(self):
        record = capture(self.file("a.mp4"))
        randomizer = random.Random(43)
        values = tuple(value for i in range(12) for value in [randomizer.getrandbits(63)] * 2)
        a = VideoFingerprint(record, 320, 180, 100, tuple(i / 4 for i in range(24)), values, b"")
        b = replace(a, record=replace(record, path=self.root / "b.mp4"), signatures=tuple(v ^ 31 for v in values))
        c = replace(a, record=replace(record, path=self.root / "c.mp4"), signatures=tuple(v ^ 1023 for v in values))
        return a, b, c

    def test_grouping_has_no_transitive_chains_and_requires_timeline_coverage(self):
        a, b, c = self.fingerprints()
        groups = group_videos([c, b, a], "Strict", Event())
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].reference.record, a.record)
        self.assertEqual([v.record for v, evidence in groups[0].matches], [b.record])
        self.assertIsNone(compare_videos(a, replace(b, duration=110), "Broad"))
        self.assertIsNone(compare_videos(a, replace(b, width=180, height=320), "Broad"))
        # Low-detail slots never count as matches, even when both videos lack detail.
        self.assertIsNone(compare_videos(replace(a, signatures=(None,) * 24),
                                         replace(b, signatures=(None,) * 24), "Broad"))
        intro = replace(b, signatures=b.signatures[:8] + tuple(v ^ ((1 << 63) - 1) for v in a.signatures[8:]))
        self.assertIsNone(compare_videos(a, intro, "Broad"))

    def test_cancel_during_sampling_discards_partial_results(self):
        make_video(self.root)
        cancel = Event()
        def stop(progress):
            if progress.stage == "Sampling video frames":
                cancel.set()
        result = scan_similar([self.root], media_kind="Videos", cancel=cancel, progress=stop)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.groups)

    def test_decoder_cancel_timeout_and_output_limits_terminate_children(self):
        actual_popen, children = subprocess.Popen, []
        program = "import time;time.sleep(10)"
        def child(*args, **kwargs):
            process = actual_popen([sys.executable, "-c", program], **kwargs)
            children.append(process)
            return process
        with patch("duplicate_cleaner.video_similarity.subprocess.Popen", side_effect=child):
            with self.assertRaisesRegex(OSError, "timed out"):
                run_decoder("ffprobe", [], Event(), timeout=.15)
            cancel = Event()
            timer = Timer(.15, cancel.set)
            timer.start()
            try:
                with self.assertRaises(Cancelled):
                    run_decoder("ffprobe", [], cancel)
            finally:
                timer.cancel()
            program = "import sys;sys.stdout.write('x'*10000)"
            with self.assertRaisesRegex(OSError, "output limit"):
                run_decoder("ffprobe", [], Event(), output_limit=100)
        self.assertTrue(all(process.poll() is not None for process in children))

    def test_missing_decoder_is_actionable(self):
        with patch("duplicate_cleaner.video_similarity.Path.is_file", return_value=False):
            with self.assertRaisesRegex(OSError, "decoder is missing"):
                decoder_path("ffprobe")

    def test_packaged_video_tools_work_without_system_ffmpeg(self):
        executable = os.environ.get("DUPLICATE_CLEANER_EXE")
        if not executable:
            self.skipTest("Set DUPLICATE_CLEANER_EXE to verify bundled video tools")
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(executable)
        bundle = self.root / "portable"
        folder = bundle / "tools" / "ffmpeg"
        folder.mkdir(parents=True)
        for filename in ("ffmpeg.exe", "ffprobe.exe", "LICENSE.txt", "UPSTREAM-README.txt"):
            entry = next(name for name in archive.toc if name.replace('\\', '/') == f"tools/ffmpeg/{filename}")
            (folder / filename).write_bytes(archive.extract(entry))
        with patch.object(sys, "_MEIPASS", str(bundle), create=True), \
                patch.dict(os.environ, {"PATH": str(Path(os.environ["WINDIR"]) / "System32")}):
            original = make_video(self.root)
            make_video(self.root, "small.mkv", original.path, "scale=160:90,fps=24")
            result = scan_similar([self.root], media_kind="Videos", recursive=False)
        self.assertEqual(result.videos_compared, 2, result.issues)
        self.assertEqual(len(result.groups), 1)
