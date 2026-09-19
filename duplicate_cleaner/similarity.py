"""Read-only media similarity scanning, independent of duplicate matching."""

import json
import math
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from threading import Event
from time import monotonic

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QPainter

from .files import capture, check_location, ensure_current, open_checked
from .models import Cancelled, FileRecord, Issue, Progress
from .thumbnails import _binary_pipe
from .video_similarity import VIDEO_SUFFIXES, VideoGroup, group_videos, read_video_fingerprint

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
PRESETS = {"Strict": 4, "Balanced": 8, "Broad": 12}


@dataclass(frozen=True)
class ImageFingerprint:
    record: FileRecord
    width: int
    height: int
    signature: int


@dataclass(frozen=True)
class SimilarGroup:
    reference: ImageFingerprint
    matches: tuple[tuple[ImageFingerprint, int], ...]


@dataclass
class SimilarResult:
    groups: list[SimilarGroup | VideoGroup] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    image_count: int = 0
    compared_count: int = 0
    ignored_count: int = 0
    cancelled: bool = False
    video_count: int = 0
    videos_compared: int = 0


def fingerprint_image(record: FileRecord) -> ImageFingerprint:
    with open_checked(record):
        QImageReader.setAllocationLimit(128)
        reader = QImageReader(str(record.path))
        reader.setAutoTransform(True)
        size = reader.size()
        if not size.isValid():
            raise OSError("Cannot read image dimensions: " + reader.errorString())
        reader.setScaledSize(size.scaled(QSize(256, 256), Qt.AspectRatioMode.KeepAspectRatio))
        if reader.transformation().value & 4:
            size.transpose()
        image = reader.read()
        if image.isNull():
            raise OSError("Cannot decode image: " + reader.errorString())
        signature = perceptual_hash(image)
        ensure_current(record)
        return ImageFingerprint(record, size.width(), size.height(), signature)


