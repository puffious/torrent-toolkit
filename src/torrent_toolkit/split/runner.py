"""Split state machine: download a torrent in batches that fit the disk.

Driven by `step()` once per poll for the split torrent at the head of the queue.
Offloading (organize / hook) runs in its own thread so a long move never blocks polling.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from torrent_toolkit.db import Database, SplitFileStatus, SplitPhase, TrackedTorrent
from torrent_toolkit.notify import Notifier
from torrent_toolkit.qbit import PRIORITY_NORMAL, PRIORITY_SKIP, FileInfo, QbitClient, TorrentInfo
from torrent_toolkit.split.planner import oversized, plan_batch

log = logging.getLogger(__name__)

SPACE_SETTLE_SECONDS = 300  # free space must stop growing this long before a partial batch
HOOK_RETRY_SECONDS = 600


class DiskUsage:
    """`(total, free)` bytes for a path; swapped out in tests."""

    def __call__(self, path: str) -> tuple[int, int]:
        usage = shutil.disk_usage(path)
        return usage.total, usage.free


# (torrent, tracked, completed batch files, all files) → None; raises on failure.
Offloader = Callable[[TorrentInfo, TrackedTorrent, list[FileInfo], list[FileInfo]], None]


def run_hook(command: str, t: TorrentInfo, files: list[FileInfo]) -> None:
    """Run `TK_SPLIT_HOOK` with the completed files (absolute paths, one per line)."""
    with tempfile.NamedTemporaryFile("w", prefix="tk-files-", suffix=".txt", delete=False) as fh:
        fh.writelines(str(Path(t.save_path) / f.name) + "\n" for f in files)
        list_path = fh.name
    try:
        env = {
            **os.environ,
            "TK_FILES": list_path,
            "TK_SAVE_PATH": t.save_path,
            "TK_TORRENT_NAME": t.name,
            "TK_TORRENT_HASH": t.hash,
        }
        result = subprocess.run(
            ["sh", "-c", command, "tk-hook", list_path],
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            output = (result.stderr or result.stdout).strip()[-500:]
            raise RuntimeError(f"hook exited {result.returncode}: {output}")
    finally:
        os.unlink(list_path)


class SplitRunner:
    def __init__(
        self,
        db: Database,
        qbit: QbitClient,
        notifier: Notifier,
        *,
        reserve: int,
        offload: Offloader,
        disk_usage: Callable[[str], tuple[int, int]] | None = None,
        clock: Callable[[], float] = time.time,
        executor: ThreadPoolExecutor | None = None,
    ):
        self.db = db
        self.qbit = qbit
        self.notifier = notifier
        self.reserve = reserve
        self.offload = offload
        self.disk_usage = disk_usage or DiskUsage()
        self.clock = clock
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="offload")
        self._offloads: dict[str, Future] = {}
        self._failing: set[str] = set()  # notify once per failure streak

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)

    # --- queue ---

    def head(self, active_split: list[TrackedTorrent]) -> TrackedTorrent | None:
        """First split torrent (oldest takeover) that is not finished: one runs at a time."""
        for tracked in active_split:
            state = self.db.get_split_state(tracked.hash)
            if state is None or state.phase != SplitPhase.FINISHED:
                return tracked
        return None

    def finished(self, hash: str) -> bool:
        state = self.db.get_split_state(hash)
        return state is not None and state.phase == SplitPhase.FINISHED

    # --- state machine ---

    def step(self, t: TorrentInfo, tracked: TrackedTorrent) -> None:
        state = self.db.get_split_state(t.hash)
        if state is None:
            self._init(t)
            state = self.db.get_split_state(t.hash)

        match state.phase:
            case SplitPhase.QUEUED:
                self._try_plan(t, settled=True)
            case SplitPhase.DOWNLOADING:
                self._check_batch(t, tracked)
            case SplitPhase.OFFLOADING:
                self._check_offload(t, tracked)
            case SplitPhase.WAITING_SPACE:
                self._wait_space(t)

    def _init(self, t: TorrentInfo) -> None:
        files = [(f.index, f.size) for f in self.qbit.files(t.hash)]
        total, _ = self.disk_usage(t.save_path)
        skipped = set(oversized(files, total - self.reserve))
        self.db.init_split(t.hash, files, skipped)
        log.info(
            "split initialised",
            extra={"hash": t.hash, "files": len(files), "skipped": len(skipped)},
        )
        if skipped:
            names = {f.index: f.name for f in self.qbit.files(t.hash)}
            listing = "\n".join(f"- {names[i]}" for i in sorted(skipped))
            self.notifier.send(
                "files too large for disk",
                f"'{t.name}': {len(skipped)} file(s) larger than the disk are skipped:\n{listing}",
            )

    def _capacity(self, t: TorrentInfo) -> tuple[int, int]:
        _, free = self.disk_usage(t.save_path)
        return free, free - self.reserve

    def _try_plan(self, t: TorrentInfo, settled: bool) -> None:
        pending = self.db.split_files(t.hash, SplitFileStatus.PENDING)
        if not pending:
            self._finish(t)
            return

        free, capacity = self._capacity(t)
        remaining = sum(f.size for f in pending)
        if capacity < remaining and not settled:
            return
        batch = plan_batch([(f.file_index, f.size) for f in pending], capacity)
        if not batch:
            state = self.db.get_split_state(t.hash)
            if settled and not state.stall_notified:
                smallest = min(f.size for f in pending)
                self.notifier.send(
                    "split waiting for space",
                    f"'{t.name}': next file needs {smallest / 1024**3:.1f} GiB but only "
                    f"{max(capacity, 0) / 1024**3:.1f} GiB is usable. Free up space to continue.",
                )
                self.db.update_split_state(t.hash, stall_notified=True)
            return

        batch_no = self.db.get_split_state(t.hash).batch_no + 1
        self.db.set_split_files_status(t.hash, batch, SplitFileStatus.BATCH, batch_no)
        self.qbit.set_file_priority(t.hash, batch, PRIORITY_NORMAL)
        if t.paused:
            self.qbit.start(t.hash)
        self.db.update_split_state(
            t.hash,
            phase=SplitPhase.DOWNLOADING,
            batch_no=batch_no,
            last_free=None,
            last_growth_at=None,
            stall_notified=False,
        )
        size = sum(f.size for f in pending if f.file_index in batch)
        log.info(
            "split batch started",
            extra={
                "hash": t.hash,
                "batch": batch_no,
                "files": len(batch),
                "bytes": size,
                "free": free,
                "left_files": len(pending) - len(batch),
            },
        )

    def _batch_files(
        self, t: TorrentInfo, all_files: list[FileInfo] | None = None
    ) -> list[FileInfo]:
        indexes = {f.file_index for f in self.db.split_files(t.hash, SplitFileStatus.BATCH)}
        return [f for f in all_files or self.qbit.files(t.hash) if f.index in indexes]

    def _check_batch(self, t: TorrentInfo, tracked: TrackedTorrent) -> None:
        files = self._batch_files(t)
        if files and all(f.progress >= 1.0 for f in files):
            state = self.db.get_split_state(t.hash)
            log.info("split batch downloaded", extra={"hash": t.hash, "batch": state.batch_no})
            self.db.update_split_state(t.hash, phase=SplitPhase.OFFLOADING, retry_at=None)
            self._check_offload(t, tracked)

    def _check_offload(self, t: TorrentInfo, tracked: TrackedTorrent) -> None:
        future = self._offloads.get(t.hash)
        if future is None:
            state = self.db.get_split_state(t.hash)
            if state.retry_at and self.clock() < state.retry_at:
                return
            all_files = self.qbit.files(t.hash)
            batch = self._batch_files(t, all_files)
            future = self._offloads[t.hash] = self._executor.submit(
                self.offload, t, tracked, batch, all_files
            )
        if not future.done():
            return

        del self._offloads[t.hash]
        if (error := future.exception()) is not None:
            log.error("split offload failed", extra={"hash": t.hash, "error": str(error)})
            if t.hash not in self._failing:
                self._failing.add(t.hash)
                self.notifier.send(
                    "split offload failed",
                    f"'{t.name}': {error}\nRetrying every {HOOK_RETRY_SECONDS // 60} minutes.",
                )
            self.db.update_split_state(t.hash, retry_at=self.clock() + HOOK_RETRY_SECONDS)
            return
        self._failing.discard(t.hash)
        self._offloaded(t)

    def _offloaded(self, t: TorrentInfo) -> None:
        batch = [f.file_index for f in self.db.split_files(t.hash, SplitFileStatus.BATCH)]
        self.qbit.set_file_priority(t.hash, batch, PRIORITY_SKIP)
        self.db.set_split_files_status(t.hash, batch, SplitFileStatus.DONE)
        free, _ = self._capacity(t)
        self.db.update_split_state(
            t.hash, phase=SplitPhase.WAITING_SPACE, last_free=free, last_growth_at=self.clock()
        )
        log.info("split batch offloaded", extra={"hash": t.hash, "files": len(batch)})
        self._wait_space(t)

    def _wait_space(self, t: TorrentInfo) -> None:
        state = self.db.get_split_state(t.hash)
        free, _ = self._capacity(t)
        now = self.clock()
        if state.last_free is None or free > state.last_free:
            self.db.update_split_state(t.hash, last_free=free, last_growth_at=now)
            state = self.db.get_split_state(t.hash)
        settled = now - (state.last_growth_at or now) >= SPACE_SETTLE_SECONDS
        self._try_plan(t, settled=settled)

    def _finish(self, t: TorrentInfo) -> None:
        self.db.update_split_state(t.hash, phase=SplitPhase.FINISHED)
        skipped = self.db.split_files(t.hash, SplitFileStatus.SKIPPED)
        log.info("split finished", extra={"hash": t.hash, "skipped": len(skipped)})
        self.notifier.send(
            "split finished",
            f"'{t.name}': all batches done"
            + (f" ({len(skipped)} oversized file(s) skipped)." if skipped else "."),
        )


def remove_finished(qbit: QbitClient, t: TorrentInfo) -> None:
    """Drop a fully offloaded split torrent from qBittorrent and clean its `.parts` file."""
    qbit.delete(t.hash, delete_files=False)
    parts = Path(t.save_path) / f".{t.hash}.parts"
    try:
        parts.unlink(missing_ok=True)
    except OSError as e:
        log.warning("could not delete parts file", extra={"path": parts, "error": str(e)})
