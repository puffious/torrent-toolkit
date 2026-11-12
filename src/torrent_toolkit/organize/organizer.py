"""Organize a torrent's files into the library: plan, move, record, report."""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from torrent_toolkit.arr import Arr
from torrent_toolkit.db import Database
from torrent_toolkit.organize.layout import MOVIES_DIR, SHOWS_DIR, Action, Finder, Planner
from torrent_toolkit.organize.mover import Collision, move_file, move_to_review, prune_empty_dirs

log = logging.getLogger(__name__)


class LibraryUnavailable(Exception):
    """The library folders are missing — most likely the rclone mount is down."""


@dataclass
class OrganizeResult:
    moved: int = 0
    duplicates: int = 0
    deleted: int = 0
    review: list[tuple[str, str]] = field(default_factory=list)  # (relative path, reason)


class Organizer:
    def __init__(
        self,
        db: Database,
        library: Path,
        review: Path,
        finder: Finder | None,
        arr: Arr | None = None,
    ):
        self.db = db
        self.library = library
        self.review = review
        self.arr = arr if arr is not None and arr.enabled else None
        self.planner = Planner(library, finder, self.arr)
        # Split offloads and normal organize jobs run in different threads; two torrents
        # with the same movie must not race on the same destination.
        self._lock = threading.Lock()

    def check_library(self) -> None:
        missing = [d for d in (MOVIES_DIR, SHOWS_DIR) if not (self.library / d).is_dir()]
        if len(missing) == 2:
            raise LibraryUnavailable(
                f"{self.library} has no '{MOVIES_DIR}/' or '{SHOWS_DIR}/' folder "
                "(is the rclone mount up?)"
            )

    def organize(
        self,
        hash: str,
        save_path: str | Sequence[str],
        all_files: dict[int, str],
        indexes: list[int],
    ) -> OrganizeResult:
        """Organize `indexes` out of `all_files` (index → relative path).

        `save_path` may list several folders (save path, incomplete folder); each file is
        taken from the first one that has it.

        All files are planned so subtitles and extras can be paired with videos that are
        in other split batches. Files already recorded as organized are skipped, so a rerun
        after a crash or failure is safe. Raises `LibraryUnavailable` or I/O / network errors;
        the caller retries later.
        """
        with self._lock:
            return self._organize(hash, save_path, all_files, indexes)

    def _organize(
        self,
        hash: str,
        save_path: str | Sequence[str],
        all_files: dict[int, str],
        indexes: list[int],
    ) -> OrganizeResult:
        self.check_library()
        plans = self.planner.plan(all_files)
        done = self.db.organized_indexes(hash)
        bases = [Path(save_path)] if isinstance(save_path, str) else [Path(p) for p in save_path]
        result = OrganizeResult()
        touched_bases: dict[Path, Path] = {}
        touched_dirs: set[Path] = set()
        moved_media: set[tuple[str, int]] = set()

        for i in sorted(indexes):
            if i in done:
                continue
            rel = all_files[i]
            base = next((b for b in bases if (b / rel).exists()), bases[0])
            src = base / rel
            plan = plans[i]

            if not src.exists():
                log.warning("file missing, skipped", extra={"hash": hash, "file": rel})
                self.db.record_organized(hash, i, str(src), None, "missing")
                continue

            if plan.action is Action.DELETE:
                src.unlink()
                result.deleted += 1
                self.db.record_organized(hash, i, str(src), None, "deleted")
            elif plan.action is Action.MOVE:
                try:
                    outcome = move_file(src, plan.dest)
                except Collision as e:
                    self._to_review(hash, i, rel, src, str(e), result)
                else:
                    if outcome == "duplicate":
                        result.duplicates += 1
                    else:
                        result.moved += 1
                        if plan.media:
                            moved_media.add(plan.media)
                    self.db.record_organized(hash, i, str(src), str(plan.dest), outcome)
            else:
                self._to_review(hash, i, rel, src, plan.reason or "", result)
            touched_dirs.add(src.parent)
            touched_bases[src.parent] = base

        for d in touched_dirs:
            prune_empty_dirs(d, touched_bases[d])
        if self.arr:
            for kind, tmdb_id in sorted(moved_media):
                self.arr.rescan(kind, tmdb_id)
        log.info(
            "organized",
            extra={
                "hash": hash,
                "moved": result.moved,
                "duplicates": result.duplicates,
                "deleted": result.deleted,
                "review": len(result.review),
            },
        )
        return result

    def _to_review(
        self,
        hash: str,
        index: int,
        rel: str,
        src: Path,
        reason: str,
        result: OrganizeResult,
    ) -> None:
        rel_path = Path(rel)
        # Multi-file torrents already start with their root folder; give single files one.
        target = rel_path if len(rel_path.parts) > 1 else Path(rel_path.stem) / rel_path
        dest = move_to_review(src, self.review / target)
        result.review.append((rel, reason))
        self.db.record_organized(hash, index, str(src), str(dest), "review", reason)
