from dataclasses import replace
from pathlib import Path

import pytest
from fakes import Clock, FakeDisk, InlineExecutor
from test_organizer import FINDER

from torrent_toolkit.daemon import TAG_REVIEW, TAG_SERVICE, Daemon
from torrent_toolkit.db import TorrentState

GB = 1024**3


@pytest.fixture
def paths(tmp_path):
    save = tmp_path / "downloads"
    library = tmp_path / "mount"
    (library / "movies").mkdir(parents=True)
    (library / "shows").mkdir()
    save.mkdir()
    return save, library, tmp_path / "review"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_daemon(db, qbit, notifier, settings, paths, clock):
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
            clock=clock,
        )

    return make


def add_with_files(qbit, save: Path, hash: str, files: dict[str, int], tags: set[str]):
    for rel, size in files.items():
        p = save / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    return qbit.add(hash, tags=tags, files=list(files.items()), save_path=str(save))


def finish(qbit, hash):
    for f in qbit.torrents[hash].files:
        qbit.set_file_progress(hash, f.index, 1.0)
    qbit.update(hash, progress=1.0, state="uploading")


def test_organize_torrent_when_complete(make_daemon, qbit, db, paths, notifier):
    save, library, review = paths
    daemon = make_daemon()
    add_with_files(
        qbit, save, "h", {"Heat.1995/Heat.1995.mkv": 10, "Heat.1995/x.nfo": 1}, {"organize"}
    )
    daemon.tick()
    assert (save / "Heat.1995/Heat.1995.mkv").exists()  # not complete yet

    finish(qbit, "h")
    daemon.tick()  # job submitted (runs inline)
    daemon.tick()  # result collected
    assert (library / "movies/Heat (1995)/Heat.1995.mkv").exists()
    assert TAG_SERVICE not in qbit.torrents["h"].info.tags
    assert "h" in qbit.torrents  # stays in qBittorrent
    assert db.get_torrent("h").state == TorrentState.DONE
    assert notifier.sent == []


def test_unidentified_files_tagged_for_review(make_daemon, qbit, paths, notifier):
    save, library, review = paths
    daemon = make_daemon()
    add_with_files(qbit, save, "h", {"Unknown.Thing.2001.mkv": 10}, {"organize"})
    finish(qbit, "h")
    daemon.tick()
    daemon.tick()
    assert TAG_REVIEW in qbit.torrents["h"].info.tags
    assert notifier.sent[0][0] == "files need review"
    assert (review / "Unknown.Thing.2001/Unknown.Thing.2001.mkv").exists()


def test_library_down_notifies_once_and_retries(make_daemon, qbit, paths, notifier, clock):
    save, library, review = paths
    (library / "movies").rmdir()
    (library / "shows").rmdir()
    daemon = make_daemon()
    add_with_files(qbit, save, "h", {"Heat.1995.mkv": 10}, {"organize"})
    finish(qbit, "h")
    for _ in range(3):
        daemon.tick()
    assert [t for t, _ in notifier.sent] == ["organize failed"]

    (library / "movies").mkdir()
    clock.now += 601
    daemon.tick()
    daemon.tick()
    assert (library / "movies/Heat (1995)/Heat.1995.mkv").exists()


def test_split_and_organize(make_daemon, qbit, db, paths):
    save, library, review = paths
    disk = FakeDisk(total=20 * GB, free=10 * GB)
    daemon = make_daemon(disk=disk, split_reserve=1 * GB)
    # Sizes for the planner come from qBittorrent; files on disk can be tiny.
    files = {"Pack/Heat.1995.mkv": 6, "Pack/Inception.2010.mkv": 5, "Pack/Heat.1995.en.srt": 1}
    t = add_with_files(qbit, save, "h", files, {"split", "organize"})
    sizes = [6 * GB, 5 * GB, 1]
    t.files = [replace(f, size=s) for f, s in zip(t.files, sizes, strict=True)]

    daemon.tick()  # batch 1: 6 GB + srt (5 GB would exceed 9 GB capacity)
    assert [i for i, p in enumerate(qbit.priorities("h")) if p] == [0, 2]
    for i in (0, 2):
        qbit.set_file_progress("h", i, 1.0)
    disk.free = 1 * GB
    daemon.tick()  # batch complete → organized → waiting for space
    assert (library / "movies/Heat (1995)/Heat.1995.mkv").exists()
    assert (library / "movies/Heat (1995)/Heat.1995.en.srt").exists()

    disk.free = 10 * GB
    daemon.tick()  # batch 2
    qbit.set_file_progress("h", 1, 1.0)
    daemon.tick()
    assert (library / "movies/Inception (2010)/Inception.2010.mkv").exists()
    assert "h" not in qbit.torrents
    assert db.get_torrent("h").state == TorrentState.DONE
