"""Poll loop: takes over tagged torrents and drives their features."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from torrent_toolkit.config import ConfigError, Settings, parse_speed
from torrent_toolkit.db import Database, Spoof, SpoofStatus, TorrentState, TrackedTorrent
from torrent_toolkit.jobs import Jobs
from torrent_toolkit.notify import Notifier
from torrent_toolkit.organize.layout import Finder
from torrent_toolkit.organize.organizer import Organizer, OrganizeResult
from torrent_toolkit.organize.tmdb import Tmdb
from torrent_toolkit.qbit import PRIORITY_SKIP, FileInfo, QbitClient, QbitError, TorrentInfo
from torrent_toolkit.split.runner import SplitRunner, remove_finished, remove_parts_file, run_hook
from torrent_toolkit.spoof.announcer import http_trackers
from torrent_toolkit.spoof.bencode import BencodeError, parse_torrent

log = logging.getLogger(__name__)

POLL_INTERVAL = 10.0
MAX_BACKOFF = 300.0

TAG_SPLIT = "split"
TAG_ORGANIZE = "organize"
TAG_SPOOF = "spoof"
TAG_SERVICE = "service"
TAG_REVIEW = "tk-review"
TAG_SPOOF_SPEED = "spoof-speed:"
TAG_SPOOF_FOREVER = "spoof-forever"
FEATURE_TAGS = frozenset({TAG_SPLIT, TAG_ORGANIZE, TAG_SPOOF})

LIVE_STATES = (TorrentState.METADATA, TorrentState.ACTIVE)
ORGANIZE_RETRY_SECONDS = 600
REVIEW_LIST_LIMIT = 20


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
        organize_executor: ThreadPoolExecutor | None = None,
        finder: Finder | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.db = db
        self.qbit = qbit
        self.notifier = notifier
        self.settings = settings
        clock_kw = {"clock": clock} if clock else {}
        self.split = SplitRunner(
            db,
            qbit,
            notifier,
            reserve=settings.split_reserve,
            offload=self._split_offload,
            disk_usage=disk_usage,
            executor=split_executor,
            **clock_kw,
        )
        if finder is None and settings.tmdb_api_key:
            finder = Tmdb(settings.tmdb_api_key, db)
        self.organizer = Organizer(db, settings.library_path, settings.review_path, finder)
        self.organize_jobs = Jobs(
            "organize", ORGANIZE_RETRY_SECONDS, executor=organize_executor, **clock_kw
        )
        self._organize_failing: set[str] = set()
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
        self.organize_jobs.shutdown()

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
        self._activate_pending_spoofs(torrents)
        self._run_split([tr for tr in active if TAG_SPLIT in tr.features], torrents)
        for tracked in active:
            t = torrents[tracked.hash]
            if TAG_SPLIT in tracked.features:
                continue
            if TAG_SPOOF in tracked.features:
                if t.progress >= 1.0:
                    self._handoff(t, tracked, organize_after=TAG_ORGANIZE in tracked.features)
            elif TAG_ORGANIZE in tracked.features:
                self._run_organize(t, tracked)
        self._run_detached()

    # --- split ---

    def _run_split(self, active: list[TrackedTorrent], torrents: dict[str, TorrentInfo]) -> None:
        head = self.split.head(active)
        if head is not None:
            self.split.step(torrents[head.hash], head)
        for tracked in active:
            if self.split.finished(tracked.hash):
                self._split_finished(torrents[tracked.hash], tracked)

    def _split_offload(
        self,
        t: TorrentInfo,
        tracked: TrackedTorrent,
        batch: list[FileInfo],
        all_files: list[FileInfo],
    ) -> None:
        """Runs in the split offload thread."""
        if TAG_ORGANIZE in tracked.features:
            names = {f.index: f.name for f in all_files}
            result = self.organizer.organize(t.hash, t.save_path, names, [f.index for f in batch])
            self._report_review(t.hash, t.name, result)
        elif self.settings.split_hook:
            run_hook(self.settings.split_hook, t, batch)
        # else: the user frees the space; the runner waits for it.

    def _split_finished(self, t: TorrentInfo, tracked: TrackedTorrent) -> None:
        if TAG_SPOOF in tracked.features and self._handoff(t, tracked, organize_after=False):
            remove_parts_file(t)
            return
        remove_finished(self.qbit, t)
        self.db.set_torrent_state(t.hash, TorrentState.DONE)
        log.info("split torrent removed from qBittorrent", extra={"hash": t.hash})

    # --- spoof hand-off ---

    def _spoof_options(self, t: TorrentInfo) -> tuple[int, bool]:
        speed = self.settings.spoof_speed
        for tag in t.tags:
            if tag.startswith(TAG_SPOOF_SPEED):
                try:
                    speed = parse_speed(tag[len(TAG_SPOOF_SPEED) :])
                except ConfigError:
                    log.warning("invalid speed tag ignored", extra={"hash": t.hash, "tag": tag})
        return speed, TAG_SPOOF_FOREVER in t.tags

    def _handoff(self, t: TorrentInfo, tracked: TrackedTorrent, organize_after: bool) -> bool:
        """Move a finished torrent from qBittorrent to the spoofer. False if it failed."""
        try:
            data = self.qbit.export(t.hash)
            meta = parse_torrent(data)
        except (QbitError, BencodeError) as e:
            return self._handoff_failed(t, tracked, f"could not read the .torrent: {e}")
        if meta.info_hash_hex != t.hash.lower():
            return self._handoff_failed(t, tracked, "info hash mismatch (v2/hybrid torrent?)")
        trackers = http_trackers(self.qbit.trackers(t.hash)) or http_trackers(meta.trackers)
        if not trackers:
            return self._handoff_failed(t, tracked, "no HTTP(S) tracker")

        speed, forever = self._spoof_options(t)
        names: dict[int, str] = {}
        if organize_after:
            names = {
                f.index: f.name
                for f in self.qbit.files(t.hash)
                if f.priority > 0 and f.progress >= 1.0
            }
        path = self.settings.torrents_dir / f"{t.hash}.torrent"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

        # Recorded as pending first: if we crash before the delete, the hand-off is redone.
        self.db.add_spoof(
            Spoof(
                hash=t.hash,
                name=t.name,
                torrent_path=str(path),
                info_hash=meta.info_hash_hex,
                trackers=trackers,
                size=meta.total_size,
                piece_length=meta.piece_length,
                uploaded=t.uploaded,
                speed=speed,
                forever=forever,
                port=self.qbit.listen_port(),
                client_version=self.qbit.version(),
                status=SpoofStatus.PENDING,
                stop_reason=None,
                started_at=self._now(),
                last_announce=None,
                interval=None,
                seeders=None,
                leechers=None,
                last_error=None,
            )
        )
        self.qbit.delete(t.hash, delete_files=False)
        self.db.update_spoof(t.hash, status=SpoofStatus.ACTIVE)
        if organize_after:
            self.db.add_detached(t.hash, t.name, t.save_path, names)
            self.db.set_torrent_state(t.hash, TorrentState.DETACHED)
        else:
            self.db.set_torrent_state(t.hash, TorrentState.DONE)
        log.info(
            "handed off to spoofer",
            extra={"hash": t.hash, "torrent": t.name, "uploaded": t.uploaded, "speed": speed},
        )
        return True

    def _handoff_failed(self, t: TorrentInfo, tracked: TrackedTorrent, reason: str) -> bool:
        log.error("spoof hand-off failed", extra={"hash": t.hash, "error": reason})
        self.notifier.send(
            "spoof hand-off failed", f"'{t.name}': {reason}. It stays in qBittorrent, not spoofed."
        )
        remaining = tracked.features - {TAG_SPOOF}
        self.db.set_torrent_features(t.hash, remaining)
        if not remaining & {TAG_ORGANIZE, TAG_SPLIT}:
            self._release(t)
        return False

    def _activate_pending_spoofs(self, torrents: dict[str, TorrentInfo]) -> None:
        """A pending spoof whose torrent is already gone was removed just before a crash."""
        for s in self.db.list_spoofs(SpoofStatus.PENDING):
            if s.hash not in torrents:
                self.db.update_spoof(s.hash, status=SpoofStatus.ACTIVE)

    def _now(self) -> float:
        return self.split.clock()

    # --- organize ---

    def _run_organize(self, t: TorrentInfo, tracked: TrackedTorrent) -> None:
        """Organize a normal (non-split, non-spoof) torrent once it has finished."""
        finished, result, error = self.organize_jobs.collect(t.hash)
        if finished:
            if error is not None:
                self._organize_failed(t.hash, t.name, error)
            else:
                self._organize_failing.discard(t.hash)
                self._report_review(t.hash, t.name, result)
                self._release(t)
                return
        if self.organize_jobs.running(t.hash) or t.progress < 1.0:
            return
        files = self.qbit.files(t.hash)
        names = {f.index: f.name for f in files}
        wanted = [f.index for f in files if f.priority > 0 and f.progress >= 1.0]
        self.organize_jobs.submit(
            t.hash, self.organizer.organize, t.hash, t.save_path, names, wanted
        )

    def _run_detached(self) -> None:
        """Organize files of torrents that already left qBittorrent (spoof hand-off)."""
        for hash, name, save_path, files in self.db.list_detached():
            finished, result, error = self.organize_jobs.collect(hash)
            if finished:
                if error is not None:
                    self._organize_failed(hash, name, error)
                else:
                    self._organize_failing.discard(hash)
                    self._report_review(hash, name, result)
                    self.db.delete_detached(hash)
                    self.db.set_torrent_state(hash, TorrentState.DONE)
                    continue
            if not self.organize_jobs.running(hash):
                self.organize_jobs.submit(
                    hash, self.organizer.organize, hash, save_path, files, list(files)
                )

    def _organize_failed(self, hash: str, name: str, error: BaseException) -> None:
        log.error("organize failed", extra={"hash": hash, "error": str(error)})
        if hash not in self._organize_failing:
            self._organize_failing.add(hash)
            self.notifier.send(
                "organize failed",
                f"'{name}': {error}\nRetrying every {ORGANIZE_RETRY_SECONDS // 60} minutes.",
            )

    def _report_review(self, hash: str, name: str, result: OrganizeResult) -> None:
        if not result.review:
            return
        try:
            self.qbit.add_tags(hash, TAG_REVIEW)
        except QbitError as e:  # torrent may already be gone (spoof hand-off)
            log.warning("could not tag for review", extra={"hash": hash, "error": str(e)})
        lines = [f"- {path} ({reason})" for path, reason in result.review[:REVIEW_LIST_LIMIT]]
        if len(result.review) > REVIEW_LIST_LIMIT:
            lines.append(f"... and {len(result.review) - REVIEW_LIST_LIMIT} more")
        self.notifier.send(
            "files need review",
            f"'{name}': {len(result.review)} file(s) moved to {self.settings.review_path}:\n"
            + "\n".join(lines),
        )

    def _release(self, t: TorrentInfo) -> None:
        """Done with a torrent that stays in qBittorrent."""
        self.qbit.remove_tags(t.hash, TAG_SERVICE)
        self.db.set_torrent_state(t.hash, TorrentState.DONE)
        log.info("released", extra={"hash": t.hash, "torrent": t.name})

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
