"""SQLite state. Migrations are applied in order based on `PRAGMA user_version`."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: tracked torrents
    """
    CREATE TABLE torrents (
        hash        TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        save_path   TEXT NOT NULL,
        features    TEXT NOT NULL,
        state       TEXT NOT NULL,
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    );
    """,
]


class TorrentState:
    METADATA = "metadata"  # taken over, waiting for the file list
    ACTIVE = "active"  # features running
    DONE = "done"  # daemon finished with it
    ORPHAN = "orphan"  # had `service` tag but no DB record; left alone
    GONE = "gone"  # vanished from qBittorrent while active


@dataclass
class TrackedTorrent:
    hash: str
    name: str
    save_path: str
    features: frozenset[str]
    state: str
    created_at: float
    updated_at: float


def _row_to_torrent(row: sqlite3.Row) -> TrackedTorrent:
    return TrackedTorrent(
        hash=row["hash"],
        name=row["name"],
        save_path=row["save_path"],
        features=frozenset(f for f in row["features"].split(",") if f),
        state=row["state"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            # Statements run one by one: executescript() would commit mid-migration.
            self.conn.execute("BEGIN")
            try:
                for stmt in filter(str.strip, sql.split(";")):
                    self.conn.execute(stmt)
                self.conn.execute(f"PRAGMA user_version = {i}")
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        self.conn.close()

    # --- torrents ---

    def get_torrent(self, hash: str) -> TrackedTorrent | None:
        row = self.conn.execute("SELECT * FROM torrents WHERE hash = ?", (hash,)).fetchone()
        return _row_to_torrent(row) if row else None

    def list_torrents(self, states: tuple[str, ...] | None = None) -> list[TrackedTorrent]:
        if states:
            marks = ",".join("?" * len(states))
            rows = self.conn.execute(
                f"SELECT * FROM torrents WHERE state IN ({marks}) ORDER BY created_at", states
            )
        else:
            rows = self.conn.execute("SELECT * FROM torrents ORDER BY created_at")
        return [_row_to_torrent(r) for r in rows]

    def add_torrent(
        self, hash: str, name: str, save_path: str, features: frozenset[str], state: str
    ) -> TrackedTorrent:
        now = time.time()
        self.conn.execute(
            "INSERT INTO torrents VALUES (?, ?, ?, ?, ?, ?, ?)",
            (hash, name, save_path, ",".join(sorted(features)), state, now, now),
        )
        return self.get_torrent(hash)  # type: ignore[return-value]

    def set_torrent_state(self, hash: str, state: str) -> None:
        self.conn.execute(
            "UPDATE torrents SET state = ?, updated_at = ? WHERE hash = ?",
            (state, time.time(), hash),
        )

    def update_torrent(self, hash: str, *, name: str, save_path: str) -> None:
        self.conn.execute(
            "UPDATE torrents SET name = ?, save_path = ?, updated_at = ? WHERE hash = ?",
            (name, save_path, time.time(), hash),
        )

    def delete_torrent(self, hash: str) -> None:
        self.conn.execute("DELETE FROM torrents WHERE hash = ?", (hash,))
