"""Content-multiset folder comparison and revalidated, Recycle Bin-only cleanup."""

import os
import stat
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event

from .empty_folders import EmptyFolderRecord
from .files import (UnsafeFile, capture, check_location, named_streams,
                    open_checked, open_named_streams)
from .models import Cancelled, FileRecord, Issue, RecycleResult
from .scanner import Reporter, compare_streams, full_digest, hash_named_streams, identical, differing_named_streams
from .windows_trash import recycle_file


@dataclass(frozen=True)
class FolderRecord:
    path: Path
    directories: tuple[EmptyFolderRecord, ...]
    files: tuple[FileRecord, ...]
    content_ids: tuple[int, ...] = field(default=(), compare=False)
    digests: tuple[tuple[Path, bytes], ...] = field(default=(), compare=False)

    @property
    def total_size(self):
        return sum(record.total_size for record in self.files)

    @property
    def empty(self):
        return not self.files and len(self.directories) == 1


@dataclass(frozen=True)
class FolderGroup:
    folders: tuple[FolderRecord, ...]


@dataclass
class FolderScanResult:
    groups: list[FolderGroup] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    folder_count: int = 0
    cancelled: bool = False
    index: dict[Path, FolderRecord] = field(default_factory=dict, repr=False)
    file_ids: dict[Path, int] = field(default_factory=dict, repr=False)
    roots: tuple[Path, ...] = ()
    settings: tuple = field(default=(), repr=False)

    @property
    def folders(self):
        return [folder for group in self.groups for folder in group.folders]


def directory_record(path):
    check_location(path)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or not info.st_ino:
        raise UnsafeFile("Not a folder with a reliable filesystem identity")
    if named_streams(path):
        raise UnsafeFile("Folder has extra NTFS data streams; folder recycling is unsupported")
    return EmptyFolderRecord(path, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino)


def overlaps(first, second):
    return first == second or first in second.parents or second in first.parents


def inspect_tree(path, reporter, exclusions=frozenset()):
    """Capture the whole tree. A skipped entry makes the entire tree ineligible."""
    directories, files = [], []
    stack = [path]
    while stack:
        reporter.check()
        folder = stack.pop()
        if folder in exclusions or exclusions.intersection(folder.parents):
            raise UnsafeFile("Contains an excluded folder; whole-folder comparison is incomplete")
        directories.append(directory_record(folder))
        with os.scandir(folder) as entries:
            for entry in entries:
                reporter.check()
                child = Path(entry.path)
                check_location(child)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(child)
                else:
                    files.append(capture(child))
    return FolderRecord(path, tuple(sorted(directories, key=lambda item: str(item.path))),
                        tuple(sorted(files, key=lambda item: str(item.path))))


def ensure_folder_current(record, reporter):
    if inspect_tree(record.path, reporter) != record:
        raise UnsafeFile("Folder contents changed or were replaced since scanning; scan again")


