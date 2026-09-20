"""Shared file categories for discovery and result display."""

from pathlib import Path

FILE_TYPES = {
    "Videos": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".mpg", ".mpeg",
               ".ts", ".mts", ".m2ts", ".flv", ".vob", ".3gp", ".ogv"},
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic",
               ".heif", ".avif", ".ico", ".svg", ".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".psd"},
    "Archives": {".zip", ".zipx", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".tgz",
                 ".tbz2", ".txz", ".zst", ".cab", ".iso"},
    "Documents": {".pdf", ".doc", ".docx", ".docm", ".odt", ".rtf", ".txt", ".md",
                  ".xls", ".xlsx", ".xlsm", ".ods", ".csv", ".ppt", ".pptx", ".pptm",
                  ".odp", ".epub", ".mobi"},
    "Audio": {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma", ".aiff",
              ".aif", ".alac", ".ape", ".mid", ".midi"},
}


def file_type(path: Path) -> str:
    extension = path.suffix.casefold()
    return next((name for name, extensions in FILE_TYPES.items() if extension in extensions), "Other")



def validate_file_types(selected):
    if selected is None:
        return None
    selected = frozenset(selected)
    if selected - (FILE_TYPES.keys() | {"Other"}):
        raise ValueError("Unknown file type category")
    return selected


def accepts_file(path, selected):
    return selected is None or file_type(path) in selected
