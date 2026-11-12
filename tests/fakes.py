"""In-memory stand-in for qBittorrent, implementing `QbitClient`."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field, replace

from torrent_toolkit.qbit import PRIORITY_NORMAL, FileInfo, TorrentInfo, TorrentNotFound


@dataclass
class FakeTorrent:
    info: TorrentInfo
    files: list[FileInfo] = field(default_factory=list)
    torrent_bytes: bytes = b""
    trackers: list[str] = field(default_factory=list)


class FakeQbit:
    def __init__(self, port: int = 51413, version: str = "5.0.2"):
        self.torrents: dict[str, FakeTorrent] = {}
        self.calls: list[tuple] = []
        self.port = port
        self._version = version
        self.fail_next_poll: Exception | None = None

    # --- test helpers ---

    def add(
        self,
        hash: str,
        name: str = "Some.Torrent",
        *,
        tags: set[str] = frozenset(),
        state: str = "stoppedDL",
        files: list[tuple[str, int]] = (),
        save_path: str = "/data/torrents",
        has_metadata: bool = True,
        uploaded: int = 0,
        added_on: float = 0.0,
        download_path: str = "",
        progress: list[float] | None = None,
    ) -> FakeTorrent:
        file_infos = [
            FileInfo(
                index=i,
                name=n,
                size=s,
                progress=progress[i] if progress else 0.0,
                priority=PRIORITY_NORMAL,
            )
            for i, (n, s) in enumerate(files)
        ]
        info = TorrentInfo(
            hash=hash,
            name=name,
            state=state,
            tags=frozenset(tags),
            save_path=save_path,
            size=sum(s for _, s in files),
            progress=0.0,
            uploaded=uploaded,
            has_metadata=has_metadata,
            added_on=added_on,
            download_path=download_path,
        )
        self.torrents[hash] = FakeTorrent(info, file_infos)
        return self.torrents[hash]

    def _get(self, hash: str) -> FakeTorrent:
        if hash not in self.torrents:
            raise TorrentNotFound(f"torrent {hash} not found")
        return self.torrents[hash]

    def update(self, hash: str, **changes) -> None:
        t = self._get(hash)
        t.info = replace(t.info, **changes)

    def set_file_progress(self, hash: str, index: int, progress: float) -> None:
        t = self.torrents[hash]
        t.files[index] = replace(t.files[index], progress=progress)

    def priorities(self, hash: str) -> list[int]:
        return [f.priority for f in self.torrents[hash].files]

    # --- QbitClient ---

    def poll(self) -> dict[str, TorrentInfo]:
        if self.fail_next_poll:
            e, self.fail_next_poll = self.fail_next_poll, None
            raise e
        return {h: t.info for h, t in self.torrents.items()}

    def files(self, hash: str) -> list[FileInfo]:
        return list(self.torrents[hash].files)

    def set_file_priority(self, hash: str, indexes: list[int], priority: int) -> None:
        self.calls.append(("set_file_priority", hash, tuple(indexes), priority))
        t = self.torrents[hash]
        for i in indexes:
            t.files[i] = replace(t.files[i], priority=priority)

    def start(self, hash: str) -> None:
        self.calls.append(("start", hash))
        self.update(
            hash,
            state=self.torrents[hash].info.state.replace("stopped", "").replace("paused", "")
            or "downloading",
        )

    def stop(self, hash: str) -> None:
        self.calls.append(("stop", hash))
        self.update(hash, state="stoppedDL")

    def add_tags(self, hash: str, *tags: str) -> None:
        self.update(hash, tags=self._get(hash).info.tags | set(tags))

    def remove_tags(self, hash: str, *tags: str) -> None:
        self.update(hash, tags=self.torrents[hash].info.tags - set(tags))

    def export(self, hash: str) -> bytes:
        return self._get(hash).torrent_bytes

    def trackers(self, hash: str) -> list[str]:
        return list(self._get(hash).trackers)

    def delete(self, hash: str, delete_files: bool) -> None:
        self.calls.append(("delete", hash, delete_files))
        self._get(hash)
        del self.torrents[hash]

    def listen_port(self) -> int:
        return self.port

    def version(self) -> str:
        return self._version


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
