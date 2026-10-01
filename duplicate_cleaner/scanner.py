import hashlib
import os
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import monotonic
from typing import BinaryIO, Callable, Iterable

from .files import capture, check_attributes, check_location, ensure_current, open_checked, open_named_streams, UnsafeFile
from .file_types import accepts_file, validate_file_types
from .matching import candidate_key, compatible
from .models import Cancelled, DuplicateGroup, FileRecord, IndexedFile, Issue, Progress, ScanResult, SearchCriteria
from .scan_control import check_scan

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
        check_scan(self.cancel)

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
    with open_checked(record) as stream:
        count = 0
        while data := reporter.read(stream, CHUNK_SIZE):
            digest.update(data)
            count += len(data)
        if count != record.size:
            raise UnsafeFile("File length changed during hashing")
        ensure_current(record)
    return digest.digest()


def hash_named_streams(record: FileRecord, reporter: Reporter) -> tuple[tuple[str, str], ...]:
    hashes = []
    with open_checked(record), open_named_streams(record) as extra:
        for name, size in record.streams:
            digest = hashlib.sha256()
            count = 0
            while data := reporter.read(extra[name], CHUNK_SIZE):
                digest.update(data)
                count += len(data)
            if count != size:
                raise UnsafeFile("NTFS data stream length changed during hashing")
            hashes.append((name, digest.hexdigest()))
        ensure_current(record)
    return tuple(hashes)


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
    if left.size != right.size:
        return False
    with open_checked(left) as first, open_checked(right) as second:
        same = compare_streams(first, second, left.size, reporter)
        ensure_current(left)
        ensure_current(right)
        return same


def differing_named_streams(left: FileRecord, first: dict[str, BinaryIO],
                            right: FileRecord, second: dict[str, BinaryIO], reporter: Reporter) -> set[str]:
    left_sizes, right_sizes = dict(left.streams), dict(right.streams)
    different = set()
    for name in sorted(left_sizes.keys() | right_sizes.keys()):
        reporter.check()
        if (name not in left_sizes or name not in right_sizes or left_sizes[name] != right_sizes[name]
                or not compare_streams(first[name], second[name], left_sizes[name], reporter)):
            different.add(name)
    return different


