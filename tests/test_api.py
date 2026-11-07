from fastapi.testclient import TestClient

from torrent_toolkit.api import create_app
from torrent_toolkit.db import TorrentState


def test_health_and_status(db):
    db.add_torrent("a", "Movie", "/data", frozenset({"organize"}), TorrentState.ACTIVE)
    client = TestClient(create_app(db))
    assert client.get("/health").json()["status"] == "ok"
    [t] = client.get("/status").json()["torrents"]
    assert t["hash"] == "a" and t["features"] == ["organize"] and t["state"] == "active"
