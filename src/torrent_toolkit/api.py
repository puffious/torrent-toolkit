"""HTTP API: health, status and spoof control."""

from __future__ import annotations

from collections import Counter

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from torrent_toolkit import __version__
from torrent_toolkit.config import ConfigError, parse_speed
from torrent_toolkit.db import Database, SplitFileStatus, Spoof, TrackedTorrent
from torrent_toolkit.spoof.announcer import redact
from torrent_toolkit.spoof.manager import SpoofManager


class SpoofPatch(BaseModel):
    speed: str | None = None  # e.g. "2mbps", "0kbps"
    forever: bool | None = None


def _torrent_status(db: Database, t: TrackedTorrent) -> dict:
    out: dict = {
        "hash": t.hash,
        "name": t.name,
        "features": sorted(t.features),
        "state": t.state,
        "updated_at": t.updated_at,
    }
    if (split := db.get_split_state(t.hash)) is not None:
        files = db.split_files(t.hash)
        counts = Counter(f.status for f in files)
        out["split"] = {
            "phase": split.phase,
            "batch": split.batch_no,
            "files_done": counts[SplitFileStatus.DONE],
            "files_in_batch": counts[SplitFileStatus.BATCH],
            "files_pending": counts[SplitFileStatus.PENDING],
            "files_skipped": counts[SplitFileStatus.SKIPPED],
            "bytes_done": sum(f.size for f in files if f.status == SplitFileStatus.DONE),
            "bytes_total": sum(f.size for f in files if f.status != SplitFileStatus.SKIPPED),
        }
    if organized := db.organized_files(t.hash):
        out["organize"] = dict(Counter(f["action"] for f in organized))
    return out


def _spoof_status(s: Spoof) -> dict:
    return {
        "hash": s.hash,
        "name": s.name,
        "status": s.status,
        "stop_reason": s.stop_reason,
        "uploaded": s.uploaded,
        "size": s.size,
        "ratio": round(s.ratio, 4),
        "speed": s.speed,
        "forever": s.forever,
        "started_at": s.started_at,
        "last_announce": s.last_announce,
        "interval": s.interval,
        "seeders": s.seeders,
        "leechers": s.leechers,
        "last_error": s.last_error,
        "trackers": [redact(u) for u in s.trackers],
    }


def create_app(db: Database, spoofer: SpoofManager | None = None) -> FastAPI:
    app = FastAPI(title="torrent-toolkit", version=__version__)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/status")
    def status() -> dict:
        return {
            "torrents": [_torrent_status(db, t) for t in db.list_torrents()],
            "spoofs": [_spoof_status(s) for s in db.list_spoofs()],
        }

    @app.get("/spoof/{hash}")
    def get_spoof(hash: str) -> dict:
        s = db.get_spoof(hash.lower())
        if s is None:
            raise HTTPException(404, "unknown spoof")
        return _spoof_status(s)

    @app.post("/spoof/{hash}/stop")
    async def stop_spoof(hash: str) -> dict:
        if spoofer is None:
            raise HTTPException(503, "spoofer not running")
        if not await spoofer.stop(hash.lower()):
            raise HTTPException(404, "no active spoof with that hash")
        return _spoof_status(db.get_spoof(hash.lower()))

    @app.patch("/spoof/{hash}")
    def patch_spoof(hash: str, patch: SpoofPatch) -> dict:
        if spoofer is None:
            raise HTTPException(503, "spoofer not running")
        speed = None
        if patch.speed is not None:
            try:
                speed = parse_speed(patch.speed)
            except ConfigError as e:
                raise HTTPException(422, str(e)) from None
        if not spoofer.update(hash.lower(), speed=speed, forever=patch.forever):
            raise HTTPException(404, "unknown spoof")
        return _spoof_status(db.get_spoof(hash.lower()))

    return app