def scan(roots: Iterable[Path | str], recursive: bool = True, *,
         excluded_folders: Iterable[Path | str] = (),
         criteria: SearchCriteria = SearchCriteria(),
         file_types: Iterable[str] | None = None,
         cancel: Event | None = None,
         progress: Callable[[Progress], None] | None = None,
         previous: ScanResult | None = None) -> ScanResult:
    if not criteria.enabled:
        raise ValueError("Choose at least one search criterion")
    if criteria.size_tolerance < 0 or criteria.text_tolerance < 0 or criteria.folder_depth < 1:
        raise ValueError("Tolerances must be nonnegative and folder depth must be positive")
    result = ScanResult()
    if previous is not None:
        result.index = dict(previous.index)
        result.issues = list(previous.issues)
        result.file_count, result.total_bytes = previous.file_count, previous.total_bytes
    file_types = validate_file_types(file_types)
    reporter = Reporter(cancel if cancel is not None else Event(), progress)
    by_match: dict[tuple, list[FileRecord]] = defaultdict(list)
    seen_dirs: set[tuple[int, int]] = set()
    seen_files: set[tuple[int, int]] = set()
    if previous is not None:
        seen_files.update((item.record.device, item.record.inode) for item in previous.index.values())
        for item in previous.index.values():
            by_match[item.key].append(item.record)
    old_paths = set(result.index)
    next_byte_class = max((item.byte_class for item in result.index.values() if item.byte_class is not None), default=-1) + 1
    scan_roots = sorted({Path(os.path.abspath(root)) for root in roots},
                        key=lambda path: (-len(path.parts), str(path).casefold()))
    stack = list(scan_roots)
    result.roots = tuple(sorted(set(scan_roots) | set(previous.roots if previous else ())))
    search_roots = sorted(result.roots, key=lambda path: (-len(path.parts), str(path).casefold()))
    new_keys = set()
    exclusions = {Path(os.path.abspath(path)) for path in excluded_folders}
    result.settings = (recursive, frozenset(exclusions), criteria, file_types)
    if previous is not None and previous.settings != result.settings:
        raise ValueError("Scan settings changed; run a fresh scan before scanning added folders")
    try:
        reporter.emit(True)
        while stack:
            folder = stack.pop()
            reporter.check()
            if previous and any(folder == root or (recursive and root in folder.parents) for root in previous.roots):
                continue
            # Also honor exclusions when an overlapping root was explicitly added.
            if folder in exclusions or exclusions.intersection(folder.parents):
                continue
            try:
                check_location(folder, allow_cloud=True)
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
                            entry_info = entry.stat(follow_symlinks=False)
                            attributes = getattr(entry_info, "st_file_attributes", 0)
                            if entry.is_symlink():
                                raise UnsafeFile("Symbolic link or junction skipped")
                            check_attributes(path, attributes, getattr(entry_info, "st_reparse_tag", 0),
                                             allow_cloud=True)
                            if entry.is_dir(follow_symlinks=False):
                                if recursive:
                                    stack.append(path)
                                continue
                            if not accepts_file(path, file_types):
                                continue
                            record = capture(path)
                            identity = (record.device, record.inode)
                            if identity in seen_files:
                                continue
                            seen_files.add(identity)
                            # Overlapping roots always use the most specific configured root.
                            root = (next(root for root in search_roots if record.path.is_relative_to(root))
                                    if criteria.folder and criteria.from_search_root else folder)
                            key = candidate_key(record, root, entry_info, criteria)
                            by_match[key].append(record)
                            result.index[record.path] = IndexedFile(record, key)
                            new_keys.add(key)
                            result.file_count += 1
                            result.total_bytes += record.total_size
                            reporter.completed = result.file_count
                        except OSError as exc:
                            result.issues.append(Issue(path, str(exc)))
                        reporter.emit()
            except OSError as exc:
                result.issues.append(Issue(folder, str(exc)))

        candidates = [files for key, files in by_match.items() if len(files) > 1
                      and (previous is None or key in new_keys)]
        if previous:
            result.groups = [group for group in previous.groups
                             if result.index[group.files[0].path].key not in new_keys]
        # Discovery-only indexes otherwise retain every unique file until the scan ends.
        by_match.clear()
        seen_dirs.clear()
        seen_files.clear()
        digests = {}
        stages = (("Comparing samples", sample_digest), ("Hashing full contents", full_digest)) if criteria.hashes else ()
        for stage, digest_function in stages:
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
                        indexed = result.index[record.path]
                        cached = indexed.sample if stage == "Comparing samples" else indexed.digest
                        if cached is not None:
                            ensure_current(record)
                        digest = cached if cached is not None else digest_function(record, reporter)
                        if stage == "Hashing full contents" and record.streams:
                            if not record.stream_hashes:
                                record = replace(record, stream_hashes=hash_named_streams(record, reporter))
                        result.index[record.path] = replace(indexed, record=record, **{
                            "sample" if stage == "Comparing samples" else "digest": digest})
                        buckets[digest].append(record)
                    except OSError as exc:
                        result.issues.append(Issue(record.path, str(exc)))
                        result.index.pop(record.path, None)
                    reporter.completed += 1
                    reporter.emit()
                for digest, bucket in buckets.items():
                    if len(bucket) > 1:
                        next_candidates.append(bucket)
                        digests[bucket[0].path] = digest.hex()
            candidates = next_candidates

        reporter.stage = "Verifying every byte" if criteria.contents else "Matching search criteria"
        reporter.completed = 0
        reporter.total = sum(len(files) for files in candidates)
        reporter.emit(True)
        for files in candidates:
            verified: list[list[FileRecord]] = []
            try:
                for record in sorted(files, key=lambda item: (
                        item.size if criteria.size and criteria.size_tolerance else 0,
                        item.path not in old_paths, str(item.path).casefold())):
                    reporter.path = str(record.path)
                    reporter.check()
                    if record.streams and not criteria.hashes and (criteria.contents or criteria.streams):
                        if not record.stream_hashes:
                            record = replace(record, stream_hashes=hash_named_streams(record, reporter))
                            result.index[record.path] = replace(result.index[record.path], record=record)
                    for bucket in verified:
                        if criteria.streams and (record.streams, record.stream_hashes) != (
                                bucket[0].streams, bucket[0].stream_hashes):
                            continue
                        # Size-tolerant candidates arrive in ascending size order.
                        matches = not criteria.size or record.size - bucket[0].size <= criteria.size_tolerance
                        if matches and criteria.similar_names:
                            # Every pair must meet tolerances; do not chain loose matches.
                            for other in bucket:
                                reporter.check()
                                if not compatible(other, record, criteria):
                                    matches = False
                                    break
                        first_class, second_class = result.index[bucket[0].path].byte_class, result.index[record.path].byte_class
                        # Similar-name partitions can split equal contents without reading
                        # them. After cleanup changes the representatives, different class
                        # IDs therefore need a byte comparison under those criteria.
                        known_pair = (first_class is not None and second_class is not None
                                      and (not criteria.similar_names or first_class == second_class))
                        same_bytes = (first_class == second_class if known_pair else
                                      identical(bucket[0], record, reporter) if matches and criteria.contents else True)
                        if matches and (not criteria.contents or same_bytes):
                            bucket.append(record)
                            if criteria.contents:
                                result.index[record.path] = replace(result.index[record.path], byte_class=first_class)
                            break
                    else:
                        verified.append([record])
                        if criteria.contents and result.index[record.path].byte_class is None:
                            result.index[record.path] = replace(result.index[record.path], byte_class=next_byte_class)
                            next_byte_class += 1
                    reporter.completed += 1
                    reporter.emit()
                for record in files:
                    ensure_current(record)
                for bucket in verified:
                    if len(bucket) > 1 and (not criteria.ignore_same_folder or
                                            len({record.path.parent for record in bucket}) > 1):
                        result.groups.append(DuplicateGroup(
                            tuple(sorted(bucket, key=lambda item: str(item.path).casefold())),
                            digests[files[0].path] if criteria.hashes else None,
                            None if criteria.contents and criteria.hashes else criteria.contents,
                        ))
            except OSError as exc:
                for record in files:
                    result.issues.append(Issue(record.path, f"Group could not be verified: {exc}"))
                    # Do not keep a potentially stale member in the next extension's index.
                    result.index.pop(record.path, None)
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
