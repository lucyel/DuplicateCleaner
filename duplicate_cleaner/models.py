from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class FileRecord:
    path: Path
    size: int
    modified_ns: int
    changed_ns: int
    device: int
    inode: int


@dataclass(frozen=True)
class DuplicateGroup:
    files: tuple[FileRecord, ...]
    digest: str

    @property
    def extra_bytes(self) -> int:
        return (len(self.files) - 1) * self.files[0].size


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
