"""Read-only visual matching of near-complete videos, exclusively for Similar files."""

import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from time import monotonic

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter

from .files import ensure_current, open_checked
from .models import Cancelled, FileRecord, Progress

VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm"}
SLOTS = 12
TILE_WIDTH, TILE_HEIGHT, COLUMNS = 192, 108, 6
# Hamming radius, required matching slots, relative duration tolerance.
VIDEO_PRESETS = {"Strict": (5, 11, .005), "Balanced": (9, 10, .015), "Broad": (12, 9, .03)}


@dataclass(frozen=True)
class VideoFingerprint:
    record: FileRecord
    width: int
    height: int
    duration: float
    timestamps: tuple[float, ...]
    signatures: tuple[int | None, ...]
    storyboard: bytes = field(repr=False, compare=False)


@dataclass(frozen=True)
class VideoEvidence:
    # One comparison per temporal slot: reference frame, candidate frame, distance (or None).
    pairs: tuple[tuple[int, int, int | None], ...]
    matched: int
    valid: int
    radius: int


@dataclass(frozen=True)
class VideoGroup:
    reference: VideoFingerprint
    matches: tuple[tuple[VideoFingerprint, VideoEvidence], ...]


def decoder_path(name):
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    path = root / "tools" / "ffmpeg" / (name + ".exe")
    if not path.is_file():
        raise OSError("Video decoder is missing. Run tools/fetch_ffmpeg.py or use the complete portable build.")
    return path


def run_decoder(name, arguments, cancel, timeout=15, output_limit=1024 * 1024):
    """Bound subprocess time/output; disk-backed pipes avoid unbounded error buffering."""
    if cancel.is_set():
        raise Cancelled()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        with subprocess.Popen([str(decoder_path(name)), *arguments], stdin=subprocess.DEVNULL,
                              stdout=output, stderr=errors, creationflags=flags) as process:
            deadline = monotonic() + timeout
            try:
                while process.poll() is None:
                    if cancel.wait(.05):
                        raise Cancelled()
                    if monotonic() >= deadline:
                        raise OSError("Video decoder timed out")
                    if os.fstat(output.fileno()).st_size > output_limit or os.fstat(errors.fileno()).st_size > 65536:
                        raise OSError("Video decoder exceeded its output limit")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            if cancel.is_set():
                raise Cancelled()
            output.seek(0)
            errors.seek(0)
            data, error = output.read(output_limit + 1), errors.read(65537)
            if len(data) > output_limit or len(error) > 65536:
                raise OSError("Video decoder exceeded its output limit")
            if process.returncode:
                raise OSError("Video decoder failed: " + error.decode("utf-8", "replace")[:400])
            return data


def probe_video(record, cancel):
    data = run_decoder("ffprobe", ["-v", "error", "-max_alloc", "134217728",
        "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm,avi",
        "-select_streams", "V:0", "-show_entries",
        "stream=index,width,height,duration:stream_side_data=rotation:format=duration",
        "-of", "json", str(record.path)], cancel, output_limit=65536)
    try:
        info = json.loads(data)
        stream = info["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        duration = float(stream.get("duration", info.get("format", {}).get("duration", 0)))
        index = int(stream["index"])
        rotation = next((float(side["rotation"]) for side in stream.get("side_data_list", [])
                         if "rotation" in side), 0)
        if not (0 < width * height <= 100_000_000 and width > 0 and height > 0
                and math.isfinite(duration) and 0 < duration <= 7 * 86400 and index >= 0
                and math.isfinite(rotation)):
            raise ValueError()
        if round(rotation) % 180 == 90:
            width, height = height, width
        return width, height, duration, index
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise OSError("Video has no usable video stream, dimensions, or duration") from exc


def encode_storyboard(image):
    for quality in (65, 40, 20):
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "JPEG", quality):
            raise OSError("Could not create video frame previews")
        data = bytes(buffer.data())
        if len(data) <= 96 * 1024:
            return data
    raise OSError("Video frame preview exceeded its memory budget")


