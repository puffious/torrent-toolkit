from concurrent.futures import Future
from dataclasses import replace

import pytest

from torrent_toolkit.daemon import Daemon
from torrent_toolkit.db import Database, SplitPhase, TorrentState
from torrent_toolkit.qbit import PRIORITY_NORMAL, PRIORITY_SKIP
from torrent_toolkit.split.runner import SPACE_SETTLE_SECONDS

GB = 1024**3


class FakeDisk:
    def __init__(self, total: int, free: int):
        self.total = total
        self.free = free

    def __call__(self, path: str) -> tuple[int, int]:
        return self.total, self.free


class InlineExecutor:
    """Runs offloads synchronously so tests are deterministic."""

    def submit(self, fn, *args):
        f = Future()
        try:
            f.set_result(fn(*args))
        except Exception as e:
            f.set_exception(e)
        return f

    def shutdown(self, wait=True):
        pass


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def disk():
    return FakeDisk(total=20 * GB, free=10 * GB)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_daemon(db, qbit, notifier, settings, disk, clock):
    def make(**overrides):
        s = replace(settings, split_reserve=1 * GB, **overrides)
        return Daemon(
            db, qbit, notifier, s, disk_usage=disk, split_executor=InlineExecutor(), clock=clock
        )

    return make


def complete(qbit, hash, indexes):
    for i in indexes:
        qbit.set_file_progress(hash, i, 1.0)


def normal(qbit, hash):
    return [i for i, p in enumerate(qbit.priorities(hash)) if p == PRIORITY_NORMAL]


FILES = [("a.mkv", 6 * GB), ("b.mkv", 5 * GB), ("c.mkv", 4 * GB), ("d.mkv", 3 * GB)]


def test_full_split_without_hook(make_daemon, qbit, db, disk, notifier):
    daemon = make_daemon()
    qbit.add("t", tags={"split"}, files=FILES)

    daemon.tick()  # capacity 9 GB → FFD picks 6 + 3
    assert normal(qbit, "t") == [0, 3]
    assert ("start", "t") in qbit.calls

    disk.free = 1 * GB  # downloading eats the space
    complete(qbit, "t", [0, 3])
    daemon.tick()  # batch done → offloaded (user frees space) → waiting
    assert qbit.priorities("t") == [PRIORITY_SKIP] * 4
    assert db.get_split_state("t").phase == SplitPhase.WAITING_SPACE

    disk.free = 10 * GB  # user moved the files away
    daemon.tick()  # remaining 9 GB fits capacity 9 GB → batch 2 right away
    assert normal(qbit, "t") == [1, 2]
    assert db.get_split_state("t").batch_no == 2

    complete(qbit, "t", [1, 2])
    daemon.tick()  # last batch offloaded → finished → removed
    assert ("delete", "t", False) in qbit.calls
    assert db.get_torrent("t").state == TorrentState.DONE
    assert notifier.sent[-1][0] == "split finished"


def test_partial_batch_waits_for_space_to_settle(make_daemon, qbit, db, disk, clock):
    daemon = make_daemon()
    qbit.add("t", tags={"split"}, files=FILES)
    daemon.tick()
    disk.free = 1 * GB
    complete(qbit, "t", [0, 3])
    daemon.tick()  # waiting: remaining 9 GB needs free = 10 GB

    disk.free = 6 * GB  # space trickling back (e.g. rclone uploading)
    daemon.tick()
    clock.now += SPACE_SETTLE_SECONDS - 1
    daemon.tick()
    assert db.get_split_state("t").phase == SplitPhase.WAITING_SPACE  # still growing recently

    disk.free = 7 * GB
    daemon.tick()  # grew → settle timer restarts
    clock.now += SPACE_SETTLE_SECONDS
    daemon.tick()  # settled at capacity 6 GB → only the 5 GB file fits
    assert normal(qbit, "t") == [1]


