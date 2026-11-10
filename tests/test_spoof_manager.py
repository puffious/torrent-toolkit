import asyncio
import random
from urllib.parse import parse_qs

import httpx
import pytest

from torrent_toolkit.db import Database, Spoof, SpoofStatus
from torrent_toolkit.spoof.bencode import encode
from torrent_toolkit.spoof.manager import GIVE_UP_AFTER, SpoofManager

MB = 1024**2
SIZE = 1000 * MB
PIECE = 4 * MB
HASH = "b1680a55cfc8693c6c02de732dd17c33e251e8e5"


class FakeTime:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


class FakeTracker:
    def __init__(self, interval=1800):
        self.interval = interval
        self.requests: list[dict] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        q = {
            k: v[0] for k, v in parse_qs(request.url.query.decode(), keep_blank_values=True).items()
        }
        q["_ua"] = request.headers.get("user-agent")
        self.requests.append(q)
        if self.fail:
            return httpx.Response(200, content=encode({"failure reason": "tracker down"}))
        return httpx.Response(
            200,
            content=encode(
                {"interval": self.interval, "complete": 3, "incomplete": 1, "peers": b""}
            ),
        )


def make_spoof(**overrides) -> Spoof:
    values = dict(
        hash=HASH,
        name="Debian",
        torrent_path="/config/torrents/x.torrent",
        info_hash=HASH,
        trackers=["http://tracker/announce?passkey=abc"],
        size=SIZE,
        piece_length=PIECE,
        uploaded=100 * MB,
        speed=1 * MB,
        forever=False,
        port=51413,
        client_version="5.0.2",
        status=SpoofStatus.ACTIVE,
        stop_reason=None,
        started_at=1_000_000.0,
        last_announce=None,
        interval=None,
        seeders=None,
        leechers=None,
        last_error=None,
    )
    values.update(overrides)
    return Spoof(**values)


@pytest.fixture
def ft():
    return FakeTime()


@pytest.fixture
def tracker():
    return FakeTracker()


@pytest.fixture
def make_manager(db, notifier, ft, tracker):
    def make(database=None, **kw):
        client = httpx.AsyncClient(transport=httpx.MockTransport(tracker.handler))
        client.headers.clear()
        opts = dict(target_ratio=2.0, max_duration=14 * 86400, jitter=0.4)
        opts.update(kw)
        return SpoofManager(
            database or db,
            notifier,
            client=client,
            clock=ft,
            sleep=ft.sleep,
            rng=random.Random(1),
            **opts,
        )

    return make


async def run_until(manager, predicate, limit=500):
    manager.sync()
    for _ in range(limit):
        await asyncio.sleep(0)
        if predicate():
            break
    else:
        raise AssertionError("condition not reached")
    for task in list(manager._tasks.values()):
        task.cancel()
    await asyncio.gather(*manager._tasks.values(), return_exceptions=True)
    manager._tasks.clear()


async def test_first_and_regular_announces(make_manager, db, tracker):
    db.add_spoof(make_spoof())
    m = make_manager()
    await run_until(m, lambda: len(tracker.requests) >= 3)
    first, second, third = tracker.requests[:3]
    assert first["event"] == "started"
    assert first["uploaded"] == str(100 * MB)
    assert first["downloaded"] == str(SIZE) and first["left"] == "0"
    assert first["port"] == "51413" and first["passkey"] == "abc"
    assert first["peer_id"].startswith("-qB5020-") and first["_ua"] == "qBittorrent/5.0.2"
    assert "event" not in second
    assert second["peer_id"] == first["peer_id"] and second["key"] == first["key"]

    grown = int(second["uploaded"]) - int(first["uploaded"])
    # 1 MiB/s ±40% over 1800 s, plus 1-9 pieces
    assert 0.6 * MB * 1800 <= grown <= 1.4 * MB * 1800 + 9 * PIECE
    assert int(second["uploaded"]) % (16 * 1024) == 0
    assert int(third["uploaded"]) > int(second["uploaded"])
    s = db.get_spoof(HASH)
    assert s.uploaded == int(tracker.requests[-1]["uploaded"])
    assert (s.interval, s.seeders, s.leechers) == (1800, 3, 1)


