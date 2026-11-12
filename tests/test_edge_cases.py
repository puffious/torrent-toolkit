"""Edge cases found after the first release."""

import asyncio
import time
from dataclasses import replace

import pytest
from fakes import Clock, FakeDisk, InlineExecutor
from test_daemon_spoof import add_spoof_torrent, complete
from test_organizer import FINDER, make_files, tree
from test_spoof_manager import HASH, FakeTime, FakeTracker, make_spoof

from torrent_toolkit.daemon import TAG_SERVICE, Daemon
from torrent_toolkit.db import SpoofStatus, TorrentState
from torrent_toolkit.organize.layout import Planner
from torrent_toolkit.organize.tmdb import pick
from torrent_toolkit.qbit import PRIORITY_NORMAL
from torrent_toolkit.spoof.manager import MIN_ANNOUNCE_INTERVAL, SpoofManager

GB = 1024**3


@pytest.fixture
def paths(tmp_path):
    save, library = tmp_path / "downloads", tmp_path / "mount"
    (library / "movies").mkdir(parents=True)
    (library / "shows").mkdir()
    save.mkdir()
    return save, library, tmp_path / "review"


@pytest.fixture
def make_daemon(db, qbit, notifier, settings, paths):
    save, library, review = paths

    def make(disk=None, **overrides):
        s = replace(settings, library_path=library, review_path=review, **overrides)
        return Daemon(
            db,
            qbit,
            notifier,
            s,
            disk_usage=disk or FakeDisk(10**12, 10**12),
            split_executor=InlineExecutor(),
            organize_executor=InlineExecutor(),
            finder=FINDER,
            clock=Clock(),
        )

    return make


def finish(qbit, h):
    for f in qbit.torrents[h].files:
        qbit.set_file_progress(h, f.index, 1.0)
    qbit.update(h, progress=1.0, state="uploading")


# --- one broken torrent must not stall the others ---


def test_broken_torrent_isolated(make_daemon, qbit, db, paths, notifier):
    save, library, _ = paths

    def disk(path):
        if "missing" in path:
            raise FileNotFoundError(path)
        return 10**12, 10**12

    daemon = make_daemon(disk=disk)
    qbit.add("bad", tags={"split"}, files=[("a.mkv", 1)], save_path="/missing/mount")
    make_files(save, {"Heat.1995.mkv": 1})
    qbit.add("good", tags={"organize"}, files=[("Heat.1995.mkv", 1)], save_path=str(save))
    finish(qbit, "good")
    for _ in range(3):
        daemon.tick()
    assert (library / "movies/Heat (1995)/Heat.1995.mkv").exists()
    assert [t for t, _ in notifier.sent] == ["error"]  # once, not every poll
    assert "FileNotFoundError" in notifier.sent[0][1]


def test_torrent_deleted_mid_step_is_not_fatal(make_daemon, qbit, db):
    daemon = make_daemon()
    qbit.add("a", tags={"organize"}, files=[("x.mkv", 1)])
    real_files = qbit.files

    def gone(h):
        if h == "a":
            del qbit.torrents["a"]
        return real_files(h)

    qbit.files = gone
    qbit.add("b", tags={"organize"}, files=[("y.mkv", 1)])
    finish(qbit, "a")
    daemon.tick()  # "a" vanishes while being processed
    assert db.get_torrent("b").state == TorrentState.ACTIVE


# --- qBittorrent busy / incomplete folder ---


def test_waits_while_qbittorrent_moves_files(make_daemon, qbit, paths):
    save, library, _ = paths
    daemon = make_daemon()
    make_files(save, {"Heat.1995.mkv": 1})
    qbit.add("h", tags={"organize"}, files=[("Heat.1995.mkv", 1)], save_path=str(save))
    finish(qbit, "h")
    qbit.update("h", state="moving")
    daemon.tick()
    daemon.tick()
    assert not (library / "movies/Heat (1995)/Heat.1995.mkv").exists()
    qbit.update("h", state="uploading")
    daemon.tick()
    daemon.tick()
    assert (library / "movies/Heat (1995)/Heat.1995.mkv").exists()


def test_split_reads_from_incomplete_folder(make_daemon, qbit, paths, tmp_path):
    save, library, _ = paths
    incomplete = tmp_path / "incomplete"
    make_files(incomplete, {"Pack/Heat.1995.mkv": 1})
    seen = []

    def disk(path):
        seen.append(path)
        return 10**12, 10**12

    daemon = make_daemon(disk=disk)
    qbit.add(
        "h",
        tags={"split", "organize"},
        files=[("Pack/Heat.1995.mkv", 1)],
        save_path=str(save),
        download_path=str(incomplete),
    )
    daemon.tick()
    qbit.set_file_progress("h", 0, 1.0)
    daemon.tick()
    assert (library / "movies/Heat (1995)/Heat.1995.mkv").exists()
    assert set(seen) == {str(incomplete)}  # free space measured where data lands


