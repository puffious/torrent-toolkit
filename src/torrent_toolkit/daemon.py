"""Poll loop: takes over tagged torrents and drives their features."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from torrent_toolkit.config import Settings
from torrent_toolkit.db import Database, TorrentState, TrackedTorrent
from torrent_toolkit.notify import Notifier
from torrent_toolkit.qbit import PRIORITY_SKIP, FileInfo, QbitClient, QbitError, TorrentInfo
from torrent_toolkit.split.runner import SplitRunner, remove_finished, run_hook

log = logging.getLogger(__name__)

POLL_INTERVAL = 10.0
MAX_BACKOFF = 300.0

TAG_SPLIT = "split"
TAG_ORGANIZE = "organize"
TAG_SPOOF = "spoof"
TAG_SERVICE = "service"
TAG_REVIEW = "tk-review"
FEATURE_TAGS = frozenset({TAG_SPLIT, TAG_ORGANIZE, TAG_SPOOF})

LIVE_STATES = (TorrentState.METADATA, TorrentState.ACTIVE)


class Daemon:
    def __init__(
        self,
        db: Database,
        qbit: QbitClient,
        notifier: Notifier,
        settings: Settings,
        *,
        disk_usage: Callable[[str], tuple[int, int]] | None = None,
        split_executor: ThreadPoolExecutor | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.db = db
        self.qbit = qbit
        self.notifier = notifier
        self.settings = settings
        self.split = SplitRunner(
            db,
            qbit,
            notifier,
            reserve=settings.split_reserve,
            offload=self._split_offload if settings.split_hook else None,
            disk_usage=disk_usage,
            executor=split_executor,
            **({"clock": clock} if clock else {}),
        )
        # One worker thread: ticks never overlap and blocking API calls stay off the event loop.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tick")

    # --- async loop ---

    async def run(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        delay = POLL_INTERVAL
        while not stop.is_set():
            try:
                await loop.run_in_executor(self._executor, self.tick)
                delay = POLL_INTERVAL
            except QbitError as e:
                log.warning("qBittorrent unavailable", extra={"error": str(e), "retry_in": delay})
                delay = min(delay * 2, MAX_BACKOFF)
            except Exception:
                log.exception("tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
        self._executor.shutdown(wait=True)
        self.split.shutdown()

    # --- one poll ---

    def tick(self) -> None:
        torrents = self.qbit.poll()
        self._mark_gone(torrents)
        for t in torrents.values():
            if not t.tags & FEATURE_TAGS and TAG_SERVICE not in t.tags:
                continue
            tracked = self.db.get_torrent(t.hash)
            if tracked is None or (
                tracked.state == TorrentState.ORPHAN and TAG_SERVICE not in t.tags
            ):
                tracked = self._takeover(t, known=tracked is not None)
            if tracked is None:
                continue
            if tracked.state == TorrentState.METADATA and t.has_metadata:
                self._activate(t, tracked)

        active = [tr for tr in self.db.list_torrents((TorrentState.ACTIVE,)) if tr.hash in torrents]
        self._run_split([tr for tr in active if TAG_SPLIT in tr.features], torrents)

    # --- split ---

    def _run_split(self, active: list[TrackedTorrent], torrents: dict[str, TorrentInfo]) -> None:
        head = self.split.head(active)
        if head is not None:
            self.split.step(torrents[head.hash], head)
        for tracked in active:
            if self.split.finished(tracked.hash):
                self._split_finished(torrents[tracked.hash], tracked)

    def _split_offload(self, t: TorrentInfo, tracked: TrackedTorrent, files: list[FileInfo]):
        run_hook(self.settings.split_hook, t, files)

    def _split_finished(self, t: TorrentInfo, tracked: TrackedTorrent) -> None:
        remove_finished(self.qbit, t)
        self.db.set_torrent_state(t.hash, TorrentState.DONE)
        log.info("split torrent removed from qBittorrent", extra={"hash": t.hash})

    # --- takeover ---

    def _takeover(self, t: TorrentInfo, known: bool) -> TrackedTorrent | None:
        if known:  # orphan whose `service` tag the user removed → start fresh
            self.db.delete_torrent(t.hash)
        elif TAG_SERVICE in t.tags:
            log.warning("torrent has service tag but no record", extra={"hash": t.hash})
            self.notifier.send(
                "unknown torrent",
                f"'{t.name}' carries the '{TAG_SERVICE}' tag but the toolkit has no record "
                f"of it. It is left alone; remove the '{TAG_SERVICE}' tag to let the toolkit "
                "take it over.",
            )
            self.db.add_torrent(t.hash, t.name, t.save_path, frozenset(), TorrentState.ORPHAN)
            return None

        features = t.tags & FEATURE_TAGS
        if not features:
            return None
        tracked = self.db.add_torrent(
            t.hash, t.name, t.save_path, frozenset(features), TorrentState.METADATA
        )
        self.qbit.add_tags(t.hash, TAG_SERVICE)
        log.info(
            "taken over", extra={"hash": t.hash, "torrent": t.name, "features": sorted(features)}
        )
        return tracked

    def _activate(self, t: TorrentInfo, tracked: TrackedTorrent) -> TrackedTorrent:
        """Metadata is available: prepare the torrent and hand it to its features."""
        if TAG_SPLIT in tracked.features:
            files = self.qbit.files(t.hash)
            self.qbit.set_file_priority(t.hash, [f.index for f in files], PRIORITY_SKIP)
            if not t.paused:
                self.notifier.send(
                    "split torrent was running",
                    f"'{t.name}' was added started; its files were set to 'do not download'. "
                    "Add split torrents paused to avoid partial downloads and pre-allocation.",
                )
            # The split runner enables the first batch and starts the torrent.
        elif t.paused:
            self.qbit.start(t.hash)

        self.db.update_torrent(t.hash, name=t.name, save_path=t.save_path)
        self.db.set_torrent_state(t.hash, TorrentState.ACTIVE)
        log.info("activated", extra={"hash": t.hash, "torrent": t.name})
        return self.db.get_torrent(t.hash)  # type: ignore[return-value]

    def _mark_gone(self, torrents: dict[str, TorrentInfo]) -> None:
        for tracked in self.db.list_torrents(LIVE_STATES):
            if tracked.hash not in torrents:
                log.warning(
                    "tracked torrent vanished from qBittorrent",
                    extra={"hash": tracked.hash, "torrent": tracked.name},
                )
                self.db.set_torrent_state(tracked.hash, TorrentState.GONE)
