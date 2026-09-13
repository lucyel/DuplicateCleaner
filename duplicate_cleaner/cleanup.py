import os
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from typing import Callable, Iterable

from .files import ensure_current, ensure_no_extra_streams, open_checked, UnsafeFile
from .models import Cancelled, DuplicateGroup, Issue, Progress, RecycleResult
from .scanner import compare_streams, Reporter
from .windows_trash import recycle_file


def recycle_selected(groups: Iterable[DuplicateGroup], selected: Iterable[Path], *,
                     cancel: Event | None = None,
                     progress: Callable[[Progress], None] | None = None,
                     recycler: Callable = recycle_file) -> RecycleResult:
    groups = tuple(groups)
    selected = {Path(os.path.abspath(path)) for path in selected}
    all_files = [record for group in groups for record in group.files]
    known = {record.path for record in all_files}
    identities = {(record.device, record.inode) for record in all_files}
    if len(known) != len(all_files) or len(identities) != len(all_files):
        raise ValueError("Duplicate file identities in the results; scan again")
    if selected - known:
        raise ValueError("The selection contains files outside the verified results")

    result = RecycleResult()
    reporter = Reporter(cancel if cancel is not None else Event(), progress)
    reporter.stage = "Rechecking and recycling"
    reporter.total = len(selected)
    try:
        reporter.emit(True)
        for group in groups:
            targets = [record for record in group.files if record.path in selected]
            if not targets:
                continue
            reference = next((record for record in group.files if record.path not in selected), targets[-1])
            recycle_reference = reference.path in selected
            reference_verified = False
            handled: set[Path] = set()
            try:
                # Block writes throughout verification. If every copy is selected,
                # the reference also permits recycling and is processed last.
                with open_checked(reference, allow_delete=recycle_reference) as reference_stream:
                    for record in targets:
                        reporter.check()
                        reporter.path = str(record.path)
                        try:
                            context = (nullcontext(reference_stream) if record is reference
                                       else open_checked(record, allow_delete=True))
                            with context as target_stream:
                                if record is reference:
                                    if not reference_verified:
                                        raise UnsafeFile("No matching copy could be reverified; scan again")
                                else:
                                    if not compare_streams(reference_stream, target_stream, reference.size, reporter):
                                        label = "comparison copy" if recycle_reference else "remaining copy"
                                        raise UnsafeFile(f"Contents no longer match the {label}; scan again")
                                    reference_verified = True

                                def revalidate():
                                    reporter.check()
                                    ensure_current(reference)
                                    ensure_current(record)
                                    ensure_no_extra_streams(reference.path)
                                    ensure_no_extra_streams(record.path)

                                revalidate()
                                recycler(record.path, revalidate)
                            result.recycled.append(record.path)
                        except OSError as exc:
                            result.issues.append(Issue(record.path, str(exc)))
                        handled.add(record.path)
                        reporter.completed += 1
                        reporter.emit(True)
            except OSError as exc:
                for record in targets:
                    if record.path not in handled:
                        label = "Reference copy" if recycle_reference else "Remaining copy"
                        result.issues.append(Issue(record.path, f"{label} cannot be verified: {exc}"))
                        handled.add(record.path)
                        reporter.completed += 1
                reporter.emit(True)
        reporter.path = ""
        reporter.emit(True)
    except Cancelled:
        result.cancelled = True
    return result
