import asyncio

import pytest
from fastapi.testclient import TestClient
from test_spoof_manager import HASH, make_spoof

from torrent_toolkit.api import create_app
from torrent_toolkit.db import SpoofStatus, TorrentState


class FakeSpoofer:
    def __init__(self, db):
        self.db = db
        self.stopped = []

    async def stop(self, hash, reason="stopped via API"):
        await asyncio.sleep(0)
        s = self.db.get_spoof(hash)
        if s is None or s.status != SpoofStatus.ACTIVE:
            return False
        self.db.update_spoof(hash, status=SpoofStatus.STOPPED, stop_reason=reason)
        self.stopped.append(hash)
        return True

    def update(self, hash, *, speed=None, forever=None):
        if self.db.get_spoof(hash) is None:
            return False
        fields = {k: v for k, v in {"speed": speed, "forever": forever}.items() if v is not None}
        if fields:
            self.db.update_spoof(hash, **fields)
        return True


@pytest.fixture
def client(db):
    return TestClient(create_app(db, FakeSpoofer(db)))


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_status_includes_split_organize_and_spoofs(client, db):
    db.add_torrent("a", "Pack", "/data", frozenset({"split", "organize"}), TorrentState.ACTIVE)
    db.init_split("a", [(0, 10), (1, 20), (2, 999)], skipped={2})
    db.set_split_files_status("a", [0], "done", 1)
    db.record_organized("a", 0, "/data/x.mkv", "/mount/movies/X/x.mkv", "moved")
    db.add_spoof(make_spoof())

    body = client.get("/status").json()
    [t] = body["torrents"]
    assert t["split"]["files_done"] == 1 and t["split"]["files_skipped"] == 1
    assert t["split"]["bytes_done"] == 10 and t["split"]["bytes_total"] == 30
    assert t["organize"] == {"moved": 1}
    [s] = body["spoofs"]
    assert s["hash"] == HASH and s["status"] == "active"
    assert "passkey" not in str(s["trackers"])


def test_spoof_stop(client, db):
    db.add_spoof(make_spoof())
    r = client.post(f"/spoof/{HASH.upper()}/stop")
    assert r.status_code == 200 and r.json()["status"] == "stopped"
    assert client.post(f"/spoof/{HASH}/stop").status_code == 404


def test_spoof_patch(client, db):
    db.add_spoof(make_spoof())
    r = client.patch(f"/spoof/{HASH}", json={"speed": "2mbps", "forever": True})
    assert r.status_code == 200
    assert r.json()["speed"] == 2 * 1024**2 and r.json()["forever"] is True
    assert client.patch(f"/spoof/{HASH}", json={"speed": "fast"}).status_code == 422
    assert client.patch("/spoof/nope", json={"forever": True}).status_code == 404
    assert client.get("/spoof/nope").status_code == 404
    assert client.get(f"/spoof/{HASH}").json()["forever"] is True


def test_control_without_spoofer(db):
    c = TestClient(create_app(db))
    assert c.post(f"/spoof/{HASH}/stop").status_code == 503
