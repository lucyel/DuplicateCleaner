import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Iterable

from .files import UnsafeFile, check_location
from .models import Cancelled, Issue, Progress
from .scanner import Reporter
from .windows_trash import recycle_file


@dataclass(frozen=True)
class EmptyFolderRecord:
    path: Path
    modified_ns: int
    changed_ns: int
    device: int
    inode: int


@dataclass
class EmptyFolderScanResult:
    folders: list[EmptyFolderRecord] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    folder_count: int = 0
    cancelled: bool = False


@dataclass
class EmptyFolderRecycleResult:
    recycled: list[Path] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    cancelled: bool = False


def capture_empty_folder(path: Path) -> EmptyFolderRecord:
    path = Path(os.path.abspath(path))
    check_location(path)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeFile("Not a folder")
    if not info.st_ino:
        raise UnsafeFile("The filesystem did not provide a reliable folder identity")
    with os.scandir(path) as entries:
        if next(entries, None) is not None:
            raise UnsafeFile("Folder is no longer empty")
    return EmptyFolderRecord(path, info.st_mtime_ns, info.st_ctime_ns,
                             info.st_dev, info.st_ino)


def ensure_empty_folder_current(record: EmptyFolderRecord) -> None:
    if capture_empty_folder(record.path) != record:
        raise UnsafeFile("Folder changed or was replaced since discovery; scan again")


def scan_empty_folders(roots: Iterable[Path | str], recursive: bool = True, *,
                       excluded_folders: Iterable[Path | str] = (),
                       cancel: Event | None = None,
                       progress: Callable[[Progress], None] | None = None) -> EmptyFolderScanResult:
    result = EmptyFolderScanResult()
    reporter = Reporter(cancel if cancel is not None else Event(), progress)
    reporter.stage = "Inspecting folders for empty paths"
    root_paths = [Path(os.path.abspath(root)) for root in roots]
    excluded = {os.path.normcase(str(path)) for path in root_paths}
    exclusions = {Path(os.path.abspath(path)) for path in excluded_folders}
    seen: set[tuple[int, int]] = set()
    stack = [(path, 0) for path in root_paths]
    try:
        reporter.emit(True)
        while stack:
            folder, depth = stack.pop()
            reporter.check()
            if folder in exclusions or exclusions.intersection(folder.parents):
                continue
            reporter.path = str(folder)
            try:
                check_location(folder)
                info = folder.stat(follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    raise UnsafeFile("Not a folder")
                if not info.st_ino:
                    raise UnsafeFile("The filesystem did not provide a reliable folder identity")
                identity = (info.st_dev, info.st_ino)
                if identity in seen:
                    continue
                seen.add(identity)
                result.folder_count += 1
                reporter.completed = result.folder_count

                is_empty = True
                children: list[Path] = []
                with os.scandir(folder) as entries:
                    for entry in entries:
                        reporter.check()
                        is_empty = False
                        # Excluded entries still make their parent nonempty.
                        if Path(entry.path) in exclusions:
                            continue
                        try:
                            if entry.is_symlink():
                                raise UnsafeFile("Link, junction, or reparse point skipped")
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                            entry_info = entry.stat(follow_symlinks=False)
                            if getattr(entry_info, "st_file_attributes", 0) & 0x400:
                                raise UnsafeFile("Link, junction, or reparse point skipped")
                            if recursive or depth == 0:
                                children.append(Path(entry.path))
                        except OSError as exc:
                            result.issues.append(Issue(Path(entry.path), str(exc)))

                if is_empty and os.path.normcase(str(folder)) not in excluded:
                    result.folders.append(EmptyFolderRecord(
                        folder, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino))
                stack.extend((child, depth + 1) for child in children)
                reporter.emit()
            except OSError as exc:
                result.issues.append(Issue(folder, str(exc)))

        result.folders.sort(key=lambda record: str(record.path).casefold())
        reporter.stage = "Empty-folder scan complete"
        reporter.path = ""
        reporter.emit(True)
    except Cancelled:
        result.cancelled = True
        result.folders.clear()
    return result


def recycle_empty_folders(folders: Iterable[EmptyFolderRecord], selected: Iterable[Path], *,
                          cancel: Event | None = None,
                          progress: Callable[[Progress], None] | None = None,
                          recycler: Callable = recycle_file) -> EmptyFolderRecycleResult:
    folders = tuple(folders)
    selected = {Path(os.path.abspath(path)) for path in selected}
    known = {record.path for record in folders}
    identities = {(record.device, record.inode) for record in folders}
    if len(known) != len(folders) or len(identities) != len(folders):
        raise ValueError("Duplicate folder identities in the results; scan again")
    if selected - known:
        raise ValueError("The selection contains folders outside the empty-folder results")

    result = EmptyFolderRecycleResult()
    reporter = Reporter(cancel if cancel is not None else Event(), progress)
    reporter.stage = "Rechecking and recycling empty folders"
    reporter.total = len(selected)
    try:
        reporter.emit(True)
        for record in folders:
            if record.path not in selected:
                continue
            reporter.check()
            reporter.path = str(record.path)
            try:
                def revalidate():
                    reporter.check()
                    ensure_empty_folder_current(record)

                revalidate()
                recycler(record.path, revalidate)
                result.recycled.append(record.path)
            except OSError as exc:
                result.issues.append(Issue(record.path, str(exc)))
            reporter.completed += 1
            reporter.emit(True)
        reporter.path = ""
        reporter.emit(True)
    except Cancelled:
        result.cancelled = True
    return result
