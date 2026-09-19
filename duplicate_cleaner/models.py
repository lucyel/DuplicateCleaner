from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SearchCriteria:
    contents: bool = True
    hashes: bool = True
    size: bool = True
    filename: bool = False
    extension: bool = False
    modified: bool = False
    streams: bool = False
    similar_names: bool = False
    ignore_copy: bool = False
    size_tolerance: int = 0
    created: bool = False
    created_date_only: bool = False
    modified_date_only: bool = False
    same_drive: bool = False
    folder: bool = False
    full_folder: bool = False
    folder_depth_enabled: bool = False
    folder_depth: int = 1
    from_search_root: bool = False
    ignore_same_folder: bool = False
    case_sensitive: bool = False
    text_tolerance: int = 3

    @property
    def enabled(self) -> bool:
        return any((self.contents, self.hashes, self.size, self.filename, self.extension,
                    self.modified, self.streams, self.similar_names, self.created, self.same_drive, self.folder))


@dataclass(frozen=True)
class FileRecord:
    path: Path
    size: int
    modified_ns: int
    changed_ns: int
    device: int
    inode: int
    streams: tuple[tuple[str, int], ...] = ()
    # Content hashes are scan results, not filesystem identity metadata.
    stream_hashes: tuple[tuple[str, str], ...] = field(default=(), compare=False)

    @property
    def total_size(self) -> int:
        return self.size + sum(size for name, size in self.streams)


@dataclass(frozen=True)
class DuplicateGroup:
    files: tuple[FileRecord, ...]
    # None means no main-file hash was calculated (metadata-only or byte-only scanning).
    digest: str | None
    # Older groups infer byte verification from their digest. New modes record it explicitly.
    byte_verified: bool | None = None

    @property
    def contents_verified(self) -> bool:
        return self.byte_verified if self.byte_verified is not None else self.digest is not None

    @property
    def extra_bytes(self) -> int:
        return sum(record.total_size for record in self.files) - max(record.total_size for record in self.files)

    @property
    def metadata_checked(self) -> bool:
        return all({name for name, size in record.streams} == {name for name, digest in record.stream_hashes}
                   for record in self.files)

    @property
    def differing_streams(self) -> tuple[str, ...]:
        names = {name for record in self.files for name, size in record.streams}
        return tuple(sorted(name for name in names if len({
            (dict(record.streams).get(name), dict(record.stream_hashes).get(name))
            for record in self.files}) > 1))

    @property
    def metadata_status(self) -> str:
        if not self.contents_verified:
            if self.digest is not None:
                return "Hash match — bytes not verified"
            return "Possible match — contents not verified"
        if not self.metadata_checked:
            return "Metadata not checked — rescan"
        return "Metadata differs" if self.differing_streams else "Exact match"

    @property
    def metadata_details(self) -> str:
        names = sorted({name for record in self.files for name, size in record.streams})
        differences = set(self.differing_streams)
        lines = [self.metadata_status, "Main file contents match byte for byte." if self.contents_verified
                 else "SHA-256 hashes match. Main file bytes have not been compared." if self.digest is not None
                 else "Matched search criteria only. Contents must match before recycling."]
        if not names:
            lines.append("No extra NTFS streams.")
            return "\n".join(lines)
        for record in self.files:
            lines.extend(("", str(record.path)))
            sizes, hashes = dict(record.streams), dict(record.stream_hashes)
            for name in names:
                if name not in sizes:
                    detail = "not present"
                elif name not in hashes:
                    detail = f"{sizes[name]:,} bytes; contents not checked"
                else:
                    status = "differs or is missing in other copies" if name in differences else "matches every copy"
                    detail = f"{sizes[name]:,} bytes; {status}"
                lines.append(f"  {name[1:-6]}: {detail}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Issue:
    path: Path
    reason: str


@dataclass
class ScanResult:
    groups: list[DuplicateGroup] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    file_count: int = 0
    total_bytes: int = 0
    cancelled: bool = False


@dataclass(frozen=True)
class Progress:
    stage: str
    completed: int
    total: int
    path: str = ""
    bytes_read: int = 0


@dataclass
class RecycleResult:
    recycled: list[Path] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    cancelled: bool = False


class Cancelled(Exception):
    pass


def format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:,.0f} {unit}" if unit == "B" else f"{amount:,.2f} {unit}"
        amount /= 1024
    raise AssertionError("Unreachable")
