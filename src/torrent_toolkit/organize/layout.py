"""Plan where every file of a torrent goes (pure apart from TMDB lookups)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol

from torrent_toolkit.organize.classify import FileClass, classify
from torrent_toolkit.organize.identify import Guess, guess
from torrent_toolkit.organize.tmdb import Match

MOVIES_DIR = "movies"
SHOWS_DIR = "shows"
_SUBS_DIRS = {"subs", "subtitles", "sub", "subtitle"}


class Action(StrEnum):
    MOVE = "move"  # into the library
    DELETE = "delete"  # junk
    REVIEW = "review"  # dump for manual sorting


@dataclass(frozen=True)
class Plan:
    action: Action
    dest: Path | None = None  # library destination for MOVE
    reason: str | None = None  # why REVIEW


class Finder(Protocol):
    def find(self, kind: str, title: str, year: int | None) -> Match | None: ...


def sanitize(name: str) -> str:
    name = name.replace(": ", " - ").replace(":", "-")
    name = re.sub(r'[<>"/\\|?*\x00-\x1f]', "", name)
    return name.strip().rstrip(".") or "_"


def media_folder(m: Match) -> str:
    return sanitize(f"{m.title} ({m.year})" if m.year else m.title)


class Planner:
    def __init__(self, library: Path, finder: Finder | None):
        self.library = library
        self.finder = finder

    def _match(self, g: Guess) -> tuple[Match | None, str | None]:
        if not g.title:
            return None, "no title recognised"
        if self.finder is None:
            return None, "TMDB not configured"
        kind = "movie" if g.kind == "movie" else "tv"
        m = self.finder.find(kind, g.title, g.year)
        return (m, None) if m else (None, f"no confident TMDB match for '{g.title}'")

    def _media_dir(self, g: Guess, m: Match) -> tuple[Path | None, str | None]:
        """Directory a video goes into: movie folder or season folder."""
        if g.kind == "movie":
            return self.library / MOVIES_DIR / media_folder(m), None
        if g.season is None or g.episode is None:
            return None, "episode without season/episode number"
        return self.library / SHOWS_DIR / media_folder(m) / f"Season {g.season:02d}", None

    def _media_root(self, g: Guess, m: Match) -> Path:
        """Folder that owns extras: the movie folder or the show folder."""
        top = MOVIES_DIR if g.kind == "movie" else SHOWS_DIR
        return self.library / top / media_folder(m)

    def plan(self, names: dict[int, str]) -> dict[int, Plan]:
        """`names` maps file index → path relative to the save path (all files of the torrent)."""
        classes = {i: classify(n) for i, n in names.items()}
        plans: dict[int, Plan] = {}
        video_dirs: dict[int, Path] = {}

        for i, n in names.items():
            if classes[i] is not FileClass.VIDEO:
                continue
            g = guess(n)
            m, why = self._match(g)
            d, why = self._media_dir(g, m) if m else (None, why)
            if d is None:
                plans[i] = Plan(Action.REVIEW, reason=why)
                continue
            video_dirs[i] = d
            plans[i] = Plan(Action.MOVE, d / PurePosixPath(n).name)

        for i, n in names.items():
            match classes[i]:
                case FileClass.JUNK:
                    plans[i] = Plan(Action.DELETE)
                case FileClass.OTHER:
                    plans[i] = Plan(Action.REVIEW, reason="unknown file type")
                case FileClass.EXTRA:
                    plans[i] = self._plan_extra(n, names, video_dirs)
                case FileClass.SUBTITLE:
                    plans[i] = self._plan_subtitle(n, names, classes, video_dirs)
        return plans

    def _plan_extra(self, name: str, names: dict[int, str], video_dirs: dict[int, Path]) -> Plan:
        filename = PurePosixPath(name).name
        g = guess(name)  # the path usually names the owning movie/show
        m, _ = self._match(g)
        if m:
            return Plan(Action.MOVE, self._media_root(g, m) / "extras" / filename)
        # Fall back to the largest identified video under the same top-level folder.
        top = PurePosixPath(name).parts[0]
        owners = [i for i in video_dirs if PurePosixPath(names[i]).parts[0] == top]
        if len(owners) == 1:
            d = video_dirs[owners[0]]
            root = d.parent if d.name.startswith("Season ") else d
            return Plan(Action.MOVE, root / "extras" / filename)
        return Plan(Action.REVIEW, reason="extra without an identifiable owner")

    def _plan_subtitle(
        self,
        name: str,
        names: dict[int, str],
        classes: dict[int, FileClass],
        video_dirs: dict[int, Path],
    ) -> Plan:
        video = _pair_subtitle(name, names, classes)
        if video is None:
            return Plan(Action.REVIEW, reason="subtitle without a matching video")
        if video not in video_dirs:
            return Plan(Action.REVIEW, reason="subtitle for an unidentified video")
        video_stem = PurePosixPath(names[video]).stem
        sub = PurePosixPath(name)
        if sub.name.startswith(video_stem):
            filename = sub.name
        else:  # Jellyfin links subtitles by filename prefix
            filename = f"{video_stem}.{sub.stem}{sub.suffix}"
        return Plan(Action.MOVE, video_dirs[video] / filename)


def _pair_subtitle(name: str, names: dict[int, str], classes: dict[int, FileClass]) -> int | None:
    sub = PurePosixPath(name)
    videos = {i: PurePosixPath(n) for i, n in names.items() if classes[i] is FileClass.VIDEO}

    # 1. Same folder, subtitle named after the video: Movie.mkv + Movie.en.srt
    for i, v in videos.items():
        if v.parent == sub.parent and sub.name.startswith(v.stem):
            return i

    # 2. Subs/ folder: Subs/<video stem>/2_English.srt or Subs/English.srt
    parts = sub.parts[:-1]
    subs_at = next((k for k, p in enumerate(parts) if p.lower() in _SUBS_DIRS), None)
    if subs_at is not None:
        base = PurePosixPath(*parts[:subs_at]) if subs_at else PurePosixPath(".")
        nested = parts[subs_at + 1] if len(parts) > subs_at + 1 else None
        if nested:
            for i, v in videos.items():
                if v.stem == nested:
                    return i
        siblings = [i for i, v in videos.items() if v.parent == base]
        if len(siblings) == 1:
            return siblings[0]

    # 3. Only one video in the whole torrent
    if len(videos) == 1:
        return next(iter(videos))
    return None