async def test_stops_at_target_ratio(make_manager, db, tracker, notifier):
    db.add_spoof(make_spoof(uploaded=int(1.99 * SIZE)))
    m = make_manager()
    await run_until(m, lambda: db.get_spoof(HASH).status != SpoofStatus.ACTIVE)
    s = db.get_spoof(HASH)
    assert s.status == SpoofStatus.FINISHED and "target ratio" in s.stop_reason
    assert tracker.requests[-1]["event"] == "stopped"
    assert tracker.requests[-1]["numwant"] == "0"
    assert notifier.sent[-1][0] == "spoof finished"


async def test_stops_at_max_duration(make_manager, db, tracker):
    db.add_spoof(make_spoof(speed=0))
    m = make_manager(max_duration=3 * 1800)
    await run_until(m, lambda: db.get_spoof(HASH).status != SpoofStatus.ACTIVE)
    assert db.get_spoof(HASH).stop_reason == "maximum duration reached"


async def test_forever_with_zero_speed_keeps_uploaded_constant(make_manager, db, tracker):
    db.add_spoof(make_spoof(speed=0, forever=True, uploaded=5 * MB))
    m = make_manager(max_duration=1)
    await run_until(m, lambda: len(tracker.requests) >= 50)
    assert {r["uploaded"] for r in tracker.requests} == {str(5 * MB)}
    assert db.get_spoof(HASH).status == SpoofStatus.ACTIVE


async def test_failures_retry_then_give_up(make_manager, db, tracker, notifier):
    tracker.fail = True
    db.add_spoof(make_spoof())
    m = make_manager()
    await run_until(m, lambda: db.get_spoof(HASH).status == SpoofStatus.FAILED, limit=2000)
    titles = [t for t, _ in notifier.sent]
    assert titles == ["spoof announce failing", "spoof failed"]
    assert len(tracker.requests) > 20  # backoff 30s → 900s over 24h
    assert len(tracker.requests) < GIVE_UP_AFTER / 900 + 20


async def test_recovers_after_failure(make_manager, db, tracker, ft):
    tracker.fail = True
    db.add_spoof(make_spoof())
    m = make_manager()
    await run_until(m, lambda: len(tracker.requests) >= 3)
    tracker.fail = False
    await run_until(m, lambda: db.get_spoof(HASH).last_announce is not None)
    assert db.get_spoof(HASH).last_error is None
    assert tracker.requests[-1]["event"] == "started"  # first successful announce


async def test_resume_after_restart(tmp_path, make_manager, tracker, notifier):
    path = tmp_path / "r.db"
    db1 = Database(path)
    db1.add_spoof(make_spoof(speed=1024))
    m1 = make_manager(db1)
    await run_until(m1, lambda: len(tracker.requests) >= 2)
    saved = db1.get_spoof(HASH).uploaded
    before = len(tracker.requests)
    db1.close()

    db2 = Database(path)
    m2 = make_manager(db2)
    await run_until(m2, lambda: len(tracker.requests) > before)
    resumed = tracker.requests[before]
    assert resumed["event"] == "started" and resumed["uploaded"] == str(saved)
    assert resumed["peer_id"] != tracker.requests[0]["peer_id"]  # new session


async def test_shutdown_sends_stopped_but_stays_active(make_manager, db, tracker):
    db.add_spoof(make_spoof())
    m = make_manager()
    m.sync()
    for _ in range(50):
        await asyncio.sleep(0)
        if tracker.requests:
            break
    await m.shutdown()
    assert tracker.requests[-1]["event"] == "stopped"
    assert db.get_spoof(HASH).status == SpoofStatus.ACTIVE


async def test_api_stop_and_update(make_manager, db, tracker):
    db.add_spoof(make_spoof())
    m = make_manager()
    m.sync()
    for _ in range(50):
        await asyncio.sleep(0)
        if tracker.requests:
            break
    assert m.update(HASH, speed=0, forever=True)
    assert db.get_spoof(HASH).forever and db.get_spoof(HASH).speed == 0
    assert await m.stop(HASH)
    assert tracker.requests[-1]["event"] == "stopped"
    assert db.get_spoof(HASH).status == SpoofStatus.STOPPED
    assert not await m.stop(HASH)
    assert not m.update("unknown", speed=1)
