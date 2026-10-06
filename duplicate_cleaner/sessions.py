import json
import base64
import binascii
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable

from .files import capture
from .models import Cancelled, DuplicateGroup, FileRecord, Issue, Progress
from .duplicate_folders import FolderGroup, FolderRecord, FolderScanResult, ensure_folder_current, overlaps
from .empty_folders import EmptyFolderRecord
from .scanner import Reporter
from .similarity import IMAGE_SUFFIXES, ImageFingerprint, SimilarGroup, SimilarResult
from .video_similarity import VIDEO_SUFFIXES, VideoEvidence, VideoFingerprint, VideoGroup, COLUMNS, TILE_HEIGHT, TILE_WIDTH


FORMAT_NAME = "duplicate-cleaner-session"
FORMAT_VERSION = 3
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")


class SessionFormatError(ValueError):
    pass


@dataclass(frozen=True)
class SessionData:
    groups: tuple[DuplicateGroup, ...]
    selected: frozenset[Path]
    roots: tuple[str, ...]
    recursive: bool
    issues: tuple[Issue, ...]
    file_count: int
    total_bytes: int
    active_tab: str
    saved_at: str = ""
    excluded_folders: tuple[str, ...] = ()
    folder_result: FolderScanResult | None = None
    similar_result: SimilarResult | None = None
    selected_folders: frozenset[Path] = frozenset()
    selected_similar: frozenset[Path] = frozenset()
    active_workflow: str = "duplicates"
    duplicate_available: bool = True


@dataclass(frozen=True)
class SaveResult:
    path: Path
    saved_at: str = ""
    cancelled: bool = False


@dataclass(frozen=True)
class LoadResult:
    path: Path
    data: SessionData | None = None
    validation_issues: tuple[Issue, ...] = ()
    dropped_files: int = 0
    dropped_groups: int = 0
    saved_selection_count: int = 0
    dropped_selected: int = 0
    cancelled: bool = False
    dropped_folders: int = 0


def _check(cancel: Event) -> None:
    if cancel.is_set():
        raise Cancelled()


class _SessionReporter:
    def __init__(self, progress: Callable[[Progress], None] | None, stage: str, total: int):
        self.progress = progress
        self.stage = stage
        self.total = total
        self.last_emit = 0.0

    def emit(self, completed: int, path: str = "", force: bool = False) -> None:
        now = time.monotonic()
        if self.progress and (force or now - self.last_emit >= 0.08):
            self.progress(Progress(self.stage, completed, self.total, path))
            self.last_emit = now


def _record_json(record: FileRecord, selected: frozenset[Path]) -> dict:
    return {
        "path": str(record.path),
        "size": record.size,
        "modified_ns": record.modified_ns,
        "changed_ns": record.changed_ns,
        "device": record.device,
        "inode": record.inode,
        "streams": [list(pair) for pair in record.streams],
        "stream_hashes": [list(pair) for pair in record.stream_hashes],
        "selected": record.path in selected,
    }


def _issues_json(issues):
    return [{"path": str(issue.path), "reason": issue.reason} for issue in issues]


def _folder_json(result, selected, cancel):
    if result is None:
        if selected:
            raise ValueError("Folder selection has no results")
        return None
    if result.cancelled:
        raise ValueError("Cannot save an incomplete folder scan")
    groups = []
    for group in result.groups:
        copies = []
        for tree in group.folders:
            _check(cancel)
            directories = []
            for directory in tree.directories:
                _check(cancel)
                directories.append({"path": str(directory.path), "modified_ns": directory.modified_ns,
                    "changed_ns": directory.changed_ns, "device": directory.device, "inode": directory.inode})
            files = []
            for record in tree.files:
                _check(cancel)
                files.append(_record_json(record, frozenset()))
            copies.append({"path": str(tree.path), "selected": tree.path in selected,
                "directories": directories, "files": files,
                "digests": [[str(path), digest.hex()] for path, digest in tree.digests]})
        groups.append(copies)
    known = {tree.path for group in result.groups for tree in group.folders}
    if selected - known:
        raise ValueError("Selection contains folders outside the results")
    return {"groups": groups, "folder_count": result.folder_count, "issues": _issues_json(result.issues)}


