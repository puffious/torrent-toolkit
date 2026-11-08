"""Sort torrent files into video / subtitle / extra / junk / other."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath

VIDEO_EXT = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".wmv",
    ".ts",
    ".m2ts",
    ".webm",
    ".mpg",
    ".mpeg",
    ".flv",
    ".vob",
}
SUBTITLE_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}
JUNK_EXT = {
    ".nfo",
    ".txt",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".webp",
    ".exe",
    ".url",
    ".lnk",
    ".html",
    ".htm",
    ".md5",
    ".sfv",
    ".nzb",
    ".torrent",
    ".db",
    ".ds_store",
    ".log",
    ".bat",
    ".scr",
    ".com",
    ".website",
}

_SAMPLE = re.compile(r"(^|[\W_])sample([\W_]|$)", re.IGNORECASE)
_EXTRA_DIRS = re.compile(
    r"^(extras?|featurettes?|behind[ ._-]the[ ._-]scenes|deleted[ ._-]scenes|interviews?|"
    r"trailers?|bonus|making[ ._-]of|specials? features?|shorts|scenes)$",
    re.IGNORECASE,
)
_EXTRA_SUFFIX = re.compile(
    r"[-._ ](featurette|behindthescenes|deleted|interview|trailer|extra|making[-._ ]?of)$",
    re.IGNORECASE,
)


class FileClass(StrEnum):
    VIDEO = "video"
    SUBTITLE = "subtitle"
    EXTRA = "extra"
    JUNK = "junk"
    OTHER = "other"  # unknown type → review, never deleted


def classify(path: str) -> FileClass:
    """`path` is relative to the save path, e.g. `Movie.2010/Featurettes/Making of.mkv`."""
    p = PurePosixPath(path)
    ext = p.suffix.lower()
    name = p.name.lower()
    if name.startswith("._") or name in {"thumbs.db", ".ds_store"}:
        return FileClass.JUNK
    if ext in VIDEO_EXT:
        if any(_SAMPLE.search(part) for part in p.parts[-2:]):
            return FileClass.JUNK
        if any(_EXTRA_DIRS.match(part) for part in p.parts[:-1]) or _EXTRA_SUFFIX.search(p.stem):
            return FileClass.EXTRA
        return FileClass.VIDEO
    if ext in SUBTITLE_EXT:
        return FileClass.SUBTITLE
    if ext in JUNK_EXT:
        return FileClass.JUNK
    return FileClass.OTHER
