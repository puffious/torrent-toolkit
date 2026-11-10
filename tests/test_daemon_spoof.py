from dataclasses import replace

import pytest
from fakes import Clock, FakeDisk, InlineExecutor
from test_organizer import FINDER

from torrent_toolkit.daemon import TAG_SERVICE, Daemon
from torrent_toolkit.db import SpoofStatus, TorrentState
from torrent_toolkit.spoof.bencode import encode, parse_torrent

MB = 1024**2


def torrent_bytes(name="Heat.1995", files=(("Heat.1995.mkv", 10),)):
    data = encode(
        {
            "announce": "udp://ignored:1",
            "info": {
                "name": name,
                "piece length": 16384,
                "pieces": b"\0" * 20,
                "files": [{"length": s, "path": [n]} for n, s in files],
            },
        }
    )
    return data, parse_torrent(data).info_hash_hex


@pytest.fixture
def paths(tmp_path):
    save = tmp_path / "downloads"
    library = tmp_path / "mount"
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


def add_spoof_torrent(qbit, save, tags, uploaded=123 * MB, **kw):
    data, h = torrent_bytes(**kw)
    files = [
        (f"{kw.get('name', 'Heat.1995')}/{n}", s)
        for n, s in kw.get("files", (("Heat.1995.mkv", 10),))
    ]
    for rel, size in files:
        p = save / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    t = qbit.add(
        h,
        name=kw.get("name", "Heat.1995"),
        tags=tags,
        files=files,
        save_path=str(save),
        uploaded=uploaded,
    )
    t.torrent_bytes = data
    t.trackers = ["udp://ignored:1", "https://tracker/announce?passkey=pk"]
    return h


def complete(qbit, h):
    for f in qbit.torrents[h].files:
        qbit.set_file_progress(h, f.index, 1.0)
    qbit.update(h, progress=1.0, state="uploading")


def test_handoff_after_download(make_daemon, qbit, db, paths, settings):
    save, _, _ = paths
    daemon = make_daemon()
    h = add_spoof_torrent(qbit, save, {"spoof", "spoof-speed:2mbps", "spoof-forever"})
    data = qbit.torrents[h].torrent_bytes
    daemon.tick()
    assert h in qbit.torrents  # still downloading

    complete(qbit, h)
    daemon.tick()
    assert h not in qbit.torrents
    assert ("delete", h, False) in qbit.calls
    s = db.get_spoof(h)
    assert s.status == SpoofStatus.ACTIVE
    assert s.uploaded == 123 * MB and s.size == 10
    assert s.speed == 2 * MB and s.forever
    assert s.trackers == ["https://tracker/announce?passkey=pk"]
    assert (s.port, s.client_version) == (qbit.port, "5.0.2")
    assert s.torrent_path == str(settings.torrents_dir / f"{h}.torrent")
    assert (settings.torrents_dir / f"{h}.torrent").read_bytes() == data
    assert db.get_torrent(h).state == TorrentState.DONE
    assert (save / "Heat.1995/Heat.1995.mkv").exists()  # data kept


def test_handoff_defaults(make_daemon, qbit, db, paths, settings):
    save, _, _ = paths
    daemon = make_daemon()
    h = add_spoof_torrent(qbit, save, {"spoof", "spoof-speed:bogus"})
    complete(qbit, h)
    daemon.tick()
    s = db.get_spoof(h)
    assert s.speed == settings.spoof_speed and not s.forever


def test_spoof_and_organize(make_daemon, qbit, db, paths):
    save, library, _ = paths
    daemon = make_daemon()
    h = add_spoof_torrent(qbit, save, {"spoof", "organize"})
    complete(qbit, h)
    daemon.tick()  # hand-off + organize job submitted
    assert db.get_torrent(h).state == TorrentState.DETACHED
    daemon.tick()  # organize collected
    assert (library / "movies/Heat (1995)/Heat.1995.mkv").exists()
    assert db.get_torrent(h).state == TorrentState.DONE
    assert db.list_detached() == []


def test_no_http_tracker_keeps_torrent(make_daemon, qbit, db, paths, notifier):
    save, _, _ = paths
    daemon = make_daemon()
    h = add_spoof_torrent(qbit, save, {"spoof"})
    qbit.torrents[h].trackers = ["udp://only:1"]
    complete(qbit, h)
    daemon.tick()
    daemon.tick()
    assert h in qbit.torrents
    assert db.get_spoof(h) is None
    assert notifier.sent[0][0] == "spoof hand-off failed"
    assert TAG_SERVICE not in qbit.torrents[h].info.tags
    assert db.get_torrent(h).state == TorrentState.DONE


def test_hash_mismatch_rejected(make_daemon, qbit, db, paths, notifier):
    save, _, _ = paths
    daemon = make_daemon()
    h = add_spoof_torrent(qbit, save, {"spoof", "organize"})
    qbit.torrents[h].torrent_bytes = torrent_bytes(name="Other")[0]
    complete(qbit, h)
    daemon.tick()  # hand-off fails → spoof dropped, organize continues
    daemon.tick()
    daemon.tick()
    assert "info hash mismatch" in notifier.sent[0][1]
    assert db.get_torrent(h).features == {"organize"}
    assert db.get_torrent(h).state == TorrentState.DONE
    assert h in qbit.torrents


def test_split_then_spoof(make_daemon, qbit, db, paths):
    save, _, _ = paths
    daemon = make_daemon()
    h = add_spoof_torrent(qbit, save, {"split", "spoof"})
    parts = save / f".{h}.parts"
    parts.write_bytes(b"p")
    daemon.tick()  # batch 1 = everything
    complete(qbit, h)
    daemon.tick()  # offloaded (no hook) → finished → hand-off
    assert h not in qbit.torrents
    assert db.get_spoof(h).status == SpoofStatus.ACTIVE
    assert not parts.exists()


def test_pending_spoof_activated_when_torrent_gone(make_daemon, qbit, db, paths):
    save, _, _ = paths
    daemon = make_daemon()
    h = add_spoof_torrent(qbit, save, {"spoof"})
    complete(qbit, h)
    daemon.tick()
    db.update_spoof(h, status=SpoofStatus.PENDING)  # as if we crashed after the delete
    daemon.tick()
    assert db.get_spoof(h).status == SpoofStatus.ACTIVE