def test_stall_notified_once(make_daemon, qbit, disk, notifier, clock):
    disk.free = 2 * GB  # capacity 1 GB; smallest file 3 GB
    daemon = make_daemon()
    qbit.add("t", tags={"split"}, files=FILES)
    daemon.tick()
    daemon.tick()
    assert [t for t, _ in notifier.sent] == ["split waiting for space"]
    assert normal(qbit, "t") == []

    disk.free = 10 * GB
    daemon.tick()
    assert normal(qbit, "t") == [0, 3]


def test_oversized_files_skipped(make_daemon, qbit, db, disk, notifier):
    disk.total = 8 * GB  # usable 7 GB
    qbit.add("t", tags={"split"}, files=[("huge.mkv", 50 * GB), ("ok.mkv", 2 * GB)])
    daemon = make_daemon()
    daemon.tick()
    assert notifier.sent[0][0] == "files too large for disk"
    assert "huge.mkv" in notifier.sent[0][1]
    assert normal(qbit, "t") == [1]

    complete(qbit, "t", [1])
    daemon.tick()
    assert db.get_torrent("t").state == TorrentState.DONE


def test_hook_receives_completed_files(make_daemon, qbit, db, tmp_path):
    out = tmp_path / "hook-out.txt"
    daemon = make_daemon(split_hook=f'cat "$TK_FILES" > {out}; echo "$1" >> {out}')
    qbit.add("t", tags={"split"}, files=FILES, save_path="/data/dl")
    daemon.tick()
    complete(qbit, "t", [0, 3])
    daemon.tick()
    lines = out.read_text().splitlines()
    assert lines[:2] == ["/data/dl/a.mkv", "/data/dl/d.mkv"]
    assert lines[2].endswith(".txt")
    assert db.get_split_state("t").phase in (SplitPhase.WAITING_SPACE, SplitPhase.DOWNLOADING)


def test_failing_hook_retries_later(make_daemon, qbit, db, notifier, clock, tmp_path):
    flag = tmp_path / "ok"
    daemon = make_daemon(split_hook=f"test -f {flag} || {{ echo boom >&2; exit 3; }}")
    qbit.add("t", tags={"split"}, files=FILES)
    daemon.tick()
    complete(qbit, "t", [0, 3])
    daemon.tick()  # offload runs and fails
    assert notifier.sent[-1][0] == "split offload failed"
    assert "boom" in notifier.sent[-1][1]
    assert db.get_split_state("t").phase == SplitPhase.OFFLOADING

    flag.touch()
    daemon.tick()
    assert db.get_split_state("t").phase == SplitPhase.OFFLOADING  # retry not due yet
    clock.now += 601
    daemon.tick()  # retried, succeeds
    assert db.get_split_state("t").phase != SplitPhase.OFFLOADING


def test_one_split_at_a_time(make_daemon, qbit, db, disk, clock):
    daemon = make_daemon()
    qbit.add("first", tags={"split"}, files=[("x.mkv", 2 * GB)])
    daemon.tick()
    clock.now += 1
    qbit.add("second", tags={"split"}, files=[("y.mkv", 2 * GB)])
    daemon.tick()
    assert normal(qbit, "first") == [0]
    assert normal(qbit, "second") == []
    assert db.get_split_state("second") is None

    complete(qbit, "first", [0])
    daemon.tick()  # first finishes and is removed
    daemon.tick()  # second starts
    assert "first" not in qbit.torrents
    assert normal(qbit, "second") == [0]


def test_resume_after_restart(tmp_path, qbit, notifier, settings, disk, clock):
    def make(db):
        return Daemon(
            db,
            qbit,
            notifier,
            replace(settings, split_reserve=1 * GB),
            disk_usage=disk,
            split_executor=InlineExecutor(),
            clock=clock,
        )

    db1 = Database(tmp_path / "r.db")
    qbit.add("t", tags={"split"}, files=FILES)
    make(db1).tick()
    db1.close()

    db2 = Database(tmp_path / "r.db")
    daemon = make(db2)
    disk.free = 1 * GB
    complete(qbit, "t", [0, 3])
    daemon.tick()
    assert db2.get_split_state("t").phase == SplitPhase.WAITING_SPACE
    assert db2.get_split_state("t").batch_no == 1
