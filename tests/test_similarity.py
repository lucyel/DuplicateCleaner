import json
import os
import random
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event, Timer
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter

from duplicate_cleaner.files import capture
from duplicate_cleaner.models import Cancelled
from duplicate_cleaner.similarity import (
    FingerprintIndex, ImageFingerprint, PRESETS, fingerprint_image, group_images,
    read_fingerprint, scan_similar,
)
from tests.support import FileTestCase


def sample_image(seed=42):
    image = QImage(360, 240, QImage.Format.Format_RGB32)
    image.fill(QColor("#56789a"))
    painter = QPainter(image)
    painter.setPen(Qt.PenStyle.NoPen)
    randomizer = random.Random(seed)
    for _ in range(32):
        painter.setBrush(QColor(*(randomizer.randrange(30, 225) for _ in range(3))))
        painter.drawEllipse(randomizer.randrange(300), randomizer.randrange(190),
                            randomizer.randrange(10, 85), randomizer.randrange(10, 65))
    painter.end()
    return image


class SimilarityTests(FileTestCase):
    def save(self, name, image, quality=-1):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(image.save(str(path), quality=quality))
        return capture(path)

    def test_modified_copies_match_and_unrelated_images_do_not(self):
        base = sample_image()
        records = [self.save("original.png", base), self.save("compressed.jpg", base, 25),
                   self.save("resized.bmp", base.scaled(120, 80, Qt.AspectRatioMode.KeepAspectRatio,
                                                      Qt.TransformationMode.SmoothTransformation))]
        bright = base.copy()
        for y in range(bright.height()):
            for x in range(bright.width()):
                color = bright.pixelColor(x, y)
                bright.setPixelColor(x, y, QColor(min(255, color.red() + 20),
                                                 min(255, color.green() + 20), min(255, color.blue() + 20)))
        records.append(self.save("brighter.png", bright))
        originals = [fingerprint_image(record) for record in records]
        distances = [(image.signature ^ originals[0].signature).bit_count() for image in originals[1:]]
        self.assertTrue(all(distance <= PRESETS["Balanced"] for distance in distances), distances)
        unrelated = [fingerprint_image(self.save(f"different-{seed}.png", sample_image(seed))) for seed in range(8)]
        self.assertTrue(all((image.signature ^ originals[0].signature).bit_count() > PRESETS["Broad"]
                            for image in unrelated))
        groups = group_images([*originals, *unrelated], PRESETS["Balanced"], Event())
        group = next(group for group in groups if group.reference in originals)
        self.assertEqual({group.reference.record, *(image.record for image, distance in group.matches)}, set(records))

    def test_real_scan_cross_format_different_sizes_and_overlapping_roots(self):
        base = sample_image()
        first = self.save("album/original.png", base)
        second = self.save("album/copies/resized.jpg", base.scaled(180, 120), 55)
        self.file("album/notes.txt", b"not an image")
        result = scan_similar([self.root / "album", self.root / "album/copies"])
        self.assertFalse(result.cancelled)
        self.assertEqual(result.image_count, 2)
        self.assertEqual(result.ignored_count, 1)
        self.assertEqual(len(result.groups), 1, result.issues)
        self.assertNotEqual(first.size, second.size)

    def test_exclusions_apply_to_nested_and_explicit_roots(self):
        base = sample_image()
        self.save("keep.png", base)
        self.save("copy.png", base)
        self.save("excluded/hidden.png", base)
        result = scan_similar([self.root, self.root / "excluded"], excluded_folders=[self.root / "excluded"])
        self.assertEqual(result.image_count, 2)
        self.assertEqual(len(result.groups), 1)

    def test_nonrecursive_scan_and_unsupported_files(self):
        self.save("only.png", sample_image())
        self.save("child/copy.png", sample_image())
        self.file("video.mp4", b"not a video")
        result = scan_similar([self.root], recursive=False)
        self.assertEqual(result.image_count, 1)
        self.assertEqual(result.ignored_count, 1)
        self.assertFalse(result.groups)

    def test_bad_low_detail_and_changed_images_are_reported(self):
        self.file("broken.png", b"invalid")
        blank = QImage(80, 80, QImage.Format.Format_RGB32)
        blank.fill(QColor("red"))
        self.save("flat.png", blank)
        first = self.save("a.png", sample_image())
        self.save("b.png", sample_image())
        result = scan_similar([self.root])
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(len(result.issues), 2)
        self.assertTrue(any("visual detail" in issue.reason for issue in result.issues))
        first.path.write_bytes(b"changed")
        with self.assertRaises(OSError):
            read_fingerprint(first, Event())

    def test_reference_grouping_does_not_chain_and_respects_aspect_ratio(self):
        record = capture(self.file("placeholder.png"))
        images = [ImageFingerprint(replace(record, path=self.root / name), 300, 200, signature)
                  for name, signature in (("a", 0), ("b", 15), ("c", 255))]
        groups = group_images(images, 4, Event())
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].reference, images[0])
        self.assertEqual(groups[0].matches, ((images[1], 4),))
        portrait = replace(images[1], width=200, height=300, signature=0)
        self.assertFalse(group_images([images[0], portrait], 12, Event()))

    def test_index_matches_brute_force_radius_search(self):
        randomizer = random.Random(2)
        signatures = [randomizer.getrandbits(63) for _ in range(200)]
        signatures.extend(signatures[:3])
        index = FingerprintIndex()
        for i, signature in enumerate(signatures):
            index.add(signature, i)
        for query in (signatures[0], signatures[2] ^ 15, randomizer.getrandbits(63)):
            for radius in (0, 4, 12, 32):
                expected = sorted(((query ^ value).bit_count(), i) for i, value in enumerate(signatures)
                                  if (query ^ value).bit_count() <= radius)
                self.assertEqual(index.find(query, radius, Event()), expected)

    def test_cancelled_scan_discards_groups(self):
        self.save("a.png", sample_image())
        self.save("b.png", sample_image())
        cancel = Event()

        def stop(progress):
            if progress.stage == "Checking results":
                cancel.set()

        result = scan_similar([self.root], cancel=cancel, progress=stop)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.groups)
        self.assertTrue(scan_similar([self.root], cancel=cancel).cancelled)

    def test_decoder_cancel_and_timeout_terminate_child(self):
        record = self.save("a.png", sample_image())
        import sys
        real_popen = subprocess.Popen
        processes = []

        def slow_helper(*args, **kwargs):
            process = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            processes.append(process)
            return process

        with patch("duplicate_cleaner.similarity.subprocess.Popen", side_effect=slow_helper):
            with self.assertRaisesRegex(OSError, "timed out"):
                read_fingerprint(record, Event(), timeout=0.2)
            cancel = Event()
            timer = Timer(0.2, cancel.set)
            timer.start()
            try:
                with self.assertRaises(Cancelled):
                    read_fingerprint(record, cancel)
            finally:
                timer.cancel()
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_changed_reference_is_removed_at_final_validation(self):
        first = self.save("a.png", sample_image())
        self.save("b.png", sample_image())

        def change(progress):
            if progress.stage == "Comparing visual fingerprints" and progress.completed == progress.total:
                first.path.write_bytes(b"changed after decoding")

        result = scan_similar([self.root], progress=change)
        self.assertFalse(result.groups)
        self.assertTrue(result.issues)

    def test_portable_fingerprint_helper(self):
        executable = os.environ.get("DUPLICATE_CLEANER_EXE")
        if not executable:
            self.skipTest("Set DUPLICATE_CLEANER_EXE to verify the packaged decoder")
        record = self.save("image.png", sample_image())
        environment = os.environ.copy()
        environment["PATH"] = str(Path(os.environ["WINDIR"]) / "System32")
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        result = subprocess.run([executable, "--image-fingerprint"],
                                input=json.dumps(asdict(record), default=str).encode("utf-8"),
                                capture_output=True, timeout=30, env=environment, cwd=self.root,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = json.loads(result.stdout)
        self.assertEqual(fields["signature"], fingerprint_image(record).signature)
