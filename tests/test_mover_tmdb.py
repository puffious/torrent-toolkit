import errno
import os

import httpx
import pytest
import respx

from torrent_toolkit.organize import mover
from torrent_toolkit.organize.mover import Collision, move_file, move_to_review, prune_empty_dirs
from torrent_toolkit.organize.tmdb import API, Tmdb, normalize, pick


def test_cross_device_move_uses_tmp_then_rename(tmp_path, monkeypatch):
    src = tmp_path / "a" / "f.mkv"
    src.parent.mkdir()
    src.write_bytes(b"data")
    dest = tmp_path / "lib" / "x" / "f.mkv"
    seen = []

    def no_rename(a, b):
        raise OSError(errno.EXDEV, "cross-device")

    real_replace = os.replace

    def spy_replace(a, b):
        seen.append((str(a), str(b)))
        real_replace(a, b)

    monkeypatch.setattr(mover.os, "rename", no_rename)
    monkeypatch.setattr(mover.os, "replace", spy_replace)
    assert move_file(src, dest) == "moved"
    assert dest.read_bytes() == b"data" and not src.exists()
    assert seen == [(str(dest) + ".tk-tmp", str(dest))]


def test_failed_copy_leaves_no_tmp(tmp_path, monkeypatch):
    src = tmp_path / "f.mkv"
    src.write_bytes(b"data")
    dest = tmp_path / "lib" / "f.mkv"
    monkeypatch.setattr(
        mover.os, "rename", lambda a, b: (_ for _ in ()).throw(OSError(errno.EXDEV, "x"))
    )
    monkeypatch.setattr(
        mover.shutil,
        "copyfile",
        lambda a, b: (_ for _ in ()).throw(OSError(errno.EIO, "mount gone")),
    )
    with pytest.raises(OSError):
        move_file(src, dest)
    assert src.exists() and not list((tmp_path / "lib").iterdir())


def test_collision_and_review_suffix(tmp_path):
    src = tmp_path / "f.mkv"
    src.write_bytes(b"new")
    dest = tmp_path / "d" / "f.mkv"
    dest.parent.mkdir()
    dest.write_bytes(b"different")
    with pytest.raises(Collision):
        move_file(src, dest)
    assert move_to_review(src, dest) == tmp_path / "d" / "f.1.mkv"


def test_prune_stops_at_base(tmp_path):
    deep = tmp_path / "base" / "a" / "b"
    deep.mkdir(parents=True)
    prune_empty_dirs(deep, tmp_path / "base")
    assert (tmp_path / "base").exists() and not (tmp_path / "base" / "a").exists()


def test_normalize():
    assert normalize("The Office (US)") == normalize("the office us")
    assert normalize("Amélie") == "amelie"
    assert normalize("Fast & Furious") == "fast and furious"


def test_pick_year_tolerance_and_title():
    results = [
        {"id": 1, "title": "Dune", "original_title": "Dune", "release_date": "1984-12-14"},
        {"id": 2, "title": "Dune", "original_title": "Dune", "release_date": "2021-09-15"},
        {"id": 3, "title": "Dune: Part Two", "release_date": "2024-02-27"},
    ]
    assert pick("movie", "Dune", 2021, results).tmdb_id == 2
    assert pick("movie", "Dune", 2022, results).tmdb_id == 2  # ±1
    assert pick("movie", "Dune", None, results).tmdb_id == 1  # relevance order
    assert pick("movie", "Dune", 2000, results) is None
    assert pick("movie", "Completely Else", None, results) is None


@respx.mock(base_url=API)
def test_tmdb_search_and_cache(respx_mock, db):
    route = respx_mock.get("/search/movie").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": 603, "title": "The Matrix", "release_date": "1999-03-30"}]},
        )
    )
    tmdb = Tmdb("k", db)
    m = tmdb.find("movie", "The Matrix", 1999)
    assert (m.tmdb_id, m.title, m.year) == (603, "The Matrix", 1999)
    assert route.calls[0].request.url.params["api_key"] == "k"
    assert route.calls[0].request.url.params["year"] == "1999"
    tmdb.find("movie", "the matrix", 1999)
    assert route.call_count == 1  # cached


@respx.mock(base_url=API)
def test_tmdb_miss_cached_and_year_fallback(respx_mock, db):
    def handler(request):
        if "year" in request.url.params:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(
            200, json={"results": [{"id": 9, "title": "Heat", "release_date": "1995-12-15"}]}
        )

    route = respx_mock.get("/search/movie").mock(side_effect=handler)
    tmdb = Tmdb("k", db)
    assert tmdb.find("movie", "Heat", 1996).tmdb_id == 9  # year off by one in release name
    assert tmdb.find("movie", "Nothing", None) is None
    tmdb.find("movie", "Nothing", None)
    assert route.call_count == 3  # 2 for Heat, 1 for the cached miss


@respx.mock(base_url=API)
def test_tmdb_bearer_token(respx_mock, db):
    route = respx_mock.get("/search/tv").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20"}]
            },
        )
    )
    Tmdb("eyJhbGciOi.token", db).find("tv", "Breaking Bad", None)
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer eyJhbGciOi.token"
    assert "api_key" not in req.url.params


@respx.mock(base_url=API)
def test_tmdb_network_error_raises_and_is_not_cached(respx_mock, db):
    respx_mock.get("/search/movie").mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        Tmdb("k", db).find("movie", "Heat", 1995)
    assert db.tmdb_cache_get("movie", "heat", 1995, max_age=1e9) == (False, None)