def perceptual_hash(image):
    image = image.scaled(32, 32, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    if image.hasAlphaChannel():
        background = QImage(32, 32, QImage.Format.Format_RGB32)
        background.fill(Qt.GlobalColor.white)
        painter = QPainter(background)
        painter.drawImage(0, 0, image)
        painter.end()
        image = background
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    data, stride = bytes(gray.constBits()), gray.bytesPerLine()
    pixels = [[data[y * stride + x] for x in range(32)] for y in range(32)]
    mean = sum(map(sum, pixels)) / 1024
    if sum((value - mean) ** 2 for row in pixels for value in row) / 1024 < 4:
        raise OSError("Too little visual detail for a reliable similarity fingerprint")
    # Only the low-frequency DCT coefficients are needed; exclude overall brightness (DC).
    cosines = [[math.cos(math.pi * (2 * x + 1) * u / 64) for x in range(32)] for u in range(8)]
    rows = [[sum(row[x] * cosines[u][x] for x in range(32)) for u in range(8)] for row in pixels]
    coefficients = [sum(rows[y][u] * cosines[v][y] for y in range(32))
                    * (1 / math.sqrt(2) if u == 0 else 1)
                    * (1 / math.sqrt(2) if v == 0 else 1)
                    for v in range(8) for u in range(8) if (u, v) != (0, 0)]
    center = median(coefficients)
    signature = sum(1 << index for index, value in enumerate(coefficients) if value > center)
    return signature


def fingerprint_main():
    with _binary_pipe(sys.stdin, -10, "rb") as source, _binary_pipe(sys.stdout, -11, "wb") as output:
        try:
            fields = json.loads(source.read())
            fields["path"] = Path(fields["path"])
            for key in ("streams", "stream_hashes"):
                fields[key] = tuple(tuple(pair) for pair in fields.get(key, ()))
            result = fingerprint_image(FileRecord(**fields))
            payload = {"width": result.width, "height": result.height, "signature": result.signature}
        except Exception as exc:
            payload = {"error": str(exc)}
        output.write(json.dumps(payload).encode("utf-8"))
    return 0


def read_fingerprint(record, cancel, timeout=10):
    executable = Path(sys.executable)
    arguments = ["--image-fingerprint"]
    if not getattr(sys, "frozen", False):
        if os.name == "nt":
            executable = executable.with_name("pythonw.exe")
        arguments = ["-m", "duplicate_cleaner.similarity", "--fingerprint"]
    ensure_current(record)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with subprocess.Popen([str(executable), *arguments], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          cwd=Path(__file__).resolve().parents[1], creationflags=flags) as process:
        payload = json.dumps(asdict(record), default=str).encode("utf-8")
        deadline = monotonic() + timeout
        try:
            while True:
                if cancel.is_set():
                    raise Cancelled()
                if monotonic() >= deadline:
                    raise OSError("Image decoder timed out")
                try:
                    output, errors = process.communicate(payload, timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    payload = None
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    if process.returncode:
        raise OSError("Image decoder failed" + (": " + errors.decode("utf-8", "replace")[:300] if errors else ""))
    try:
        fields = json.loads(output)
        if "error" in fields:
            raise OSError(fields["error"])
        width, height, signature = fields["width"], fields["height"], fields["signature"]
        if not (isinstance(width, int) and width > 0 and isinstance(height, int) and height > 0
                and isinstance(signature, int) and 0 <= signature < 2 ** 63):
            raise ValueError("Invalid fingerprint")
    except (ValueError, KeyError, TypeError) as exc:
        raise OSError("Image decoder returned an invalid fingerprint") from exc
    ensure_current(record)
    return ImageFingerprint(record, width, height, signature)


class FingerprintIndex:
    """BK-tree over reference hashes; Hamming distance permits exact radius lookup."""

    def __init__(self):
        self.root = None

    def add(self, signature, index):
        node = self.root
        if node is None:
            self.root = (signature, [index], {})
            return
        while True:
            distance = (signature ^ node[0]).bit_count()
            if distance == 0:
                node[1].append(index)
                return
            if distance not in node[2]:
                node[2][distance] = (signature, [index], {})
                return
            node = node[2][distance]

    def find(self, signature, radius, cancel):
        stack = [self.root] if self.root is not None else []
        matches = []
        while stack:
            if cancel.is_set():
                raise Cancelled()
            value, indices, children = stack.pop()
            distance = (signature ^ value).bit_count()
            if distance <= radius:
                matches.extend((distance, index) for index in indices)
            stack.extend(child for edge, child in children.items() if distance - radius <= edge <= distance + radius)
        return sorted(matches)


def group_images(images, radius, cancel, progress=None):
    references, members = [], []
    index = FingerprintIndex()
    # Prefer a larger image as the stable reference. Members never become extra reference links.
    images = sorted(images, key=lambda item: (-item.width * item.height, str(item.record.path).casefold()))
    for count, image in enumerate(images, 1):
        if cancel.is_set():
            raise Cancelled()
        for distance, candidate in index.find(image.signature, radius, cancel):
            reference = references[candidate]
            aspect_ratio = image.width * reference.height / (image.height * reference.width)
            if abs(math.log(aspect_ratio)) <= 0.08:
                members[candidate].append((image, distance))
                break
        else:
            index.add(image.signature, len(references))
            references.append(image)
            members.append([])
        if progress and (count % 100 == 0 or count == len(images)):
            progress(Progress("Comparing visual fingerprints", count, len(images)))
    return [SimilarGroup(reference, tuple(sorted(matches, key=lambda pair: (pair[1], str(pair[0].record.path)))))
            for reference, matches in zip(references, members) if matches]


def scan_similar(roots, recursive=True, *, excluded_folders=(), preset="Balanced", cancel=None, progress=None,
                 media_kind="Images"):
    if preset not in PRESETS:
        raise ValueError("Choose Strict, Balanced, or Broad similarity")
    if media_kind not in ("All", "Images", "Videos"):
        raise ValueError("Choose All, Images, or Videos")
    suffixes = (IMAGE_SUFFIXES if media_kind != "Videos" else set()) | (VIDEO_SUFFIXES if media_kind != "Images" else set())
    cancel = cancel if cancel is not None else Event()
    result = SimilarResult()
    exclusions = {Path(os.path.abspath(path)) for path in excluded_folders}
    stack = [Path(os.path.abspath(path)) for path in roots]
    seen_dirs, seen_files, records = set(), set(), []
    last_report = 0.0

    def report(stage, completed, total=0, path="", force=False):
        nonlocal last_report
        if cancel.is_set():
            raise Cancelled()
        now = monotonic()
        if progress and (force or now - last_report >= 0.1):
            progress(Progress(stage, completed, total, str(path)))
            last_report = now
            if cancel.is_set():
                raise Cancelled()

    try:
        report("Finding supported media", 0, force=True)
        while stack:
            if cancel.is_set():
                raise Cancelled()
            folder = stack.pop()
            if folder in exclusions or exclusions.intersection(folder.parents):
                continue
            try:
                check_location(folder)
                info = folder.stat()
                if not info.st_ino:
                    raise OSError("Folder identity is unavailable")
                identity = (info.st_dev, info.st_ino)
                if identity in seen_dirs:
                    continue
                seen_dirs.add(identity)
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if cancel.is_set():
                            raise Cancelled()
                        path = Path(entry.path)
                        if path in exclusions:
                            continue
                        try:
                            attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
                            if entry.is_symlink() or attributes & 0x400:
                                raise OSError("Link, junction, or reparse point skipped")
                            if entry.is_dir(follow_symlinks=False):
                                if recursive:
                                    stack.append(path)
                            elif path.suffix.casefold() in suffixes:
                                record = capture(path)
                                identity = (record.device, record.inode)
                                if identity not in seen_files:
                                    seen_files.add(identity)
                                    records.append(record)
                            else:
                                result.ignored_count += 1
                        except OSError as exc:
                            result.issues.append(Issue(path, str(exc)))
                        report("Finding supported media", len(records), path=path)
            except OSError as exc:
                result.issues.append(Issue(folder, str(exc)))
        result.video_count = sum(record.path.suffix.casefold() in VIDEO_SUFFIXES for record in records)
        result.image_count = len(records) - result.video_count
        images, videos = [], []
        for count, record in enumerate(records, 1):
            report("Reading visual fingerprints", count - 1, len(records), record.path, force=True)
            try:
                if record.path.suffix.casefold() in VIDEO_SUFFIXES:
                    videos.append(read_video_fingerprint(record, cancel, progress))
                else:
                    images.append(read_fingerprint(record, cancel))
            except OSError as exc:
                result.issues.append(Issue(record.path, str(exc)))
        result.videos_compared = len(videos)
        result.compared_count = len(images) + len(videos)
        report("Comparing visual fingerprints", 0, len(images), force=True)
        groups = group_images(images, PRESETS[preset], cancel, progress)
        groups.extend(group_videos(videos, preset, cancel, progress))
        for count, group in enumerate(groups, 1):
            report("Checking results", count, len(groups), force=count == 1)
            try:
                ensure_current(group.reference.record)
            except OSError as exc:
                result.issues.append(Issue(group.reference.record.path, str(exc)))
                continue
            matches = []
            for image, distance in group.matches:
                if cancel.is_set():
                    raise Cancelled()
                try:
                    ensure_current(image.record)
                    matches.append((image, distance))
                except OSError as exc:
                    result.issues.append(Issue(image.record.path, str(exc)))
            if matches:
                result.groups.append(type(group)(group.reference, tuple(matches)))
        report("Similarity scan complete", result.compared_count, result.compared_count, force=True)
    except Cancelled:
        result.cancelled = True
        result.groups.clear()
    return result


if __name__ == "__main__":
    raise SystemExit(fingerprint_main() if sys.argv[1:] == ["--fingerprint"] else 2)
