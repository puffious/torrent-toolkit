import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeQbit  # noqa: E402

from torrent_toolkit.config import Settings  # noqa: E402
from torrent_toolkit.db import Database  # noqa: E402


class RecordingNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, body: str) -> None:
        self.sent.append((title, body))


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "toolkit.db")
    yield d
    d.close()


@pytest.fixture
def qbit():
    return FakeQbit()


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def settings(tmp_path):
    return Settings(qbit_url="http://qbit", data_dir=tmp_path, split_reserve=0)
