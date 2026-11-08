"""Guess what a video file is from its filename and its path inside the torrent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from guessit import guessit


@dataclass(frozen=True)
class Guess:
    kind: str  # "movie" | "episode"
    title: str | None
    year: int | None
    season: int | None = None
    episode: int | None = None  # first episode for multi-episode files


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _int(value) -> int | None:
    value = _first(value)
    return value if isinstance(value, int) else None


def _str(value) -> str | None:
    value = _first(value)
    return str(value).strip() or None if value is not None else None


def guess(path: str) -> Guess:
    """Combine guessit on the bare filename and on the full relative path.

    The filename is reliable for movie packs (`Pack/Inception.2010.mkv`); the path
    supplies show title and season for packs like `Show.S01/01.mkv` or
    `The Office/Season 2/02x05 - Halloween.mkv`.
    """
    p = PurePosixPath(path)
    g_file = guessit(p.name)
    g_path = guessit(str(p)) if len(p.parts) > 1 else g_file

    file_episode = _int(g_file.get("episode"))
    path_episode = _int(g_path.get("episode"))
    file_is_dated_movie = g_file.get("type") == "movie" and _int(g_file.get("year"))

    if not file_is_dated_movie and (file_episode is not None or path_episode is not None):
        season = _int(g_file.get("season"))
        if season is None:
            season = _int(g_path.get("season"))
        title = _str(g_path.get("title")) if g_path.get("type") == "episode" else None
        return Guess(
            kind="episode",
            title=title or _str(g_file.get("title")),
            year=_int(g_path.get("year")) or _int(g_file.get("year")),
            season=season,
            episode=file_episode if file_episode is not None else path_episode,
        )

    title = _str(g_file.get("title")) or _str(g_path.get("title"))
    year = _int(g_file.get("year"))
    if year is None and _str(g_path.get("title")) == title:
        year = _int(g_path.get("year"))
    return Guess(kind="movie", title=title, year=year)
