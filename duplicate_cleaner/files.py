import os
import stat
from contextlib import contextmanager, ExitStack
from pathlib import Path
from typing import BinaryIO, Iterator

from .models import FileRecord


class UnsafeFile(OSError):
    pass


def check_location(path: Path) -> None:
    # Check ancestors too: a selected folder can itself be inside a junction.
    for part in (path, *path.parents):
        info = part.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & 0x400:
            raise UnsafeFile(f"Link, junction, or reparse point skipped: {part}")
        if attributes & (0x1000 | 0x40000 | 0x400000):
            raise UnsafeFile(f"Offline or cloud-only file skipped: {part}")


def capture(path: Path) -> FileRecord:
    check_location(path)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeFile("Not a regular file")
    if info.st_nlink > 1:
        raise UnsafeFile("Hard-linked file skipped; its paths share the same stored data")
    if not info.st_ino:
        raise UnsafeFile("The filesystem did not provide a reliable file identity")
    return FileRecord(path, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                      info.st_dev, info.st_ino, named_streams(path))


def ensure_current(record: FileRecord) -> None:
    if capture(record.path) != record:
        raise UnsafeFile("File changed or was replaced since discovery; scan again")


def named_streams(path: Path) -> tuple[tuple[str, int], ...]:
    if os.name != "nt":
        return ()
    import pywintypes
    import win32file

    try:
        streams = win32file.FindStreams(str(path))
    except pywintypes.error as exc:
        # FAT/exFAT do not support named streams. Other errors fail closed.
        if exc.winerror in (1, 38, 50):
            return ()
        raise OSError(str(exc)) from exc
    return tuple(sorted((name, size) for size, name in streams if name != "::$DATA"))


def _open_read(path: Path | str, allow_delete: bool) -> BinaryIO:
    if os.name == "nt":
        import msvcrt
        import pywintypes
        import win32con
        import win32file

        share = win32con.FILE_SHARE_READ
        if allow_delete:
            share |= win32con.FILE_SHARE_DELETE
        try:
            handle = win32file.CreateFile(
                str(path), win32con.GENERIC_READ, share, None,
                win32con.OPEN_EXISTING, win32con.FILE_FLAG_SEQUENTIAL_SCAN, None,
            )
        except pywintypes.error as exc:
            raise OSError(str(exc)) from exc
        try:
            descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
        except BaseException:
            handle.Close()
            raise
        handle.Detach()  # The descriptor now owns the Windows handle.
        try:
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
    return open(path, "rb")


@contextmanager
def open_checked(record: FileRecord, allow_delete: bool = False) -> Iterator[BinaryIO]:
    ensure_current(record)
    with _open_read(record.path, allow_delete) as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            record.device, record.inode, record.size, record.modified_ns
        ):
            raise UnsafeFile("File changed while it was being opened")
        ensure_current(record)
        yield stream


@contextmanager
def open_named_streams(record: FileRecord, allow_delete: bool = False) -> Iterator[dict[str, BinaryIO]]:
    # Windows sharing locks apply per stream. Keep every named stream locked
    # until comparison/recycling finishes, just like the main data stream.
    ensure_current(record)
    with ExitStack() as stack:
        opened = {}
        for name, size in record.streams:
            stream = stack.enter_context(_open_read(str(record.path) + name, allow_delete))
            info = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino, info.st_size) != (record.device, record.inode, size):
                raise UnsafeFile("NTFS data stream changed while it was being opened; scan again")
            opened[name] = stream
        ensure_current(record)
        yield opened
