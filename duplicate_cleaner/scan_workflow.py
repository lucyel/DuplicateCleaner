"""Run selected read-only scans with one immutable scope and cancellation event."""

from dataclasses import dataclass, field, replace
from threading import Event

from .empty_folders import scan_empty_folders
from .file_types import validate_file_types
from .models import Cancelled, SearchCriteria
from .scanner import scan
from .similarity import PRESETS, scan_similar

SCAN_MODES = {"duplicates": "Duplicate files", "similarity": "Similar files", "empty": "Empty folders"}


@dataclass
class ScanRunResult:
    results: dict = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    cancelled: bool = False


def run_selected_scans(roots, recursive=True, *, modes=("duplicates",), excluded_folders=(),
                       file_types=None, criteria=SearchCriteria(), preset="Balanced", cancel=None, progress=None):
    modes = tuple(dict.fromkeys(modes))
    if not modes or set(modes) - SCAN_MODES.keys():
        raise ValueError("Choose at least one valid scan mode")
    file_types = validate_file_types(file_types)
    if any(mode != "empty" for mode in modes) and file_types == frozenset():
        raise ValueError("Choose at least one file type or All file types")
    if "duplicates" in modes and not criteria.enabled:
        raise ValueError("Choose at least one duplicate comparison criterion")
    if "similarity" in modes and preset not in PRESETS:
        raise ValueError("Choose a valid similarity preset")
    roots, exclusions = tuple(roots), tuple(excluded_folders)
    cancel = cancel if cancel is not None else Event()
    outcome = ScanRunResult()
    for index, mode in enumerate(modes, 1):
        if cancel.is_set():
            break

        def report(value):
            if progress:
                progress(replace(value, stage=f"{SCAN_MODES[mode]} ({index}/{len(modes)}) · {value.stage}"))

        options = dict(excluded_folders=exclusions, cancel=cancel, progress=report)
        try:
            if mode == "duplicates":
                result = scan(roots, recursive, criteria=criteria, file_types=file_types, **options)
            elif mode == "similarity":
                result = scan_similar(roots, recursive, preset=preset, media_kind="All", file_types=file_types, **options)
            else:
                # Emptiness always examines every entry, independent of the chosen file types.
                result = scan_empty_folders(roots, recursive, **options)
            outcome.results[mode] = result
            if result.cancelled:
                cancel.set()
        except Cancelled:
            cancel.set()
        except Exception as exc:
            # Each scanner is a job boundary. Report failure and allow other selected modes to finish.
            outcome.errors[mode] = f"{type(exc).__name__}: {exc}"
    outcome.cancelled = cancel.is_set()
    return outcome
