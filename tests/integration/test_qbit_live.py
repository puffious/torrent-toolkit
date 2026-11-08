"""Against a real qBittorrent (docker-compose.test.yml). Run: uv run pytest -m integration"""

import os
import secrets
import time

import pytest

from torrent_toolkit.daemon import TAG_SERVICE, Daemon
from torrent_toolkit.db import TorrentState
from torrent_toolkit.qbit import Qbit

pytestmark = pytest.mark.integration

URL = os.environ.get("TK_TEST_QBIT_URL", "http://localhost:18080")


@pytest.fixture
def live():
    q = Qbit(URL, "", "")
    try:
        q.version()
    except Exception as e:
        pytest.skip(f"qBittorrent not reachable at {URL}: {e}")
    return q


def test_identity(live):
    assert live.version()[0].isdigit()
    assert live.listen_port() > 0


def test_takeover_of_paused_magnet(live, db, notifier, settings):
    info_hash = secrets.token_hex(20)
    live._client.torrents_add(
        urls=f"magnet:?xt=urn:btih:{info_hash}&dn=tk-test", tags="organize", is_stopped=True
    )
    try:
        for _ in range(20):
            if info_hash in live.poll():
                break
            time.sleep(0.5)
        Daemon(db, live, notifier, settings).tick()
        assert db.get_torrent(info_hash).state == TorrentState.METADATA
        assert TAG_SERVICE in live.poll()[info_hash].tags
    finally:
        live.delete(info_hash, delete_files=True)
