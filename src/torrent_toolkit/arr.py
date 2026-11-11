"""Optional Sonarr/Radarr integration: reuse their folder names and trigger rescans.

Only folder *names* are taken from the *arrs, never full paths, so the *arr containers may
mount the library at a different path than the toolkit does.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

import httpx

log = logging.getLogger(__name__)

SERIES_CACHE_SECONDS = 600


@dataclass(frozen=True)
class SeriesFolder:
    folder: str
    season_folders: bool
    season_format: str  # Sonarr's seasonFolderFormat, e.g. "Season {season:00}"
    series_id: int

    def season_dir(self, season: int) -> str | None:
        if not self.season_folders:
            return None
        return format_season(self.season_format, season)


def format_season(fmt: str, season: int) -> str:
    def repl(m: re.Match) -> str:
        pad = m.group(1) or ""
        return str(season).zfill(len(pad)) if pad else str(season)

    return re.sub(r"\{season(?::(0+))?\}", repl, fmt, flags=re.IGNORECASE)


class _Api:
    def __init__(self, url: str, key: str, client: httpx.Client | None):
        self.client = client or httpx.Client(timeout=20)
        self.base = url.rstrip("/") + "/api/v3"
        self.headers = {"X-Api-Key": key}

    def get(self, path: str, **params):
        r = self.client.get(self.base + path, params=params, headers=self.headers)
        r.raise_for_status()
        return r.json()

    def command(self, name: str, **body) -> None:
        r = self.client.post(
            self.base + "/command", json={"name": name, **body}, headers=self.headers
        )
        r.raise_for_status()


class Arr:
    def __init__(
        self,
        sonarr_url: str = "",
        sonarr_key: str = "",
        radarr_url: str = "",
        radarr_key: str = "",
        client: httpx.Client | None = None,
    ):
        self.sonarr = _Api(sonarr_url, sonarr_key, client) if sonarr_url and sonarr_key else None
        self.radarr = _Api(radarr_url, radarr_key, client) if radarr_url and radarr_key else None
        self._series: dict[int, SeriesFolder] = {}
        self._series_at = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.sonarr or self.radarr)

    # --- lookups (failures fall back to our own naming) ---

    def movie(self, tmdb_id: int) -> tuple[str, int] | None:
        """`(folder name, radarr movie id)` if Radarr tracks this movie."""
        if self.radarr is None:
            return None
        try:
            movies = self.radarr.get("/movie", tmdbId=tmdb_id)
        except httpx.HTTPError as e:
            log.warning("radarr lookup failed", extra={"tmdb_id": tmdb_id, "error": str(e)})
            return None
        for m in movies:
            if m.get("tmdbId") == tmdb_id and m.get("path"):
                return PurePosixPath(m["path"]).name, m["id"]
        return None

    def series(self, tmdb_id: int) -> SeriesFolder | None:
        if self.sonarr is None:
            return None
        with self._lock:
            if time.time() - self._series_at > SERIES_CACHE_SECONDS:
                try:
                    naming = self.sonarr.get("/config/naming")
                    fmt = naming.get("seasonFolderFormat") or "Season {season}"
                    self._series = {
                        s["tmdbId"]: SeriesFolder(
                            folder=PurePosixPath(s["path"]).name,
                            season_folders=s.get("seasonFolder", True),
                            season_format=fmt,
                            series_id=s["id"],
                        )
                        for s in self.sonarr.get("/series")
                        if s.get("tmdbId") and s.get("path")
                    }
                    self._series_at = time.time()
                except httpx.HTTPError as e:
                    log.warning("sonarr lookup failed", extra={"error": str(e)})
                    return None
            return self._series.get(tmdb_id)

    # --- rescans ---

    def rescan(self, kind: str, tmdb_id: int) -> None:
        try:
            if kind == "movie" and (hit := self.movie(tmdb_id)):
                self.radarr.command("RescanMovie", movieId=hit[1])
            elif kind == "tv" and (s := self.series(tmdb_id)):
                self.sonarr.command("RescanSeries", seriesId=s.series_id)
        except httpx.HTTPError as e:
            log.warning("rescan failed", extra={"kind": kind, "tmdb_id": tmdb_id, "error": str(e)})