def scan_duplicate_folders(roots, recursive=True, *, excluded_folders=(), cancel=None, progress=None, previous=None):
    result = FolderScanResult()
    reporter = Reporter(cancel if cancel is not None else Event(), progress)
    reporter.stage = "Inspecting complete folder contents"
    roots = {Path(os.path.abspath(path)) for path in roots}
    result.roots = tuple(sorted(roots | set(previous.roots if previous else ())))
    if previous:
        result.folder_count = previous.folder_count
        result.issues = list(previous.issues)
    exclusions = {Path(os.path.abspath(path)) for path in excluded_folders}
    result.settings = (recursive, frozenset(exclusions))
    if previous is not None and previous.settings != result.settings:
        raise ValueError("Scan settings changed; run a fresh scan before scanning added folders")
    stack = list(roots)
    seen = set()
    inventories = {}
    children = {}
    invalid = set()
    try:
        # Discover once and assemble complete subtrees from the bottom up.
        while stack:
            reporter.check()
            path = stack.pop()
            if previous and any(path == root or root in path.parents for root in previous.roots):
                continue
            if path in exclusions or exclusions.intersection(path.parents):
                continue
            if path in inventories or path in invalid:
                continue
            reporter.path = str(path)
            try:
                directory = directory_record(path)
                identity = directory.device, directory.inode
                if identity in seen:
                    raise UnsafeFile("Overlapping folder identity; whole-folder comparison is incomplete")
                seen.add(identity)
                result.folder_count += 1
                directories, files = [], []
                with os.scandir(path) as entries:
                    for entry in entries:
                        reporter.check()
                        child = Path(entry.path)
                        try:
                            if child in exclusions:
                                raise UnsafeFile("Contains an excluded folder; whole-folder comparison is incomplete")
                            check_location(child)
                            if entry.is_dir(follow_symlinks=False):
                                directories.append(child)
                                # Even a nonrecursive candidate must include its entire tree.
                                stack.append(child)
                            else:
                                files.append(capture(child))
                        except OSError as exc:
                            invalid.add(path)
                            result.issues.append(Issue(child, str(exc)))
                inventories[path] = (directory, files)
                children[path] = directories
            except OSError as exc:
                invalid.add(path)
                result.issues.append(Issue(path, str(exc)))
            reporter.completed = result.folder_count
            reporter.emit()

        reporter.stage = "Hashing and verifying folder file contents"
        classes = defaultdict(list)
        file_ids = dict(previous.file_ids) if previous else {}
        file_digests = {}
        next_id = max(file_ids.values(), default=-1) + 1
        if previous:
            seeded = set()
            for tree in previous.index.values():
                digests = dict(tree.digests)
                file_digests.update(digests)
                for file in tree.files:
                    if file.path not in seeded:
                        seeded.add(file.path)
                        classes[file.size, digests[file.path]].append((file, file_ids[file.path]))
        for path, (directory, files) in inventories.items():
            for index, record in enumerate(files):
                reporter.check()
                reporter.path = str(record.path)
                try:
                    signature = record.size, full_digest(record, reporter)
                    file_digests[record.path] = signature[1]
                    for reference, content_id in tuple(classes[signature]):
                        try:
                            same = identical(reference, record, reporter)
                        except OSError as exc:
                            result.issues.append(Issue(reference.path, str(exc)))
                            classes[signature].remove((reference, content_id))
                            invalid.add(reference.path)
                            continue
                        if same:
                            break
                    else:
                        content_id = next_id
                        next_id += 1
                        classes[signature].append((record, content_id))
                    file_ids[record.path] = content_id
                    files[index] = replace(record, stream_hashes=hash_named_streams(record, reporter))
                except OSError as exc:
                    invalid.add(path)
                    result.issues.append(Issue(record.path, str(exc)))

        trees = dict(previous.index) if previous else {}
        if previous:
            trees = {path: tree for path, tree in trees.items()
                     if not any(file.path in invalid for file in tree.files)}
        buckets = defaultdict(list)
        for tree in trees.values():
            if (recursive or tree.path in result.roots) and tree.path.parent != tree.path:
                buckets[tree.content_ids].append(tree)
        for path in sorted(inventories, key=lambda value: len(value.parts), reverse=True):
            reporter.check()
            if path in invalid or any(child not in trees for child in children[path]):
                invalid.add(path)
                continue
            directory, files = inventories[path]
            all_dirs = [directory]
            all_files = list(files)
            for child in children[path]:
                all_dirs.extend(trees[child].directories)
                all_files.extend(trees[child].files)
            tree = FolderRecord(path, tuple(sorted(all_dirs, key=lambda item: str(item.path))),
                                tuple(sorted(all_files, key=lambda item: str(item.path))),
                                tuple(sorted(file_ids[file.path] for file in all_files)),
                                tuple((file.path, file_digests[file.path]) for file in all_files))
            trees[path] = tree
            eligible = recursive or path in result.roots
            if eligible and path.parent != path:
                try:
                    ensure_folder_current(tree, reporter)
                    buckets[tree.content_ids].append(tree)
                except OSError as exc:
                    result.issues.append(Issue(path, str(exc)))
                    trees.pop(path, None)

        result.index = dict(trees)
        # Reinspect an old tree only if it acquires a potential new counterpart.
        for key, folders in buckets.items():
            if previous and any(folder.path not in previous.index for folder in folders):
                valid = []
                for folder in folders:
                    try:
                        if folder.path in previous.index:
                            ensure_folder_current(folder, reporter)
                        valid.append(folder)
                    except OSError as exc:
                        result.issues.append(Issue(folder.path, str(exc)))
                        result.index.pop(folder.path, None)
                folders = valid
            # Ancestor/descendant copies cannot serve as independent comparison folders.
            partitions = []
            for folder in sorted(folders, key=lambda item: (len(item.path.parts), str(item.path))):
                for partition in partitions:
                    if all(not overlaps(folder.path, other.path) for other in partition):
                        partition.append(folder)
                        break
                else:
                    partitions.append([folder])
            for partition in partitions:
                if len(partition) > 1 or partition[0].empty:
                    result.groups.append(FolderGroup(tuple(partition)))
        known_files = {file.path for tree in result.index.values() for file in tree.files}
        result.file_ids = {path: value for path, value in file_ids.items() if path in known_files}
        result.groups.sort(key=lambda group: str(group.folders[0].path).casefold())
        reporter.stage = "Duplicate-folder scan complete"
        reporter.path = ""
        reporter.emit(True)
    except Cancelled:
        result.cancelled = True
        result.groups.clear()
    return result