def _media_json(media, selected):
    fields = {"record": _record_json(media.record, selected), "width": media.width, "height": media.height}
    if isinstance(media, VideoFingerprint):
        fields.update(duration=media.duration, timestamps=list(media.timestamps), signatures=list(media.signatures),
                      storyboard=base64.b64encode(media.storyboard).decode("ascii"))
    else:
        fields["signature"] = media.signature
    return fields


def _similar_json(result, selected, cancel):
    if result is None:
        if selected:
            raise ValueError("Similar-file selection has no results")
        return None
    if result.cancelled:
        raise ValueError("Cannot save an incomplete similarity scan")
    groups, known = [], set()
    for group in result.groups:
        _check(cancel)
        video = isinstance(group, VideoGroup)
        known.add(group.reference.record.path)
        matches = []
        for media, evidence in group.matches:
            _check(cancel)
            known.add(media.record.path)
            proof = ({"pairs": [list(pair) for pair in evidence.pairs], "matched": evidence.matched, "valid": evidence.valid,
                      "radius": evidence.radius} if video else evidence)
            matches.append({"media": _media_json(media, selected), "evidence": proof})
        groups.append({"kind": "video" if video else "image",
                       "reference": _media_json(group.reference, selected), "matches": matches})
    if selected - known:
        raise ValueError("Selection contains files outside the similar results")
    return {"groups": groups, "issues": _issues_json(result.issues),
            **{name: getattr(result, name) for name in ("image_count", "video_count", "compared_count",
                "ignored_count", "videos_compared")}}


def save_session(path: Path, data: SessionData, *, cancel: Event | None = None,
                 progress: Callable[[Progress], None] | None = None) -> SaveResult:
    path = Path(os.path.abspath(path))
    cancel = cancel if cancel is not None else Event()
    all_records = [record for group in data.groups for record in group.files]
    known = {record.path for record in all_records}
    identities = {(record.device, record.inode) for record in all_records}
    if len(known) != len(all_records) or len(identities) != len(all_records):
        raise ValueError("Duplicate file identities cannot be saved; scan again")
    if data.selected - known:
        raise ValueError("The selection contains files outside the duplicate results")
    if not data.duplicate_available and (all_records or data.issues or data.file_count or data.total_bytes):
        raise ValueError("Unavailable duplicate results must be empty")
    if not path.parent.is_dir():
        raise OSError(f"Session folder does not exist: {path.parent}")

    total = len(all_records) + len(data.issues)
    reporter = _SessionReporter(progress, "Preparing session", total)
    completed = 0
    groups = []
    temporary_path = None
    saved_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    try:
        for group in data.groups:
            files = []
            for record in group.files:
                _check(cancel)
                files.append(_record_json(record, data.selected))
                completed += 1
                reporter.emit(completed, str(record.path))
            fields = {"digest": group.digest, "files": files}
            if group.byte_verified is not None:
                fields["byte_verified"] = group.byte_verified
            groups.append(fields)
        issues = []
        for issue in data.issues:
            _check(cancel)
            issues.append({"path": str(issue.path), "reason": issue.reason})
            completed += 1
            reporter.emit(completed, str(issue.path))
        payload = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "saved_at": saved_at,
            "roots": list(data.roots),
            "recursive": data.recursive,
            "excluded_folders": list(data.excluded_folders),
            "file_count": data.file_count,
            "total_bytes": data.total_bytes,
            "active_tab": data.active_tab,
            "groups": groups,
            "issues": issues,
            "duplicate_available": data.duplicate_available,
            "active_workflow": data.active_workflow,
            "folders": _folder_json(data.folder_result, data.selected_folders, cancel),
            "similarity": _similar_json(data.similar_result, data.selected_similar, cancel),
        }
        # Validate new mode structures before replacing any existing session file.
        _parse_extra_sections(payload, cancel)
        _check(cancel)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                         dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp") as destination:
            temporary_path = Path(destination.name)
            json.dump(payload, destination, ensure_ascii=False, separators=(",", ":"))
            destination.flush()
            os.fsync(destination.fileno())
        _check(cancel)
        os.replace(temporary_path, path)
        temporary_path = None
        reporter.stage = "Session saved"
        reporter.emit(total, str(path), True)
        return SaveResult(path, saved_at)
    except Cancelled:
        return SaveResult(path, cancelled=True)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise SessionFormatError(f"{label} must be an object")
    return value


