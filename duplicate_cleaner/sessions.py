import json
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


FORMAT_NAME = "duplicate-cleaner-session"
FORMAT_VERSION = 2
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
        "streams": record.streams,
        "stream_hashes": record.stream_hashes,
        "selected": record.path in selected,
    }


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
        }
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
        if payload.get("version") not in (1, FORMAT_VERSION):
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

        data = SessionData(
            tuple(valid_groups), frozenset(valid_selected), roots, recursive,
            tuple(saved_issues), file_count, total_bytes, active_tab, saved_at,
            excluded_folders,
        )
        reporter.stage = "Session validated"
        reporter.emit(total, str(path), True)
        return LoadResult(path, data, tuple(validation_issues), dropped_files, dropped_groups,
                          len(stored_selected), len(stored_selected - valid_selected))
    except Cancelled:
        return LoadResult(path, cancelled=True)