def recycle_duplicate_folders(groups, selected, *, cancel=None, progress=None, recycler=recycle_file):
    groups = tuple(groups)
    selected = {Path(os.path.abspath(path)) for path in selected}
    folders = [folder for group in groups for folder in group.folders]
    known = {folder.path for folder in folders}
    identities = {(folder.directories[0].device, folder.directories[0].inode) for folder in folders}
    if len(known) != len(folders) or len(identities) != len(folders) or selected - known:
        raise ValueError("Invalid folder selection or duplicate identities; scan again")
    if any(overlaps(first, second) for first in selected for second in selected if first != second):
        raise ValueError("Select either a parent folder or its descendants, not both")
    reporter = Reporter(cancel if cancel is not None else Event(), progress)
    reporter.stage = "Rechecking and recycling folders"
    reporter.total = len(selected)
    result = RecycleResult()
    try:
        for group in groups:
            targets = [folder for folder in group.folders if folder.path in selected]
            if not targets:
                continue
            reference = next((folder for folder in group.folders
                              if not any(overlaps(folder.path, path) for path in selected)), targets[-1])
            recycle_reference = reference.path in selected
            reference_verified = False
            handled = set()
            try:
                with ExitStack() as locks:
                    ensure_folder_current(reference, reporter)
                    reference_streams = [(file, locks.enter_context(open_checked(file, recycle_reference)),
                                          locks.enter_context(open_named_streams(file, recycle_reference)))
                                         for file in reference.files]
                    for folder in targets:
                        reporter.check()
                        reporter.path = str(folder.path)
                        try:
                            def revalidate():
                                # Windows cannot move a directory with open descendant handles.
                                # Compare under locks, then release target handles before the Shell
                                # move. The callback repeats this check immediately before moving.
                                ensure_folder_current(folder, reporter)
                                ensure_folder_current(reference, reporter)
                                if folder is reference:
                                    if not folder.empty and not reference_verified:
                                        raise UnsafeFile("No independent matching folder could be reverified; scan again")
                                    for file in folder.files:
                                        if full_digest(file, reporter) != dict(folder.digests).get(file.path):
                                            raise UnsafeFile("Reference contents changed; folder recycling blocked")
                                        if hash_named_streams(file, reporter) != file.stream_hashes:
                                            raise UnsafeFile("Reference NTFS streams changed; folder recycling blocked")
                                else:
                                    if len(folder.files) != len(reference.files):
                                        raise UnsafeFile("Folder file counts differ; scan again")
                                    with ExitStack() as target_locks:
                                        unmatched = list(reference_streams)
                                        for file in folder.files:
                                            stream = target_locks.enter_context(open_checked(file, True))
                                            extra = target_locks.enter_context(open_named_streams(file, True))
                                            for index, (other, other_stream, other_extra) in enumerate(unmatched):
                                                if (file.size == other.size
                                                        and compare_streams(stream, other_stream, file.size, reporter)
                                                        and not differing_named_streams(file, extra, other, other_extra, reporter)):
                                                    unmatched.pop(index)
                                                    break
                                            else:
                                                raise UnsafeFile("File contents or extra NTFS streams differ; folder recycling blocked")
                                        ensure_folder_current(folder, reporter)
                                reporter.check()
                                ensure_folder_current(reference, reporter)
                                ensure_folder_current(folder, reporter)

                            if folder is reference:
                                locks.close()
                            revalidate()
                            recycler(folder.path, revalidate)
                            reference_verified = True
                            result.recycled.append(folder.path)
                        except OSError as exc:
                            result.issues.append(Issue(folder.path, str(exc)))
                        handled.add(folder.path)
                        reporter.completed += 1
                        reporter.emit(True)
            except OSError as exc:
                for folder in targets:
                    if folder.path not in handled:
                        result.issues.append(Issue(folder.path, f"Comparison folder cannot be verified: {exc}"))
                        reporter.completed += 1
                reporter.emit(True)
        reporter.path = ""
        reporter.emit(True)
    except Cancelled:
        result.cancelled = True
    return result
