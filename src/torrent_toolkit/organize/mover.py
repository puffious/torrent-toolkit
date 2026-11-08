"""Filesystem moves with collision rules. Library moves cross into the rclone mount."""

from __future__ import annotations

import errno
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

TMP_SUFFIX = ".tk-tmp"


class Collision(Exception):
    """Destination exists with a different size."""


def _transfer(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dest)  # same filesystem: instant
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    tmp = dest.with_name(dest.name + TMP_SUFFIX)
    try:
        shutil.copyfile(src, tmp)
        try:
            shutil.copystat(src, tmp)
        except OSError:
            pass  # FUSE mounts often reject times/permissions
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    src.unlink()


def move_file(src: Path, dest: Path) -> str:
    """Move `src` to `dest`. Returns "moved" or "duplicate"; raises `Collision`."""
    if dest.exists():
        if dest.stat().st_size == src.stat().st_size:
            src.unlink()
            return "duplicate"
        raise Collision(f"{dest} exists with a different size")
    _transfer(src, dest)
    return "moved"


def move_to_review(src: Path, dest: Path) -> Path:
    """Like `move_file`, but never fails on collisions: picks `name.1.ext`, `name.2.ext`, ..."""
    candidate = dest
    n = 0
    while candidate.exists():
        if candidate.stat().st_size == src.stat().st_size:
            src.unlink()
            return candidate
        n += 1
        candidate = dest.with_name(f"{dest.stem}.{n}{dest.suffix}")
    _transfer(src, candidate)
    return candidate


def prune_empty_dirs(start: Path, stop: Path) -> None:
    """Remove empty directories from `start` upwards, never touching `stop` or above."""
    stop = stop.resolve()
    d = start.resolve()
    while d != stop and stop in d.parents:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent
