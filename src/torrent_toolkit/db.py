"""SQLite state. Migrations are applied in order based on `PRAGMA user_version`."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
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
    # 2: split
    """
    CREATE TABLE split_files (
        hash        TEXT NOT NULL REFERENCES torrents(hash) ON DELETE CASCADE,
        file_index  INTEGER NOT NULL,
        size        INTEGER NOT NULL,
        status      TEXT NOT NULL,
        batch_no    INTEGER,
        PRIMARY KEY (hash, file_index)
    );
    CREATE TABLE split_state (
        hash            TEXT PRIMARY KEY REFERENCES torrents(hash) ON DELETE CASCADE,
        phase           TEXT NOT NULL,
        batch_no        INTEGER NOT NULL DEFAULT 0,
        last_free       INTEGER,
        last_growth_at  REAL,
        retry_at        REAL,
        stall_notified  INTEGER NOT NULL DEFAULT 0
    );
    """,
    # 3: organize
    """
    CREATE TABLE organized_files (
        hash        TEXT NOT NULL,
        file_index  INTEGER NOT NULL,
        source      TEXT NOT NULL,
        dest        TEXT,
        action      TEXT NOT NULL,
        reason      TEXT,
        done_at     REAL NOT NULL,
        PRIMARY KEY (hash, file_index)
    );
    CREATE TABLE tmdb_cache (
        kind        TEXT NOT NULL,
        title       TEXT NOT NULL,
        year        INTEGER NOT NULL,
        result      TEXT,
        fetched_at  REAL NOT NULL,
        PRIMARY KEY (kind, title, year)
    );
    """,
    # 4: spoof + organize after a torrent left qBittorrent
    """
    CREATE TABLE spoofs (
        hash            TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        torrent_path    TEXT NOT NULL,
        info_hash       TEXT NOT NULL,
        trackers        TEXT NOT NULL,
        size            INTEGER NOT NULL,
        piece_length    INTEGER NOT NULL,
        uploaded        INTEGER NOT NULL,
        speed           INTEGER NOT NULL,
        forever         INTEGER NOT NULL,
        port            INTEGER NOT NULL,
        client_version  TEXT NOT NULL,
        status          TEXT NOT NULL,
        stop_reason     TEXT,
        started_at      REAL NOT NULL,
        last_announce   REAL,
        interval        INTEGER,
        seeders         INTEGER,
        leechers        INTEGER,
        last_error      TEXT
    );
    CREATE TABLE detached_organize (
        hash        TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        save_path   TEXT NOT NULL,
        files       TEXT NOT NULL,
        created_at  REAL NOT NULL
    );
    """,
]


class TorrentState:
    METADATA = "metadata"  # taken over, waiting for the file list
    ACTIVE = "active"  # features running
    DONE = "done"  # daemon finished with it
    DETACHED = "detached"  # removed from qBittorrent (spoof), still being organized
    ORPHAN = "orphan"  # had `service` tag but no DB record; left alone
    GONE = "gone"  # vanished from qBittorrent while active


class SplitPhase:
    QUEUED = "queued"  # waiting for its turn / first batch
    DOWNLOADING = "downloading"  # a batch is downloading
    OFFLOADING = "offloading"  # batch complete; organize / hook running
    WAITING_SPACE = "waiting_space"  # waiting for free space before the next batch
    FINISHED = "finished"  # every file downloaded (or skipped) and offloaded


class SplitFileStatus:
    PENDING = "pending"
    BATCH = "batch"  # in the current batch
    DONE = "done"  # downloaded and offloaded
    SKIPPED = "skipped"  # can never fit


@dataclass
class SplitFile:
    hash: str
    file_index: int
    size: int
    status: str
    batch_no: int | None


@dataclass
class SplitState:
    hash: str
    phase: str
    batch_no: int
    last_free: int | None
    last_growth_at: float | None
    retry_at: float | None
    stall_notified: bool


class SpoofStatus:
    PENDING = "pending"  # recorded; torrent not yet removed from qBittorrent
    ACTIVE = "active"  # announcing (resumed after restarts)
    FINISHED = "finished"  # hit target ratio / max duration
    STOPPED = "stopped"  # stopped through the API
    FAILED = "failed"  # tracker kept failing


@dataclass
class Spoof:
    hash: str
    name: str
    torrent_path: str
    info_hash: str
    trackers: list[str]
    size: int
    piece_length: int
    uploaded: int
    speed: int
    forever: bool
    port: int
    client_version: str
    status: str
    stop_reason: str | None
    started_at: float
    last_announce: float | None
    interval: int | None
    seeders: int | None
    leechers: int | None
    last_error: str | None

    @property
    def ratio(self) -> float:
        return self.uploaded / self.size if self.size else 0.0


def _row_to_spoof(row: sqlite3.Row) -> Spoof:
    d = dict(row)
    d["trackers"] = json.loads(d["trackers"])
    d["forever"] = bool(d["forever"])
    return Spoof(**d)


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

    def set_torrent_features(self, hash: str, features: frozenset[str]) -> None:
        self.conn.execute(
            "UPDATE torrents SET features = ?, updated_at = ? WHERE hash = ?",
            (",".join(sorted(features)), time.time(), hash),
        )

    def delete_torrent(self, hash: str) -> None:
        self.conn.execute("DELETE FROM torrents WHERE hash = ?", (hash,))

    def forget_torrent(self, hash: str) -> None:
        """Drop everything known about a torrent (split state cascades) except its spoof."""
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.execute("DELETE FROM torrents WHERE hash = ?", (hash,))
            self.conn.execute("DELETE FROM organized_files WHERE hash = ?", (hash,))
            self.conn.execute("DELETE FROM detached_organize WHERE hash = ?", (hash,))

    # --- split ---

    def init_split(self, hash: str, files: list[tuple[int, int]], skipped: set[int]) -> None:
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.executemany(
                "INSERT INTO split_files (hash, file_index, size, status) VALUES (?, ?, ?, ?)",
                [
                    (
                        hash,
                        i,
                        size,
                        SplitFileStatus.SKIPPED if i in skipped else SplitFileStatus.PENDING,
                    )
                    for i, size in files
                ],
            )
            self.conn.execute(
                "INSERT INTO split_state (hash, phase) VALUES (?, ?)", (hash, SplitPhase.QUEUED)
            )

    def get_split_state(self, hash: str) -> SplitState | None:
        row = self.conn.execute("SELECT * FROM split_state WHERE hash = ?", (hash,)).fetchone()
        if row is None:
            return None
        return SplitState(**{**dict(row), "stall_notified": bool(row["stall_notified"])})

    def update_split_state(self, hash: str, **fields) -> None:
        if "stall_notified" in fields:
            fields["stall_notified"] = int(fields["stall_notified"])
        assigns = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE split_state SET {assigns} WHERE hash = ?", (*fields.values(), hash)
        )

    def split_files(self, hash: str, status: str | None = None) -> list[SplitFile]:
        sql = "SELECT * FROM split_files WHERE hash = ?"
        args: tuple = (hash,)
        if status:
            sql += " AND status = ?"
            args += (status,)
        rows = self.conn.execute(sql + " ORDER BY file_index", args)
        return [SplitFile(**dict(r)) for r in rows]

    def set_split_files_status(
        self, hash: str, indexes: list[int], status: str, batch_no: int | None = None
    ) -> None:
        self.conn.executemany(
            "UPDATE split_files SET status = ?, batch_no = COALESCE(?, batch_no) "
            "WHERE hash = ? AND file_index = ?",
            [(status, batch_no, hash, i) for i in indexes],
        )

    # --- organize ---

    def organized_indexes(self, hash: str) -> set[int]:
        rows = self.conn.execute("SELECT file_index FROM organized_files WHERE hash = ?", (hash,))
        return {r[0] for r in rows}

    def record_organized(
        self,
        hash: str,
        file_index: int,
        source: str,
        dest: str | None,
        action: str,
        reason: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO organized_files VALUES (?, ?, ?, ?, ?, ?, ?)",
            (hash, file_index, source, dest, action, reason, time.time()),
        )

    def organized_files(self, hash: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM organized_files WHERE hash = ? ORDER BY file_index", (hash,)
        )
        return [dict(r) for r in rows]

    def tmdb_cache_get(
        self, kind: str, title: str, year: int, max_age: float
    ) -> tuple[bool, str | None]:
        """`(hit, result_json)`; a cached miss is `(True, None)`."""
        row = self.conn.execute(
            "SELECT result, fetched_at FROM tmdb_cache WHERE kind = ? AND title = ? AND year = ?",
            (kind, title, year),
        ).fetchone()
        if row is None or time.time() - row["fetched_at"] > max_age:
            return False, None
        return True, row["result"]

    def tmdb_cache_put(self, kind: str, title: str, year: int, result: str | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO tmdb_cache VALUES (?, ?, ?, ?, ?)",
            (kind, title, year, result, time.time()),
        )

    # --- spoof ---

    def add_spoof(self, spoof: Spoof) -> None:
        d = asdict(spoof)
        d["trackers"] = json.dumps(d["trackers"])
        d["forever"] = int(d["forever"])
        cols = ", ".join(d)
        marks = ", ".join("?" * len(d))
        self.conn.execute(
            f"INSERT OR REPLACE INTO spoofs ({cols}) VALUES ({marks})", tuple(d.values())
        )

    def get_spoof(self, hash: str) -> Spoof | None:
        row = self.conn.execute("SELECT * FROM spoofs WHERE hash = ?", (hash,)).fetchone()
        return _row_to_spoof(row) if row else None

    def list_spoofs(self, status: str | None = None) -> list[Spoof]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM spoofs WHERE status = ? ORDER BY started_at", (status,)
            )
        else:
            rows = self.conn.execute("SELECT * FROM spoofs ORDER BY started_at")
        return [_row_to_spoof(r) for r in rows]

    def update_spoof(self, hash: str, **fields) -> None:
        if "forever" in fields:
            fields["forever"] = int(fields["forever"])
        if "trackers" in fields:
            fields["trackers"] = json.dumps(fields["trackers"])
        assigns = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE spoofs SET {assigns} WHERE hash = ?", (*fields.values(), hash))

    # --- detached organize ---

    def add_detached(self, hash: str, name: str, save_path: str, files: dict[int, str]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO detached_organize VALUES (?, ?, ?, ?, ?)",
            (hash, name, save_path, json.dumps({str(k): v for k, v in files.items()}), time.time()),
        )

    def list_detached(self) -> list[tuple[str, str, str, dict[int, str]]]:
        rows = self.conn.execute("SELECT * FROM detached_organize ORDER BY created_at")
        return [
            (
                r["hash"],
                r["name"],
                r["save_path"],
                {int(k): v for k, v in json.loads(r["files"]).items()},
            )
            for r in rows
        ]

    def delete_detached(self, hash: str) -> None:
        self.conn.execute("DELETE FROM detached_organize WHERE hash = ?", (hash,))
