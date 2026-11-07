import asyncio

import pytest

from torrent_toolkit.daemon import TAG_SERVICE, Daemon
from torrent_toolkit.db import Database, TorrentState
from torrent_toolkit.qbit import PRIORITY_SKIP, QbitError


@pytest.fixture
def daemon(db, qbit, notifier):
    return Daemon(db, qbit, notifier)


def test_untagged_torrents_ignored(daemon, qbit, db):
    qbit.add("a", files=[("x.mkv", 10)])
    daemon.tick()
    assert db.get_torrent("a") is None
    assert qbit.calls == []


def test_organize_takeover_starts_paused_torrent(daemon, qbit, db):
    qbit.add("a", tags={"organize"}, files=[("x.mkv", 10)])
    daemon.tick()
    tracked = db.get_torrent("a")
    assert tracked.state == TorrentState.ACTIVE
    assert tracked.features == {"organize"}
    assert TAG_SERVICE in qbit.torrents["a"].info.tags
    assert ("start", "a") in qbit.calls


def test_running_torrent_not_restarted_after_user_pause(daemon, qbit):
    qbit.add("a", tags={"organize"}, files=[("x.mkv", 10)])
    daemon.tick()
    qbit.stop("a")  # user pauses it
    qbit.calls.clear()
    daemon.tick()
    assert ("start", "a") not in qbit.calls


def test_split_takeover_skips_all_files_and_does_not_start(daemon, qbit, notifier):
    qbit.add("a", tags={"split"}, files=[("1.mkv", 10), ("2.mkv", 20)])
    daemon.tick()
    assert qbit.priorities("a") == [PRIORITY_SKIP, PRIORITY_SKIP]
    assert ("start", "a") not in qbit.calls
    assert notifier.sent == []


def test_split_found_running_is_notified(daemon, qbit, notifier):
    qbit.add("a", tags={"split"}, state="downloading", files=[("1.mkv", 10)])
    daemon.tick()
    assert qbit.priorities("a") == [PRIORITY_SKIP]
    assert notifier.sent and notifier.sent[0][0] == "split torrent was running"


def test_magnet_waits_for_metadata(daemon, qbit, db):
    qbit.add("m", name="m-hash", tags={"organize"}, state="metaDL", has_metadata=False)
    daemon.tick()
    assert db.get_torrent("m").state == TorrentState.METADATA
    assert TAG_SERVICE in qbit.torrents["m"].info.tags
    assert ("start", "m") not in qbit.calls

    qbit.update("m", name="Real.Name", state="stoppedDL", has_metadata=True, size=10)
    daemon.tick()
    tracked = db.get_torrent("m")
    assert tracked.state == TorrentState.ACTIVE
    assert tracked.name == "Real.Name"
    assert ("start", "m") in qbit.calls


def test_unknown_service_tag_is_orphaned_once(daemon, qbit, db, notifier):
    qbit.add("a", tags={"organize", TAG_SERVICE}, files=[("x.mkv", 10)])
    daemon.tick()
    daemon.tick()
    assert db.get_torrent("a").state == TorrentState.ORPHAN
    assert len(notifier.sent) == 1
    assert qbit.calls == []


def test_orphan_taken_over_after_service_tag_removed(daemon, qbit, db):
    qbit.add("a", tags={"organize", TAG_SERVICE}, files=[("x.mkv", 10)])
    daemon.tick()
    qbit.remove_tags("a", TAG_SERVICE)
    daemon.tick()
    assert db.get_torrent("a").state == TorrentState.ACTIVE
    assert TAG_SERVICE in qbit.torrents["a"].info.tags


def test_vanished_torrent_marked_gone(daemon, qbit, db):
    qbit.add("a", tags={"organize"}, files=[("x.mkv", 10)])
    daemon.tick()
    del qbit.torrents["a"]
    daemon.tick()
    assert db.get_torrent("a").state == TorrentState.GONE


def test_state_survives_restart(tmp_path, qbit, notifier):
    db1 = Database(tmp_path / "t.db")
    qbit.add("a", tags={"organize"}, files=[("x.mkv", 10)])
    Daemon(db1, qbit, notifier).tick()
    db1.close()

    db2 = Database(tmp_path / "t.db")
    qbit.calls.clear()
    Daemon(db2, qbit, notifier).tick()
    assert db2.get_torrent("a").state == TorrentState.ACTIVE
    assert notifier.sent == []  # not treated as orphan
    assert ("start", "a") not in qbit.calls


async def test_run_loop_survives_qbit_errors(daemon, qbit, monkeypatch):
    monkeypatch.setattr("torrent_toolkit.daemon.POLL_INTERVAL", 0.01)
    qbit.fail_next_poll = QbitError("connection refused")
    qbit.add("a", tags={"organize"}, files=[("x.mkv", 10)])
    stop = asyncio.Event()
    task = asyncio.create_task(daemon.run(stop))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if daemon.db.get_torrent("a"):
            break
    stop.set()
    await task
    assert daemon.db.get_torrent("a").state == TorrentState.ACTIVE
