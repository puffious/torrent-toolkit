"""Runs one announce loop per active spoof; state lives in SQLite."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable

import httpx

from torrent_toolkit.db import Database, Spoof, SpoofStatus
from torrent_toolkit.notify import Notifier
from torrent_toolkit.spoof.announcer import AnnounceError, announce
from torrent_toolkit.spoof.emulation import Identity, build_query, round_uploaded

log = logging.getLogger(__name__)

SYNC_INTERVAL = 5.0
RETRY_MIN = 30
RETRY_MAX = 900
GIVE_UP_AFTER = 24 * 3600
STOP_TIMEOUT = 10.0


class SpoofManager:
    def __init__(
        self,
        db: Database,
        notifier: Notifier,
        *,
        target_ratio: float,
        max_duration: int,
        jitter: float,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ):
        self.db = db
        self.notifier = notifier
        self.target_ratio = target_ratio
        self.max_duration = max_duration
        self.jitter = jitter
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        if client is None:
            client = httpx.AsyncClient(timeout=30, follow_redirects=True)
            client.headers.clear()  # send only the emulated client's headers
        self.client = client
        self._tasks: dict[str, asyncio.Task] = {}
        self._identities: dict[tuple[str, int], Identity] = {}
        self._rates: dict[str, float] = {}  # current bytes/s incl. jitter
        self._last_success: dict[str, float] = {}
        # Announced counters are per session (since `event=started`), as in real clients;
        # the all-time total stored in the DB is `session base + session uploaded`.
        self._base: dict[str, int] = {}
        self._session_up: dict[str, int] = {}

    # --- lifecycle ---

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.sync()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=SYNC_INTERVAL)
        await self.shutdown()

    def sync(self) -> None:
        """Start loops for active spoofs that are not running (new hand-offs, restarts)."""
        for s in self.db.list_spoofs(SpoofStatus.ACTIVE):
            task = self._tasks.get(s.hash)
            if task is None or task.done():
                self._tasks[s.hash] = asyncio.create_task(
                    self._loop(s.hash), name=f"spoof-{s.hash}"
                )

    async def shutdown(self) -> None:
        """Stop announcing everything; spoofs stay active and resume on the next start."""
        active = [h for h, t in self._tasks.items() if not t.done()]
        for h in active:
            self._tasks[h].cancel()
        await asyncio.gather(*(self._tasks[h] for h in active), return_exceptions=True)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(self._send_stopped(h) for h in active), return_exceptions=True),
                timeout=STOP_TIMEOUT,
            )
        self._tasks.clear()
        await self.client.aclose()

    # --- control (HTTP API) ---

    async def stop(self, hash: str, reason: str = "stopped via API") -> bool:
        s = self.db.get_spoof(hash)
        if s is None or s.status != SpoofStatus.ACTIVE:
            return False
        self.db.update_spoof(hash, status=SpoofStatus.STOPPED, stop_reason=reason)
        task = self._tasks.pop(hash, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._send_stopped(hash)
        log.info("spoof stopped", extra={"hash": hash, "reason": reason})
        return True

    def update(self, hash: str, *, speed: int | None = None, forever: bool | None = None) -> bool:
        s = self.db.get_spoof(hash)
        if s is None:
            return False
        fields: dict = {}
        if speed is not None:
            fields["speed"] = speed
            self._rates[hash] = self._roll_rate(speed)
        if forever is not None:
            fields["forever"] = forever
        if fields:
            self.db.update_spoof(hash, **fields)
        return True

    # --- announce loop ---

    def _identity(self, s: Spoof) -> Identity:
        """One peer id per client version + port for the whole process, like a real session."""
        key = (s.client_version, s.port)
        if key not in self._identities:
            self._identities[key] = Identity.generate(s.client_version, s.port)
        return self._identities[key]

    def _roll_rate(self, speed: int) -> float:
        if speed <= 0:
            return 0.0
        return speed * (1 + self.rng.uniform(-self.jitter, self.jitter))

    def _next_session_uploaded(self, s: Spoof, now: float) -> int:
        """Session `uploaded` to report now: ratio-spoof's formula, with elapsed real time."""
        current = self._session_up.get(s.hash, 0)
        last = self._last_success.get(s.hash)
        rate = self._rates.get(s.hash, 0.0)
        if last is None or rate <= 0:
            return current
        grown = rate * max(now - last, 0) + s.piece_length * self.rng.randint(1, 9)
        return max(round_uploaded(current + int(grown)), current)

    def _should_stop(self, s: Spoof, now: float) -> str | None:
        if s.forever:
            return None
        if s.ratio >= self.target_ratio:
            return f"target ratio {self.target_ratio} reached"
        if now - s.started_at >= self.max_duration:
            return "maximum duration reached"
        return None

    async def _loop(self, hash: str) -> None:
        event: str | None = "started"
        delay = RETRY_MIN
        failing_since: float | None = None
        s = self.db.get_spoof(hash)
        self._rates[hash] = self._roll_rate(s.speed)
        self._base[hash] = s.uploaded
        self._session_up[hash] = 0
        self._last_success.pop(hash, None)
        log.info(
            "spoof announcing", extra={"hash": hash, "torrent": s.name, "uploaded": s.uploaded}
        )

        while True:
            s = self.db.get_spoof(hash)
            if s is None or s.status != SpoofStatus.ACTIVE:
                return
            now = self.clock()
            session_up = self._next_session_uploaded(s, now)
            # A seeder that downloaded nothing this session: downloaded=0, left=0.
            query = build_query(
                self._identity(s), bytes.fromhex(s.info_hash), session_up, 0, 0, event
            )
            try:
                resp, used = await announce(self.client, s.trackers, self._identity(s), query)
            except AnnounceError as e:
                first_failure = failing_since is None
                failing_since = failing_since or now
                self.db.update_spoof(hash, last_error=str(e))
                log.warning(
                    "announce failed", extra={"hash": hash, "error": str(e), "retry": delay}
                )
                if first_failure:
                    await self._notify("spoof announce failing", f"'{s.name}': {e}")
                if now - failing_since >= GIVE_UP_AFTER:
                    self.db.update_spoof(
                        hash, status=SpoofStatus.FAILED, stop_reason=f"tracker failing: {e}"
                    )
                    await self._notify(
                        "spoof failed", f"'{s.name}': tracker failing for 24h, giving up. {e}"
                    )
                    return
                await self.sleep(delay)
                delay = min(delay * 2, RETRY_MAX)
                continue

            event, delay, failing_since = None, RETRY_MIN, None
            self._last_success[hash] = now
            self._session_up[hash] = session_up
            uploaded = self._base[hash] + session_up
            trackers = s.trackers
            if used != trackers[0]:
                trackers = [used] + [t for t in trackers if t != used]
            self.db.update_spoof(
                hash,
                uploaded=uploaded,
                last_announce=now,
                interval=resp.interval,
                seeders=resp.seeders,
                leechers=resp.leechers,
                last_error=None,
                trackers=trackers,
            )
            self._rates[hash] = self._roll_rate(s.speed)
            log.debug("announced", extra={"hash": hash, "uploaded": uploaded})

            s = self.db.get_spoof(hash)
            if reason := self._should_stop(s, now):
                self.db.update_spoof(hash, status=SpoofStatus.FINISHED, stop_reason=reason)
                await self._send_stopped(hash)
                log.info("spoof finished", extra={"hash": hash, "reason": reason})
                await self._notify("spoof finished", f"'{s.name}': {reason} (ratio {s.ratio:.2f}).")
                return
            await self.sleep(resp.interval)

    async def _notify(self, title: str, body: str) -> None:
        await asyncio.to_thread(self.notifier.send, title, body)

    async def _send_stopped(self, hash: str) -> None:
        s = self.db.get_spoof(hash)
        if s is None or hash not in self._last_success:
            return  # never announced in this session: nothing to stop
        now = self.clock()
        session_up = self._next_session_uploaded(s, now)
        query = build_query(
            self._identity(s), bytes.fromhex(s.info_hash), session_up, 0, 0, "stopped", 0
        )
        try:
            await announce(self.client, s.trackers, self._identity(s), query)
            self.db.update_spoof(hash, uploaded=self._base[hash] + session_up, last_announce=now)
        except AnnounceError as e:
            log.warning("stopped announce failed", extra={"hash": hash, "error": str(e)})
        self._last_success.pop(hash, None)