# --- split ---


def test_split_with_data_already_present_needs_no_space(make_daemon, qbit, db, paths):
    save, library, _ = paths
    make_files(save, {"Heat.1995.mkv": 1, "Inception.2010.mkv": 1})
    disk = FakeDisk(total=100 * GB, free=1 * GB)  # less than the reserve
    daemon = make_daemon(disk=disk, split_reserve=2 * GB)
    qbit.add(
        "h",
        tags={"split", "organize"},
        save_path=str(save),
        progress=[1.0, 1.0],
        files=[("Heat.1995.mkv", 40 * GB), ("Inception.2010.mkv", 40 * GB)],
    )
    daemon.tick()  # cross-seed / pre-downloaded: everything fits at zero cost
    assert qbit.priorities("h") == [PRIORITY_NORMAL, PRIORITY_NORMAL]
    daemon.tick()
    assert len(tree(library)) == 2
    assert db.get_torrent("h").state == TorrentState.DONE


def test_split_partial_file_counts_only_missing_part(make_daemon, qbit, paths):
    disk = FakeDisk(total=100 * GB, free=6 * GB)
    daemon = make_daemon(disk=disk, split_reserve=1 * GB)
    qbit.add("h", tags={"split"}, files=[("a.mkv", 10 * GB)], progress=[0.6])
    daemon.tick()  # 4 GiB missing fits in 5 GiB capacity
    assert qbit.priorities("h") == [PRIORITY_NORMAL]


def test_split_where_nothing_fits_keeps_torrent(make_daemon, qbit, db, notifier):
    daemon = make_daemon(disk=FakeDisk(total=10 * GB, free=10 * GB))
    qbit.add("h", tags={"split"}, files=[("huge.mkv", 50 * GB)])
    daemon.tick()
    daemon.tick()
    assert "h" in qbit.torrents
    assert TAG_SERVICE not in qbit.torrents["h"].info.tags
    assert db.get_torrent("h").state == TorrentState.DONE
    assert "split did nothing" in [t for t, _ in notifier.sent]


def test_split_spoof_with_skipped_files_is_not_spoofed(make_daemon, qbit, db, paths, notifier):
    save, _, _ = paths
    daemon = make_daemon(disk=FakeDisk(total=10 * GB, free=10 * GB))
    h = add_spoof_torrent(
        qbit, save, {"split", "spoof"}, files=(("small.mkv", 10), ("huge.mkv", 10))
    )
    t = qbit.torrents[h]  # qBittorrent reports it huge; the file on disk stays tiny
    t.files[1] = replace(t.files[1], size=50 * GB)
    daemon.tick()
    qbit.set_file_progress(h, 0, 1.0)
    daemon.tick()
    assert db.get_spoof(h) is None
    assert "not spoofed" in [t for t, _ in notifier.sent]
    assert h not in qbit.torrents  # offloaded data: removed as usual


# --- re-added torrents ---


def test_readded_torrent_stops_active_spoof(make_daemon, qbit, db, paths, notifier):
    save, _, _ = paths
    daemon = make_daemon()
    h = add_spoof_torrent(qbit, save, {"spoof"})
    complete(qbit, h)
    daemon.tick()
    assert db.get_spoof(h).status == SpoofStatus.ACTIVE

    add_spoof_torrent(qbit, save, set())  # same torrent added back, no tags
    qbit.update(h, added_on=time.time() + 60)
    daemon.tick()
    s = db.get_spoof(h)
    assert s.status == SpoofStatus.STOPPED and "again" in s.stop_reason
    assert notifier.sent[-1][0] == "spoof stopped"


