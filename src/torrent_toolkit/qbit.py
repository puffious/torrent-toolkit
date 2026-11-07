"""Thin wrapper over qbittorrent-api exposing only what the daemon needs.

All methods are blocking; the daemon calls them from its worker thread.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import qbittorrentapi

log = logging.getLogger(__name__)

PAUSED_STATES = frozenset({"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"})
METADATA_STATES = frozenset({"metaDL", "forcedMetaDL"})

PRIORITY_SKIP = 0
PRIORITY_NORMAL = 1


class QbitError(Exception):
    pass


@dataclass(frozen=True)
class TorrentInfo:
    hash: str
    name: str
    state: str
    tags: frozenset[str]
    save_path: str
    size: int  # total size of all files, regardless of selection
    progress: float
    uploaded: int
    has_metadata: bool

    @property
    def paused(self) -> bool:
        return self.state in PAUSED_STATES

    @classmethod
    def from_api(cls, hash: str, d: dict[str, Any]) -> TorrentInfo:
        state = d.get("state", "")
        size = int(d.get("total_size", 0) or 0)
        has_metadata = d.get("has_metadata")
        if has_metadata is None:  # field only exists on qBittorrent 5+
            has_metadata = state not in METADATA_STATES and size > 0
        return cls(
            hash=hash,
            name=d.get("name", ""),
            state=state,
            tags=parse_tags(d.get("tags", "")),
            save_path=d.get("save_path", ""),
            size=size,
            progress=float(d.get("progress", 0.0)),
            uploaded=int(d.get("uploaded", 0) or 0),
            has_metadata=bool(has_metadata),
        )


@dataclass(frozen=True)
class FileInfo:
    index: int
    name: str  # path relative to save_path
    size: int
    progress: float
    priority: int


def parse_tags(raw: str | list[str]) -> frozenset[str]:
    if isinstance(raw, list):
        return frozenset(t.strip() for t in raw if t.strip())
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


class QbitClient(Protocol):
    """What the daemon depends on; implemented by `Qbit` and test fakes."""

    def poll(self) -> dict[str, TorrentInfo]: ...
    def files(self, hash: str) -> list[FileInfo]: ...
    def set_file_priority(self, hash: str, indexes: list[int], priority: int) -> None: ...
    def start(self, hash: str) -> None: ...
    def stop(self, hash: str) -> None: ...
    def add_tags(self, hash: str, *tags: str) -> None: ...
    def remove_tags(self, hash: str, *tags: str) -> None: ...
    def export(self, hash: str) -> bytes: ...
    def trackers(self, hash: str) -> list[str]: ...
    def delete(self, hash: str, delete_files: bool) -> None: ...
    def listen_port(self) -> int: ...
    def version(self) -> str: ...


class Qbit:
    def __init__(self, url: str, user: str, password: str):
        self._client = qbittorrentapi.Client(
            host=url,
            username=user or None,
            password=password or None,
            REQUESTS_ARGS={"timeout": (5, 30)},
            VERIFY_WEBUI_CERTIFICATE=False,
        )
        self._rid = 0
        self._raw: dict[str, dict[str, Any]] = {}

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except qbittorrentapi.exceptions.APIError as e:
            raise QbitError(f"{type(e).__name__}: {e}") from e

    def poll(self) -> dict[str, TorrentInfo]:
        """Incremental `sync/maindata`; returns the full merged torrent view."""
        data = self._call(self._client.sync_maindata, rid=self._rid)
        if data.get("full_update"):
            self._raw = {}
        for hash, partial in (data.get("torrents") or {}).items():
            self._raw.setdefault(hash, {}).update(partial)
        for hash in data.get("torrents_removed") or []:
            self._raw.pop(hash, None)
        self._rid = data.get("rid", self._rid)
        return {h: TorrentInfo.from_api(h, d) for h, d in self._raw.items()}

    def files(self, hash: str) -> list[FileInfo]:
        return [
            FileInfo(
                index=f["index"],
                name=f["name"],
                size=int(f["size"]),
                progress=float(f["progress"]),
                priority=int(f["priority"]),
            )
            for f in self._call(self._client.torrents_files, torrent_hash=hash)
        ]

    def set_file_priority(self, hash: str, indexes: list[int], priority: int) -> None:
        if indexes:
            self._call(
                self._client.torrents_file_priority,
                torrent_hash=hash,
                file_ids=indexes,
                priority=priority,
            )

    def start(self, hash: str) -> None:
        self._call(self._client.torrents_start, torrent_hashes=hash)

    def stop(self, hash: str) -> None:
        self._call(self._client.torrents_stop, torrent_hashes=hash)

    def add_tags(self, hash: str, *tags: str) -> None:
        self._call(self._client.torrents_add_tags, tags=list(tags), torrent_hashes=hash)

    def remove_tags(self, hash: str, *tags: str) -> None:
        self._call(self._client.torrents_remove_tags, tags=list(tags), torrent_hashes=hash)

    def export(self, hash: str) -> bytes:
        return self._call(self._client.torrents_export, torrent_hash=hash)

    def trackers(self, hash: str) -> list[str]:
        """Real tracker URLs in tier order (DHT/PeX/LSD pseudo-entries dropped)."""
        entries = self._call(self._client.torrents_trackers, torrent_hash=hash)
        real = [t for t in entries if "://" in t["url"]]
        return [t["url"] for t in sorted(real, key=lambda t: t.get("tier", 0))]

    def delete(self, hash: str, delete_files: bool) -> None:
        self._call(self._client.torrents_delete, torrent_hashes=hash, delete_files=delete_files)

    def listen_port(self) -> int:
        return int(self._call(self._client.app_preferences)["listen_port"])

    def version(self) -> str:
        return str(self._call(self._client.app_version)).lstrip("v")
