import httpx
import pytest
import respx
from test_organizer import FINDER, make_files, tree

from torrent_toolkit.arr import Arr, format_season
from torrent_toolkit.organize.organizer import Organizer

SONARR = "http://sonarr:8989"
RADARR = "http://radarr:7878"


@pytest.mark.parametrize(
    ("fmt", "season", "out"),
    [
        ("Season {season:00}", 3, "Season 03"),
        ("Season {season}", 3, "Season 3"),
        ("S{season:000}", 12, "S012"),
        ("Specials", 0, "Specials"),
    ],
)
def test_format_season(fmt, season, out):
    assert format_season(fmt, season) == out


@pytest.fixture
def arr_mock():
    with respx.mock(assert_all_called=False) as m:
        m.get(f"{RADARR}/api/v3/movie", params={"tmdbId": "949"}).mock(
            return_value=httpx.Response(
                200, json=[{"id": 7, "tmdbId": 949, "path": "/movies/Heat (1995) {tmdb-949}"}]
            )
        )
        m.get(f"{RADARR}/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))
        m.get(f"{SONARR}/api/v3/config/naming").mock(
            return_value=httpx.Response(200, json={"seasonFolderFormat": "Season {season}"})
        )
        m.get(f"{SONARR}/api/v3/series").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": 3, "tmdbId": 1396, "path": "/tv/Breaking Bad", "seasonFolder": True},
                    {"id": 4, "tmdbId": 2316, "path": "/tv/The Office (US)", "seasonFolder": False},
                ],
            )
        )
        m.post(f"{RADARR}/api/v3/command").mock(return_value=httpx.Response(201, json={}))
        m.post(f"{SONARR}/api/v3/command").mock(return_value=httpx.Response(201, json={}))
        yield m


def test_organize_uses_arr_folders_and_rescans(tmp_path, db, arr_mock):
    save, library = tmp_path / "dl", tmp_path / "mount"
    (library / "movies").mkdir(parents=True)
    (library / "shows").mkdir()
    arr = Arr(SONARR, "sk", RADARR, "rk")
    org = Organizer(db, library, tmp_path / "review", FINDER, arr)
    names = make_files(
        save,
        {
            "Heat.1995.mkv": 1,
            "Inception.2010.mkv": 1,  # not in Radarr → our own naming
            "BB/Breaking.Bad.S03E07.mkv": 1,
            "Office/The.Office.S02E05.mkv": 1,
        },
    )
    org.organize("h", str(save), names, list(names))
    assert tree(library) == {
        "movies/Heat (1995) {tmdb-949}/Heat.1995.mkv",
        "movies/Inception (2010)/Inception.2010.mkv",
        "shows/Breaking Bad/Season 3/Breaking.Bad.S03E07.mkv",
        "shows/The Office (US)/The.Office.S02E05.mkv",
    }
    commands = [c.request for c in arr_mock.calls if c.request.method == "POST"]
    bodies = sorted(c.content.decode() for c in commands)
    assert bodies == [
        '{"name":"RescanMovie","movieId":7}',
        '{"name":"RescanSeries","seriesId":3}',
        '{"name":"RescanSeries","seriesId":4}',
    ]
    assert all(c.headers["x-api-key"] in ("sk", "rk") for c in commands)


def test_arr_down_falls_back_to_own_naming(tmp_path, db):
    save, library = tmp_path / "dl", tmp_path / "mount"
    (library / "movies").mkdir(parents=True)
    with respx.mock:
        respx.get(url__startswith=RADARR).mock(side_effect=httpx.ConnectError("down"))
        respx.post(url__startswith=RADARR).mock(side_effect=httpx.ConnectError("down"))
        org = Organizer(
            db, library, tmp_path / "review", FINDER, Arr(radarr_url=RADARR, radarr_key="k")
        )
        names = make_files(save, {"Heat.1995.mkv": 1})
        org.organize("h", str(save), names, list(names))
    assert tree(library) == {"movies/Heat (1995)/Heat.1995.mkv"}


def test_disabled_without_keys():
    assert not Arr().enabled
    assert not Arr(sonarr_url=SONARR).enabled
    assert Arr(sonarr_url=SONARR, sonarr_key="k").enabled
