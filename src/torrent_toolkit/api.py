"""HTTP API: health and status (control endpoints arrive with the spoofer)."""

from __future__ import annotations

from fastapi import FastAPI

from torrent_toolkit import __version__
from torrent_toolkit.db import Database


def create_app(db: Database) -> FastAPI:
    app = FastAPI(title="torrent-toolkit", version=__version__)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/status")
    def status() -> dict:
        return {
            "torrents": [
                {
                    "hash": t.hash,
                    "name": t.name,
                    "features": sorted(t.features),
                    "state": t.state,
                    "updated_at": t.updated_at,
                }
                for t in db.list_torrents()
            ]
        }

    return app