def _list(value, label: str) -> list:
    if not isinstance(value, list):
        raise SessionFormatError(f"{label} must be a list")
    return value


def _string(value, label: str) -> str:
    if not isinstance(value, str):
        raise SessionFormatError(f"{label} must be text")
    return value


def _integer(value, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SessionFormatError(f"{label} must be an integer of at least {minimum}")
    return value


def _absolute_path(value, label: str) -> Path:
    raw = _string(value, label)
    if not os.path.isabs(raw):
        raise SessionFormatError(f"{label} must be an absolute path")
    return Path(os.path.abspath(raw))


def _parse_record(value, label: str) -> tuple[FileRecord, bool]:
    fields = _mapping(value, label)
    selected = fields.get("selected")
    if not isinstance(selected, bool):
        raise SessionFormatError(f"{label}.selected must be true or false")
    streams = []
    for index, raw in enumerate(_list(fields.get("streams", []), f"{label}.streams")):
        pair = _list(raw, f"{label}.streams[{index}]")
        if len(pair) != 2:
            raise SessionFormatError(f"{label}.streams entries must contain a name and size")
        name = _string(pair[0], f"{label}.streams[{index}].name")
        if not re.fullmatch(r":[^:\\/\x00]+:\$DATA", name):
            raise SessionFormatError(f"{label}.streams contains an invalid stream name")
        streams.append((name, _integer(pair[1], f"{label}.streams[{index}].size")))
    if len({name for name, size in streams}) != len(streams):
        raise SessionFormatError(f"{label}.streams repeats a stream name")
    hashes = []
    for raw in _list(fields.get("stream_hashes", []), f"{label}.stream_hashes"):
        pair = _list(raw, f"{label}.stream_hashes entry")
        if len(pair) != 2:
            raise SessionFormatError(f"{label}.stream_hashes entries must contain a name and SHA-256")
        name = _string(pair[0], f"{label}.stream_hashes name")
        digest = _string(pair[1], f"{label}.stream_hashes digest")
        if not DIGEST_PATTERN.fullmatch(digest):
            raise SessionFormatError(f"{label}.stream_hashes has an invalid SHA-256")
        hashes.append((name, digest))
    if hashes and (len(hashes) != len(streams) or {name for name, digest in hashes} != {name for name, size in streams}):
        raise SessionFormatError(f"{label}.stream_hashes must match the stream names without duplicates")
    record = FileRecord(
        _absolute_path(fields.get("path"), f"{label}.path"),
        _integer(fields.get("size"), f"{label}.size"),
        _integer(fields.get("modified_ns"), f"{label}.modified_ns"),
        _integer(fields.get("changed_ns"), f"{label}.changed_ns"),
        _integer(fields.get("device"), f"{label}.device"),
        _integer(fields.get("inode"), f"{label}.inode", 1),
        tuple(sorted(streams)),
        tuple(sorted(hashes)),
    )
    return record, selected


def _bounded_integer(value, label, maximum, minimum=0):
    value = _integer(value, label, minimum)
    if value > maximum:
        raise SessionFormatError(f"{label} exceeds {maximum}")
    return value


def _boolean(value, label):
    if type(value) is not bool:
        raise SessionFormatError(f"{label} must be true or false")
    return value


def _number(value, label, minimum=0):
    try:
        valid = type(value) in (float, int) and math.isfinite(value) and value >= minimum
    except OverflowError:
        valid = False
    if not valid:
        raise SessionFormatError(f"{label} must be a finite number of at least {minimum}")
    return float(value)


def _parse_issues(value, label):
    issues = []
    for raw in _list(value, label):
        fields = _mapping(raw, label)
        issues.append(Issue(_absolute_path(fields.get("path"), label + ".path"),
                            _string(fields.get("reason"), label + ".reason")))
    return issues


def _unique_record(record, paths, identities, label):
    identity = record.device, record.inode
    if record.path in paths or identity in identities:
        raise SessionFormatError(f"{label} repeats a path or filesystem identity")
    paths.add(record.path)
    identities.add(identity)


def _parse_folder(value, cancel):
    fields = _mapping(value, "folder")
    path = _absolute_path(fields.get("path"), "folder.path")
    if path.parent == path:
        raise SessionFormatError("Drive roots cannot be folder cleanup results")
    selected = _boolean(fields.get("selected"), "folder.selected")
    directories, files, paths, identities = [], [], set(), set()
    for raw in _list(fields.get("directories"), "folder.directories"):
        _check(cancel)
        item = _mapping(raw, "directory")
        directory = EmptyFolderRecord(_absolute_path(item.get("path"), "directory.path"),
            *(_integer(item.get(name), "directory." + name, 1 if name == "inode" else 0)
              for name in ("modified_ns", "changed_ns", "device", "inode")))
        if directory.path != path and path not in directory.path.parents:
            raise SessionFormatError("Directory escapes its folder snapshot")
        _unique_record(directory, paths, identities, "folder snapshot")
        directories.append(directory)
    if not directories or directories[0].path != path:
        raise SessionFormatError("Folder snapshot must start with its root directory")
    directory_paths = set(paths)
    if any(item.path != path and item.path.parent not in directory_paths for item in directories):
        raise SessionFormatError("Folder directory parent chain is incomplete")
    for raw in _list(fields.get("files"), "folder.files"):
        _check(cancel)
        record, checked = _parse_record(raw, "folder.file")
        if checked or path not in record.path.parents or record.path.parent not in directory_paths:
            raise SessionFormatError("File escapes its folder snapshot or has an invalid selection")
        if len(record.stream_hashes) != len(record.streams):
            raise SessionFormatError("Folder files require complete stream hashes")
        _unique_record(record, paths, identities, "folder snapshot")
        files.append(record)
    digests = {}
    for raw in _list(fields.get("digests"), "folder.digests"):
        pair = _list(raw, "folder.digest")
        if len(pair) != 2:
            raise SessionFormatError("Folder digest must contain a path and SHA-256")
        file_path = _absolute_path(pair[0], "folder.digest.path")
        digest = _string(pair[1], "folder.digest.sha256")
        if file_path in digests or not DIGEST_PATTERN.fullmatch(digest):
            raise SessionFormatError("Invalid or repeated folder SHA-256")
        digests[file_path] = bytes.fromhex(digest)
    if set(digests) != {record.path for record in files}:
        raise SessionFormatError("Folder must have exactly one SHA-256 per file")
    return FolderRecord(path, tuple(sorted(directories, key=lambda item: str(item.path))),
                        tuple(sorted(files, key=lambda item: str(item.path))),
                        digests=tuple(sorted(digests.items()))), selected


def _storyboard(value):
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImageReader
    encoded = _string(value, "video.storyboard")
    if len(encoded) > 128 * 1024:
        raise SessionFormatError("Video storyboard exceeds its size limit")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SessionFormatError("Invalid video storyboard base64") from exc
    if not data or len(data) > 96 * 1024:
        raise SessionFormatError("Invalid video storyboard size")
    buffer = QBuffer()
    buffer.setData(data)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    if bytes(reader.format()).lower() != b"jpeg" or (size.width(), size.height()) != (TILE_WIDTH * COLUMNS, TILE_HEIGHT * 4):
        raise SessionFormatError("Video storyboard must be a 1152 by 432 JPEG")
    if reader.read().isNull():
        raise SessionFormatError("Video storyboard cannot be decoded")
    return data


def _parse_media(value, kind):
    fields = _mapping(value, "media")
    record, selected = _parse_record(fields.get("record"), "media.record")
    if record.path.suffix.casefold() not in (IMAGE_SUFFIXES if kind == "image" else VIDEO_SUFFIXES):
        raise SessionFormatError("Media path does not have a supported extension for its kind")
    width = _bounded_integer(fields.get("width"), "media.width", 2**31 - 1, 1)
    height = _bounded_integer(fields.get("height"), "media.height", 2**31 - 1, 1)
    if kind == "image":
        return ImageFingerprint(record, width, height,
            _bounded_integer(fields.get("signature"), "image.signature", 2**63 - 1)), selected
    duration = _number(fields.get("duration"), "video.duration")
    if not duration:
        raise SessionFormatError("Video duration must be positive")
    timestamps = tuple(_number(time, "video.timestamp") for time in _list(fields.get("timestamps"), "video.timestamps"))
    signatures = tuple(None if signature is None else _bounded_integer(signature, "video.signature", 2**63 - 1)
                       for signature in _list(fields.get("signatures"), "video.signatures"))
    if len(timestamps) != 24 or len(signatures) != 24 or any(time > duration for time in timestamps):
        raise SessionFormatError("Video needs 24 timestamps and signatures within its duration")
    if tuple(sorted(timestamps)) != timestamps:
        raise SessionFormatError("Video timestamps must be in order")
    return VideoFingerprint(record, width, height, duration, timestamps, signatures,
                            _storyboard(fields.get("storyboard"))), selected


def _parse_evidence(value, reference, candidate):
    fields = _mapping(value, "video.evidence")
    radius = _bounded_integer(fields.get("radius"), "video.radius", 63)
    pairs = []
    for slot, raw in enumerate(_list(fields.get("pairs"), "video.pairs")):
        pair = _list(raw, "video.pair")
        if len(pair) != 3:
            raise SessionFormatError("Video pair needs two indices and a distance")
        a, b = (_bounded_integer(pair[i], "video.frame_index", 23) for i in (0, 1))
        distance = None if pair[2] is None else _bounded_integer(pair[2], "video.distance", 63)
        if a // 2 != slot or b // 2 != slot:
            raise SessionFormatError("Video evidence frame indices do not match their temporal slot")
        first, second = reference.signatures[a], candidate.signatures[b]
        distances = [(reference.signatures[x] ^ candidate.signatures[y]).bit_count()
                     for x in (slot * 2, slot * 2 + 1) for y in (slot * 2, slot * 2 + 1)
                     if reference.signatures[x] is not None and candidate.signatures[y] is not None]
        if distance != (min(distances) if distances else None):
            raise SessionFormatError("Video evidence does not contain the closest sampled comparison")
        if distance is not None and (first is None or second is None or (first ^ second).bit_count() != distance):
            raise SessionFormatError("Video evidence does not match its fingerprints")
        pairs.append((a, b, distance))
    matched = _bounded_integer(fields.get("matched"), "video.matched", 12)
    valid = _bounded_integer(fields.get("valid"), "video.valid", 12)
    if (len(pairs) != 12 or valid != sum(pair[2] is not None for pair in pairs)
            or matched != sum(pair[2] is not None and pair[2] <= radius for pair in pairs)):
        raise SessionFormatError("Video evidence counts are inconsistent")
    return VideoEvidence(tuple(pairs), matched, valid, radius)


def _parse_extra_sections(payload, cancel):
    folder_result, similar_result = None, None
    selected_folders, selected_similar = set(), set()
    if payload.get("folders") is not None:
        fields = _mapping(payload["folders"], "folders")
        groups, paths, identities = [], set(), set()
        for raw_group in _list(fields.get("groups"), "folders.groups"):
            copies = []
            for raw in _list(raw_group, "folder group"):
                tree, selected = _parse_folder(raw, cancel)
                _unique_record(tree.directories[0], paths, identities, "folder results")
                if any(overlaps(tree.path, other.path) for other in copies):
                    raise SessionFormatError("Folder group copies must be independent")
                copies.append(tree)
                if selected:
                    selected_folders.add(tree.path)
            if not copies or (len(copies) == 1 and not copies[0].empty):
                raise SessionFormatError("Folder group needs two copies or a truly empty folder")
            groups.append(FolderGroup(tuple(copies)))
        folder_result = FolderScanResult(groups=groups, issues=_parse_issues(fields.get("issues"), "folders.issues"),
                                        folder_count=_integer(fields.get("folder_count"), "folders.folder_count"))
    if payload.get("similarity") is not None:
        fields = _mapping(payload["similarity"], "similarity")
        groups, images, videos, paths, identities = [], [], [], set(), set()
        for raw_group in _list(fields.get("groups"), "similarity.groups"):
            _check(cancel)
            group = _mapping(raw_group, "similar group")
            kind = group.get("kind")
            if kind not in ("image", "video"):
                raise SessionFormatError("Unknown similar group kind")
            reference, selected = _parse_media(group.get("reference"), kind)
            _unique_record(reference.record, paths, identities, "similar results")
            (videos if kind == "video" else images).append(reference)
            if selected:
                selected_similar.add(reference.record.path)
            matches = []
            for raw in _list(group.get("matches"), "similar.matches"):
                _check(cancel)
                match = _mapping(raw, "similar.match")
                media, selected = _parse_media(match.get("media"), kind)
                _unique_record(media.record, paths, identities, "similar results")
                (videos if kind == "video" else images).append(media)
                if selected:
                    selected_similar.add(media.record.path)
                evidence = (_parse_evidence(match.get("evidence"), reference, media) if kind == "video" else
                            _bounded_integer(match.get("evidence"), "image.distance", 63))
                if kind == "image" and evidence != (reference.signature ^ media.signature).bit_count():
                    raise SessionFormatError("Image evidence does not match its fingerprints")
                matches.append((media, evidence))
            if not matches:
                raise SessionFormatError("Similar group must have a reference and a match")
            groups.append((VideoGroup if kind == "video" else SimilarGroup)(reference, tuple(matches)))
        counters = {name: _integer(fields.get(name), "similarity." + name) for name in
                    ("image_count", "video_count", "compared_count", "ignored_count", "videos_compared")}
        if (not len(videos) <= counters["videos_compared"] <= counters["video_count"]
                or not len(images) <= counters["compared_count"] - counters["videos_compared"] <= counters["image_count"]):
            raise SessionFormatError("Similarity counters are inconsistent with the saved groups")
        similar_result = SimilarResult(groups=groups, issues=_parse_issues(fields.get("issues"), "similarity.issues"),
                                       images=tuple(images), videos=tuple(videos), **counters)
    _boolean(payload.get("duplicate_available", True), "duplicate_available")
    if payload.get("active_workflow", "duplicates") not in ("duplicates", "folders", "similarity", "location", "criteria"):
        raise SessionFormatError("Invalid active workflow tab")
    return folder_result, similar_result, selected_folders, selected_similar


def load_session(path: Path, *, cancel: Event | None = None,
                 progress: Callable[[Progress], None] | None = None) -> LoadResult:
    path = Path(os.path.abspath(path))
    cancel = cancel if cancel is not None else Event()
    try:
        _check(cancel)
        with path.open("r", encoding="utf-8") as source:
            payload = _mapping(json.load(source), "Session")
        if payload.get("format") != FORMAT_NAME:
            raise SessionFormatError("This is not a Duplicate Cleaner session")
        if type(payload.get("version")) is not int or payload["version"] not in (1, 2, FORMAT_VERSION):
            raise SessionFormatError(
                f"Unsupported session version: {payload.get('version')!r}; expected {FORMAT_VERSION}")
        saved_at = _string(payload.get("saved_at"), "saved_at")
        try:
            datetime.fromisoformat(saved_at)
        except ValueError as exc:
            raise SessionFormatError("saved_at is not a valid timestamp") from exc
        roots = tuple(str(_absolute_path(root, f"roots[{index}]"))
                      for index, root in enumerate(_list(payload.get("roots"), "roots")))
        excluded_folders = tuple(
            str(_absolute_path(folder, f"excluded_folders[{index}]"))
            for index, folder in enumerate(_list(payload.get("excluded_folders", []), "excluded_folders")))
        for folder in excluded_folders:
            if not any(Path(root) in Path(folder).parents for root in roots):
                raise SessionFormatError("Excluded folders must be subfolders of an added scan folder")
        recursive = payload.get("recursive")
        if not isinstance(recursive, bool):
            raise SessionFormatError("recursive must be true or false")
        file_count = _integer(payload.get("file_count"), "file_count")
        total_bytes = _integer(payload.get("total_bytes"), "total_bytes")
        active_tab = _string(payload.get("active_tab"), "active_tab")
        if not active_tab or len(active_tab) > 40:
            raise SessionFormatError("active_tab is invalid")

        extras = (_parse_extra_sections(payload, cancel) if payload["version"] == 3 else (None, None, set(), set()))
        folder_result, similar_result, stored_folders, stored_similar = extras
        duplicate_available = payload.get("duplicate_available", True) if payload["version"] == 3 else True
        if not duplicate_available and (payload.get("groups") or payload.get("issues") or file_count or total_bytes):
            raise SessionFormatError("Unavailable duplicate results must be empty")
        raw_groups = _list(payload.get("groups"), "groups")
        parsed_groups = []
        stored_selected = set()
        seen_paths = set()
        seen_identities = set()
        for group_index, raw_group in enumerate(raw_groups):
            group_fields = _mapping(raw_group, f"groups[{group_index}]")
            digest = group_fields.get("digest")
            if digest is not None or "digest" not in group_fields:
                digest = _string(digest, f"groups[{group_index}].digest").casefold()
                if not DIGEST_PATTERN.fullmatch(digest):
                    raise SessionFormatError(f"groups[{group_index}].digest is not a SHA-256 value")
            byte_verified = group_fields.get("byte_verified")
            if byte_verified is not None and type(byte_verified) is not bool:
                raise SessionFormatError(f"groups[{group_index}].byte_verified must be boolean")
            raw_files = _list(group_fields.get("files"), f"groups[{group_index}].files")
            if len(raw_files) < 2:
                raise SessionFormatError(f"groups[{group_index}] has fewer than two files")
            records = []
            for file_index, raw_file in enumerate(raw_files):
                record, selected = _parse_record(raw_file, f"groups[{group_index}].files[{file_index}]")
                identity = (record.device, record.inode)
                if record.path in seen_paths or identity in seen_identities:
                    raise SessionFormatError("The session repeats a file path or filesystem identity")
                seen_paths.add(record.path)
                seen_identities.add(identity)
                records.append(record)
                if selected:
                    stored_selected.add(record.path)
            parsed_groups.append(DuplicateGroup(tuple(records), digest, byte_verified))

        saved_issues = []
        for index, raw_issue in enumerate(_list(payload.get("issues"), "issues")):
            fields = _mapping(raw_issue, f"issues[{index}]")
            issue_path = _absolute_path(fields.get("path"), f"issues[{index}].path")
            reason = _string(fields.get("reason"), f"issues[{index}].reason")
            saved_issues.append(Issue(issue_path, reason))

        total = sum(len(group.files) for group in parsed_groups)
        reporter = _SessionReporter(progress, "Validating saved session", total)
        completed = 0
        valid_groups = []
        valid_selected = set()
        validation_issues = []
        dropped_files = 0
        dropped_groups = 0
        for group in parsed_groups:
            current_records = []
            for record in group.files:
                _check(cancel)
                try:
                    current = capture(record.path)
                    if current != record:
                        raise OSError("File changed or was replaced since the session was saved")
                    current_records.append(replace(current, stream_hashes=record.stream_hashes))
                except OSError as exc:
                    dropped_files += 1
                    validation_issues.append(Issue(record.path, f"Session validation: {exc}"))
                completed += 1
                reporter.emit(completed, str(record.path))
            if len(current_records) > 1:
                valid_groups.append(replace(group, files=tuple(current_records)))
                valid_selected.update(record.path for record in current_records
                                      if record.path in stored_selected)
            else:
                dropped_groups += 1
                for record in current_records:
                    validation_issues.append(Issue(
                        record.path, "Session validation: duplicate group no longer has two unchanged files"))

        valid_folders, valid_similar = set(), set()
        dropped_folders = 0
        if folder_result is not None:
            groups = []
            folder_reporter = Reporter(cancel, progress)
            folder_reporter.stage = "Validating saved folder contents"
            for group in folder_result.groups:
                copies = []
                for tree in group.folders:
                    _check(cancel)
                    try:
                        ensure_folder_current(tree, folder_reporter)
                        copies.append(tree)
                    except OSError as exc:
                        dropped_folders += 1
                        validation_issues.append(Issue(tree.path, f"Session folder validation: {exc}"))
                if len(copies) > 1 or (copies and copies[0].empty):
                    groups.append(FolderGroup(tuple(copies)))
                    valid_folders.update(tree.path for tree in copies if tree.path in stored_folders)
                else:
                    dropped_groups += 1
                    for tree in copies:
                        validation_issues.append(Issue(tree.path, "Session validation: folder group no longer has two unchanged copies"))
            folder_result = replace(folder_result, groups=groups)
        if similar_result is not None:
            groups, images, videos = [], [], []
            for group in similar_result.groups:
                survivors = []
                for media, evidence in ((group.reference, None), *group.matches):
                    _check(cancel)
                    try:
                        if capture(media.record.path) != media.record:
                            raise OSError("File changed or was replaced since the session was saved")
                        survivors.append((media, evidence))
                        (videos if isinstance(media, VideoFingerprint) else images).append(media)
                    except OSError as exc:
                        dropped_files += 1
                        validation_issues.append(Issue(media.record.path, f"Session similarity validation: {exc}"))
                if survivors and survivors[0][0] is group.reference and len(survivors) > 1:
                    groups.append(type(group)(group.reference, tuple(survivors[1:])))
                    valid_similar.update(media.record.path for media, evidence in survivors
                                         if media.record.path in stored_similar)
                else:
                    dropped_groups += 1
                    for media, evidence in survivors:
                        validation_issues.append(Issue(media.record.path, "Session validation: similar group lost its reference or all matches"))
            similar_result = replace(similar_result, groups=groups, images=tuple(images), videos=tuple(videos))
        data = SessionData(
            tuple(valid_groups), frozenset(valid_selected), roots, recursive,
            tuple(saved_issues), file_count, total_bytes, active_tab, saved_at,
            excluded_folders, folder_result, similar_result, frozenset(valid_folders),
            frozenset(valid_similar), payload.get("active_workflow", "duplicates") if payload["version"] == 3 else "duplicates",
            duplicate_available,
        )
        reporter.stage = "Session validated"
        reporter.emit(total, str(path), True)
        return LoadResult(path, data, tuple(validation_issues), dropped_files, dropped_groups,
                          len(stored_selected) + len(stored_folders) + len(stored_similar),
                          len(stored_selected - valid_selected) + len(stored_folders - valid_folders) + len(stored_similar - valid_similar),
                          dropped_folders=dropped_folders)
    except Cancelled:
        return LoadResult(path, cancelled=True)