def read_video_fingerprint(record, cancel, progress=None):
    from .similarity import perceptual_hash

    with open_checked(record):
        width, height, duration, stream = probe_video(record, cancel)
        storyboard = QImage(COLUMNS * TILE_WIDTH, 4 * TILE_HEIGHT, QImage.Format.Format_RGB888)
        storyboard.fill(Qt.GlobalColor.black)
        signatures, timestamps = [], []
        deadline = monotonic() + 90
        for slot in range(SLOTS):
            if cancel.is_set():
                raise Cancelled()
            # Two nearby timestamps accommodate small timing/frame-rate differences.
            start = max(0, duration * (.05 + .9 * slot / (SLOTS - 1)) - .125)
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise OSError("Video sampling exceeded its 90-second deadline")
            raw = run_decoder("ffmpeg", ["-v", "error", "-nostdin", "-max_alloc", "134217728",
                "-threads", "2", "-filter_threads", "1", "-protocol_whitelist", "file,pipe",
                "-format_whitelist", "mov,matroska,webm,avi", "-ss", f"{start:.6f}",
                "-i", str(record.path), "-map", f"0:{stream}", "-an", "-sn", "-dn",
                "-vf", f"fps=4,scale={TILE_WIDTH}:{TILE_HEIGHT}:force_original_aspect_ratio=decrease,"
                       f"pad={TILE_WIDTH}:{TILE_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                "-frames:v", "2", "-threads", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
                cancel, timeout=min(15, remaining), output_limit=2 * TILE_WIDTH * TILE_HEIGHT * 3)
            length = TILE_WIDTH * TILE_HEIGHT * 3
            if len(raw) not in (length, 2 * length):
                raise OSError("Video did not provide the requested frames")
            painter = QPainter(storyboard)
            try:
                for offset in range(2):
                    # A final frame can be repeated near EOF; it still contributes only one slot.
                    actual = min(offset, len(raw) // length - 1)
                    frame = QImage(raw[actual * length:(actual + 1) * length], TILE_WIDTH, TILE_HEIGHT,
                                   TILE_WIDTH * 3, QImage.Format.Format_RGB888).copy()
                    index = slot * 2 + offset
                    painter.drawImage((index % COLUMNS) * TILE_WIDTH, (index // COLUMNS) * TILE_HEIGHT, frame)
                    try:
                        # Exclude padding introduced by our preview scaler: black bars alone are not detail.
                        factor = min(TILE_WIDTH / width, TILE_HEIGHT / height)
                        content_width = max(1, round(width * factor))
                        content_height = max(1, round(height * factor))
                        content = frame.copy((TILE_WIDTH - content_width) // 2,
                                             (TILE_HEIGHT - content_height) // 2, content_width, content_height)
                        signature = perceptual_hash(content)
                    except OSError:
                        signature = None  # Blank/low-detail frames must not count as matching evidence.
                    signatures.append(signature)
                    timestamps.append(min(duration, start + actual / 4))
            finally:
                painter.end()
            if progress:
                progress(Progress("Sampling video frames", slot + 1, SLOTS, str(record.path)))
        if sum(any(s is not None for s in signatures[i:i + 2]) for i in range(0, 24, 2)) < 9:
            raise OSError("Too few informative video frames for a reliable visual comparison")
        ensure_current(record)
        return VideoFingerprint(record, width, height, duration, tuple(timestamps), tuple(signatures),
                                encode_storyboard(storyboard))


def compare_videos(reference, candidate, preset):
    radius, required, duration_tolerance = VIDEO_PRESETS[preset]
    difference = abs(reference.duration - candidate.duration)
    if difference > max(.25, min(5, min(reference.duration, candidate.duration) * duration_tolerance)):
        return None
    if abs(math.log(reference.width * candidate.height / (reference.height * candidate.width))) > .08:
        return None
    pairs, matched, valid, thirds = [], 0, 0, [0, 0, 0]
    for slot in range(SLOTS):
        distances = [((reference.signatures[a] ^ candidate.signatures[b]).bit_count(), a, b)
                     for a in (slot * 2, slot * 2 + 1) for b in (slot * 2, slot * 2 + 1)
                     if reference.signatures[a] is not None and candidate.signatures[b] is not None]
        if distances:
            distance, a, b = min(distances)
            valid += 1
            if distance <= radius:
                matched += 1
                thirds[slot // 4] += 1
            pairs.append((a, b, distance))
        else:
            pairs.append((slot * 2, slot * 2, None))
    if matched < required or min(thirds) < 2:
        return None
    return VideoEvidence(tuple(pairs), matched, valid, radius)


def group_videos(videos, preset, cancel, progress=None):
    from .similarity import FingerprintIndex

    references, members = [], []
    index = FingerprintIndex()
    radius = VIDEO_PRESETS[preset][0]
    for count, video in enumerate(sorted(videos, key=lambda v: (-v.width * v.height, str(v.record.path))), 1):
        if cancel.is_set():
            raise Cancelled()
        candidates = set()
        # A true match must share a close sampled hash with its reference. Only references are indexed.
        for signature in video.signatures:
            if signature is not None:
                candidates.update(i for distance, i in index.find(signature, radius, cancel))
        matches = []
        for candidate in sorted(candidates):
            if cancel.is_set():
                raise Cancelled()
            evidence = compare_videos(references[candidate], video, preset)
            if evidence is not None:
                matches.append((-evidence.matched, sum(p[2] or 0 for p in evidence.pairs), candidate, evidence))
        if matches:
            _, _, candidate, evidence = min(matches)
            members[candidate].append((video, evidence))
        else:
            for signature in set(video.signatures) - {None}:
                index.add(signature, len(references))
            references.append(video)
            members.append([])
        if progress:
            progress(Progress("Comparing video sequences", count, len(videos)))
    return [VideoGroup(reference, tuple(matches)) for reference, matches in zip(references, members) if matches]


def preview_frame(video, index):
    ensure_current(video.record)
    sheet = QImage.fromData(video.storyboard, "JPEG")
    if sheet.isNull() or not 0 <= index < 24:
        raise OSError("Sampled video preview is unavailable")
    return sheet.copy((index % COLUMNS) * TILE_WIDTH, (index // COLUMNS) * TILE_HEIGHT, TILE_WIDTH, TILE_HEIGHT)


def timestamp(seconds):
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours:02}:{minutes:02}:{seconds:06.3f}"
