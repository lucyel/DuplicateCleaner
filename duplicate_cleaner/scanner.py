import hashlib
import os
from collections import defaultdict
from pathlib import Path
from threading import Event
from time import monotonic
from typing import BinaryIO, Callable, Iterable

from .files import capture, check_location, ensure_current, open_checked, UnsafeFile
from .models import Cancelled, DuplicateGroup, FileRecord, Issue, Progress, ScanResult

CHUNK_SIZE = 1024 * 1024
SAMPLE_SIZE = 64 * 1024


class Reporter:
    def __init__(self, cancel: Event, callback: Callable[[Progress], None] | None):
        self.cancel = cancel
        self.callback = callback
        self.last_emit = 0.0
        self.bytes_read = 0
        self.stage = "Discovering files"
        self.completed = 0
        self.total = 0
        self.path = ""

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def emit(self, force: bool = False) -> None:
        self.check()
        now = monotonic()
        if self.callback and (force or now - self.last_emit >= 0.1):
            self.callback(Progress(self.stage, self.completed, self.total,
                                   self.path, self.bytes_read))
            self.last_emit = now

    def read(self, stream: BinaryIO, size: int) -> bytes:
        self.check()
        data = stream.read(size)
        self.bytes_read += len(data)
        self.emit()
        return data


def sample_digest(record: FileRecord, reporter: Reporter) -> bytes:
    digest = hashlib.sha256()
    offsets = sorted({0, max(0, (record.size - SAMPLE_SIZE) // 2),
                      max(0, record.size - SAMPLE_SIZE)})
    with open_checked(record) as stream:
        for offset in offsets:
            stream.seek(offset)
            data = reporter.read(stream, min(SAMPLE_SIZE, record.size - offset))
            if len(data) != min(SAMPLE_SIZE, record.size - offset):
                raise UnsafeFile("File length changed during sampling")
            digest.update(data)
        ensure_current(record)
    return digest.digest()


def full_digest(record: FileRecord, reporter: Reporter) -> bytes:
    digest = hashlib.sha256()
    count = 0
    with open_checked(record) as stream:
        while data := reporter.read(stream, CHUNK_SIZE):
            digest.update(data)
            count += len(data)
        if count != record.size:
            raise UnsafeFile("File length changed during hashing")
        ensure_current(record)
    return digest.digest()


def compare_streams(left: BinaryIO, right: BinaryIO, size: int, reporter: Reporter) -> bool:
    left.seek(0)
    right.seek(0)
    count = 0
    while True:
        first = reporter.read(left, CHUNK_SIZE)
        second = reporter.read(right, CHUNK_SIZE)
        if first != second:
            return False
        if not first:
            if count != size:
                raise UnsafeFile("File length changed during byte comparison")
            return True
        count += len(first)


def identical(left: FileRecord, right: FileRecord, reporter: Reporter) -> bool:
    with open_checked(left) as first, open_checked(right) as second:
        same = compare_streams(first, second, left.size, reporter)
        ensure_current(left)
        ensure_current(right)
        return same


def scan(roots: Iterable[Path | str], recursive: bool = True, *,
         excluded_folders: Iterable[Path | str] = (),
         cancel: Event | None = None,
         progress: Callable[[Progress], None] | None = None) -> ScanResult:
    result = ScanResult()
    reporter = Reporter(cancel if cancel is not None else Event(), progress)
    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    seen_dirs: set[tuple[int, int]] = set()
    seen_files: set[tuple[int, int]] = set()
    stack = [Path(os.path.abspath(root)) for root in roots]
    exclusions = {Path(os.path.abspath(path)) for path in excluded_folders}
    try:
        reporter.emit(True)
        while stack:
            folder = stack.pop()
            reporter.check()
            # Also honor exclusions when an overlapping root was explicitly added.
            if folder in exclusions or exclusions.intersection(folder.parents):
                continue
            try:
                check_location(folder)
                info = folder.stat()
                if not info.st_ino:
                    raise UnsafeFile("The filesystem did not provide a reliable folder identity")
                identity = (info.st_dev, info.st_ino)
                if identity in seen_dirs:
                    continue
                seen_dirs.add(identity)
                with os.scandir(folder) as entries:
                    for entry in entries:
                        reporter.check()
                        path = Path(entry.path)
                        if path in exclusions:
                            continue
                        reporter.path = str(path)
                        try:
                            attributes = getattr(entry.stat(follow_symlinks=False),
                                                 "st_file_attributes", 0)
                            if entry.is_symlink() or attributes & 0x400:
                                raise UnsafeFile("Link, junction, or reparse point skipped")
                            if entry.is_dir(follow_symlinks=False):
                                if recursive:
                                    stack.append(path)
                                continue
                            record = capture(path)
                            identity = (record.device, record.inode)
                            if identity in seen_files:
                                continue
                            seen_files.add(identity)
                            by_size[record.size].append(record)
                            result.file_count += 1
                            result.total_bytes += record.size
                            reporter.completed = result.file_count
                        except OSError as exc:
                            result.issues.append(Issue(path, str(exc)))
                        reporter.emit()
            except OSError as exc:
                result.issues.append(Issue(folder, str(exc)))

        candidates = [files for files in by_size.values() if len(files) > 1]
        # Discovery-only indexes otherwise retain every unique file until the scan ends.
        by_size.clear()
        seen_dirs.clear()
        seen_files.clear()
        for stage, digest_function in (("Comparing samples", sample_digest),
                                       ("Hashing full contents", full_digest)):
            reporter.stage = stage
            reporter.completed = 0
            reporter.total = sum(len(files) for files in candidates)
            reporter.emit(True)
            next_candidates = []
            digests = {}
            for files in candidates:
                buckets: dict[bytes, list[FileRecord]] = defaultdict(list)
                for record in files:
                    reporter.path = str(record.path)
                    try:
                        digest = digest_function(record, reporter)
                        buckets[digest].append(record)
                    except OSError as exc:
                        result.issues.append(Issue(record.path, str(exc)))
                    reporter.completed += 1
                    reporter.emit()
                for digest, bucket in buckets.items():
                    if len(bucket) > 1:
                        next_candidates.append(bucket)
                        digests[bucket[0].path] = digest.hex()
            candidates = next_candidates

        reporter.stage = "Verifying every byte"
        reporter.completed = 0
        reporter.total = sum(len(files) for files in candidates)
        reporter.emit(True)
        for files in candidates:
            verified: list[list[FileRecord]] = []
            try:
                for record in files:
                    reporter.path = str(record.path)
                    reporter.check()
                    for bucket in verified:
                        if identical(bucket[0], record, reporter):
                            bucket.append(record)
                            break
                    else:
                        verified.append([record])
                    reporter.completed += 1
                    reporter.emit()
                for record in files:
                    ensure_current(record)
                for bucket in verified:
                    if len(bucket) > 1:
                        result.groups.append(DuplicateGroup(
                            tuple(sorted(bucket, key=lambda item: str(item.path).casefold())),
                            digests[files[0].path],
                        ))
            except OSError as exc:
                for record in files:
                    result.issues.append(Issue(record.path, f"Group could not be verified: {exc}"))
        result.groups.sort(key=lambda group: group.extra_bytes, reverse=True)
        reporter.stage = "Scan complete"
        reporter.completed = reporter.total
        reporter.path = ""
        reporter.emit(True)
    except Cancelled:
        result.cancelled = True
        # An interrupted scan never exposes a partial group for cleanup.
        result.groups.clear()
    return result
