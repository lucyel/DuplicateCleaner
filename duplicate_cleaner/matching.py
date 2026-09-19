"""Filename, date, location, and tolerance rules used before content comparison."""

import os
import re
from datetime import datetime

from .files import UnsafeFile


def text_key(value, criteria):
    return value if criteria.case_sensitive else value.casefold()


def filename_key(path, criteria):
    name = path.stem
    if criteria.ignore_copy:
        flags = 0 if criteria.case_sensitive else re.IGNORECASE
        # Strip common copy prefixes/suffixes, never substrings such as "copyright".
        name = re.sub(r"^Copy(?:\s+of)?[\s_-]+", "", name, flags=flags)
        name = re.sub(r"[\s_-]+Copy(?:\s*\(\d+\))?$", "", name, flags=flags)
    return text_key(name + path.suffix, criteria)


def date_key(timestamp_ns, date_only):
    return datetime.fromtimestamp(timestamp_ns / 1_000_000_000).date() if date_only else timestamp_ns


def creation_time(info):
    if hasattr(info, "st_birthtime_ns"):
        return info.st_birthtime_ns
    if os.name == "nt":
        return info.st_ctime_ns  # Python 3.10/3.11 Windows creation timestamp.
    raise UnsafeFile("Creation time is unavailable on this filesystem")


def folder_key(path, root, criteria):
    folder = path.parent
    if criteria.from_search_root:
        parts = folder.relative_to(root).parts
    else:
        parts = folder.parts[1:]  # Depth one is the first folder below the drive/share.
    if criteria.folder_depth_enabled:
        # Shallower folders must not accidentally match a complete depth prefix.
        value = (len(parts) >= criteria.folder_depth, parts[:criteria.folder_depth])
        return value[0], tuple(text_key(part, criteria) for part in value[1])
    if criteria.from_search_root:
        return tuple(text_key(part, criteria) for part in parts)
    return text_key(str(folder) if criteria.full_folder else folder.name, criteria)


def candidate_key(record, root, info, criteria):
    exact_size = criteria.contents or criteria.hashes or (criteria.size and criteria.size_tolerance == 0)
    return (record.size if exact_size else None,
            filename_key(record.path, criteria) if criteria.filename else None,
            text_key(record.path.suffix, criteria) if criteria.extension else None,
            date_key(record.modified_ns, criteria.modified_date_only) if criteria.modified else None,
            date_key(creation_time(info), criteria.created_date_only) if criteria.created else None,
            record.device if criteria.same_drive else None,
            folder_key(record.path, root, criteria) if criteria.folder else None)


def similar_text(left, right, tolerance):
    """Bounded Levenshtein distance (insertions, deletions, substitutions)."""
    if left == right:
        return True
    if abs(len(left) - len(right)) > tolerance:
        return False
    previous = {j: j for j in range(min(len(right), tolerance) + 1)}
    for i, first in enumerate(left, 1):
        current = {0: i} if i <= tolerance else {}
        for j in range(max(1, i - tolerance), min(len(right), i + tolerance) + 1):
            current[j] = min(previous.get(j, tolerance + 1) + 1,
                             current.get(j - 1, tolerance + 1) + 1,
                             previous.get(j - 1, tolerance + 1) + (first != right[j - 1]))
        if not current or min(current.values()) > tolerance:
            return False
        previous = current
    return previous.get(len(right), tolerance + 1) <= tolerance


def compatible(left, right, criteria):
    if criteria.size and abs(left.size - right.size) > criteria.size_tolerance:
        return False
    return not criteria.similar_names or similar_text(
        filename_key(left.path, criteria), filename_key(right.path, criteria), criteria.text_tolerance)