def test_readded_with_tags_starts_over(make_daemon, qbit, db, paths):
    save, library, _ = paths
    daemon = make_daemon()
    make_files(save, {"Heat.1995.mkv": 1})
    qbit.add("h", tags={"organize"}, files=[("Heat.1995.mkv", 1)], save_path=str(save))
    finish(qbit, "h")
    daemon.tick()
    daemon.tick()
    assert db.get_torrent("h").state == TorrentState.DONE

    daemon.tick()  # organized torrent stays tagged in qBittorrent: not taken over again
    assert db.get_torrent("h").state == TorrentState.DONE

    del qbit.torrents["h"]
    daemon.tick()
    make_files(save, {"Heat.1995.mkv": 1})
    qbit.add(
        "h",
        tags={"organize"},
        files=[("Heat.1995.mkv", 1)],
        save_path=str(save),
        added_on=time.time(),  # added after the toolkit's first record of it
    )
    finish(qbit, "h")
    daemon.tick()  # forgotten and taken over again
    assert TAG_SERVICE in qbit.torrents["h"].info.tags
    daemon.tick()
    daemon.tick()
    assert db.get_torrent("h").state == TorrentState.DONE


async def test_manager_halts_spoof_stopped_elsewhere(db, notifier):
    ft, tracker = FakeTime(), FakeTracker()
    import httpx

    client = httpx.AsyncClient(transport=httpx.MockTransport(tracker.handler))
    db.add_spoof(make_spoof(speed=0, forever=True))
    m = SpoofManager(
        db,
        notifier,
        target_ratio=2,
        max_duration=10**9,
        jitter=0,
        client=client,
        clock=ft,
        sleep=ft.sleep,
    )
    m.sync()
    for _ in range(20):
        await asyncio.sleep(0)
    db.update_spoof(HASH, status=SpoofStatus.STOPPED)
    m.sync()
    for _ in range(20):
        await asyncio.sleep(0)
    assert tracker.requests[-1]["event"] == "stopped"
    assert not m._tasks


async def test_tiny_tracker_interval_is_clamped(db, notifier):
    ft, tracker = FakeTime(), FakeTracker(interval=5)
    import httpx

    client = httpx.AsyncClient(transport=httpx.MockTransport(tracker.handler))
    db.add_spoof(make_spoof(speed=0, forever=True))
    m = SpoofManager(
        db,
        notifier,
        target_ratio=2,
        max_duration=10**9,
        jitter=0,
        client=client,
        clock=ft,
        sleep=ft.sleep,
    )
    start = ft.now
    m.sync()
    while len(tracker.requests) < 4:
        await asyncio.sleep(0)
    await m.shutdown()
    assert ft.now - start >= 3 * MIN_ANNOUNCE_INTERVAL
    assert db.get_spoof(HASH).interval == MIN_ANNOUNCE_INTERVAL


# --- identification ---


def test_show_pack_extras_go_to_show(paths, db):
    _, library, _ = paths
    plans = Planner(library, FINDER).plan(
        {
            0: "Breaking.Bad.S01.1080p/Breaking.Bad.S01E01.mkv",
            1: "Breaking.Bad.S01.1080p/Extras/Bloopers.mkv",
        }
    )
    assert plans[1].dest == library / "shows/Breaking Bad (2008)/extras/Bloopers.mkv"


def test_movie_pack_extras_use_nearest_folder(paths):
    _, library, _ = paths
    plans = Planner(library, FINDER).plan(
        {
            0: "Pack/Heat (1995)/Heat.1995.mkv",
            1: "Pack/Heat (1995)/Featurettes/Making of.mkv",
            2: "Pack/Inception (2010)/Inception.2010.mkv",
        }
    )
    assert plans[1].dest == library / "movies/Heat (1995)/extras/Making of.mkv"


def test_daily_show_goes_to_review(paths):
    _, library, _ = paths
    plans = Planner(library, FINDER).plan({0: "The.Office.2024.01.15.mkv"})
    assert plans[0].action == "review"


def test_subtitle_pairs_with_longest_stem(paths):
    _, library, _ = paths
    plans = Planner(library, FINDER).plan(
        {
            0: "Pack/Heat.1995.mkv",
            1: "Pack/Heat.1995.Extended.mkv",
            2: "Pack/Heat.1995.Extended.en.srt",
        }
    )
    assert plans[2].dest.name == "Heat.1995.Extended.en.srt"
    assert plans[2].dest.parent == plans[1].dest.parent


def test_country_hint_picks_us_show():
    results = [
        {
            "id": 2996,
            "name": "The Office",
            "first_air_date": "2001-07-09",
            "origin_country": ["GB"],
        },
        {
            "id": 2316,
            "name": "The Office",
            "first_air_date": "2005-03-24",
            "origin_country": ["US"],
        },
    ]
    assert pick("tv", "The Office", None, results).tmdb_id == 2996
    assert pick("tv", "The Office", None, results, country="US").tmdb_id == 2316
    assert pick("tv", "The Office", None, results, country="GB").tmdb_id == 2996
