"""TMDB lookup with confidence rules and an SQLite cache."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher

import httpx

from torrent_toolkit.db import Database

log = logging.getLogger(__name__)

API = "https://api.themoviedb.org/3"
SIMILARITY = 0.85
HIT_TTL = 30 * 86400
MISS_TTL = 3 * 86400


@dataclass(frozen=True)
class Match:
    kind: str  # "movie" | "tv"
    tmdb_id: int
    title: str
    year: int | None


def normalize(title: str) -> str:
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"^(the|a|an) ", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def _similar(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    return bool(na) and (na == nb or SequenceMatcher(None, na, nb).ratio() >= SIMILARITY)


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


def pick(kind: str, title: str, year: int | None, results: list[dict]) -> Match | None:
    """First result (TMDB relevance order) whose title matches and whose year is ±1."""
    for r in results:
        if kind == "movie":
            names = [r.get("title"), r.get("original_title")]
            r_year = _year(r.get("release_date"))
        else:
            names = [r.get("name"), r.get("original_name")]
            r_year = _year(r.get("first_air_date"))
        if not any(n and _similar(title, n) for n in names):
            continue
        if year is not None and (r_year is None or abs(r_year - year) > 1):
            continue
        return Match(kind=kind, tmdb_id=r["id"], title=names[0] or title, year=r_year)
    return None


class Tmdb:
    def __init__(self, api_key: str, db: Database, client: httpx.Client | None = None):
        self.db = db
        if api_key.startswith("eyJ"):  # v4 read access token
            headers, self._params = {"Authorization": f"Bearer {api_key}"}, {}
        else:
            headers, self._params = {}, {"api_key": api_key}
        self._client = client or httpx.Client(base_url=API, headers=headers, timeout=20)
        if client is not None:
            self._client.headers.update(headers)

    def find(self, kind: str, title: str, year: int | None) -> Match | None:
        """`kind` is "movie" or "tv". Raises httpx errors on network failure."""
        key = (kind, normalize(title), year or 0)
        hit, cached = self.db.tmdb_cache_get(*key, max_age=HIT_TTL)
        if hit and cached is not None:
            return Match(**json.loads(cached))
        hit, _ = self.db.tmdb_cache_get(*key, max_age=MISS_TTL)
        if hit:
            return None

        match = self._search(kind, title, year)
        if match is None and year is not None:
            match = self._search(kind, title, None)  # wrong year in the release name
            if match and match.year is not None and abs(match.year - year) > 1:
                match = None
        self.db.tmdb_cache_put(*key, json.dumps(asdict(match)) if match else None)
        log.info(
            "tmdb lookup",
            extra={"kind": kind, "title": title, "year": year, "match": match and match.title},
        )
        return match

    def _search(self, kind: str, title: str, year: int | None) -> Match | None:
        params = {**self._params, "query": title, "include_adult": "false"}
        if year is not None:
            params["year" if kind == "movie" else "first_air_date_year"] = str(year)
        resp = self._client.get(f"/search/{kind}", params=params)
        resp.raise_for_status()
        return pick(kind, title, year, resp.json().get("results", []))
