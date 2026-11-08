from pathlib import Path

import pytest

from torrent_toolkit.organize.layout import sanitize
from torrent_toolkit.organize.organizer import LibraryUnavailable, Organizer
from torrent_toolkit.organize.tmdb import Match, normalize


class FakeFinder:
    def __init__(self, entries):
        # (kind, normalized title) → (title, year, id)
        self.entries = {(k, normalize(t)): (t, y, i) for k, t, y, i in entries}
        self.calls = []

    def find(self, kind, title, year):
        self.calls.append((kind, title, year))
        hit = self.entries.get((kind, normalize(title)))
        if hit is None:
            return None
        t, y, i = hit
        if year is not None and abs(y - year) > 1:
            return None
        return Match(kind, i, t, y)


FINDER = FakeFinder(
    [
        ("movie", "The Matrix", 1999, 603),
        ("movie", "Inception", 2010, 27205),
        ("movie", "Heat", 1995, 949),
        ("movie", "Mission: Impossible", 1996, 954),
        ("tv", "Breaking Bad", 2008, 1396),
        ("tv", "The Office", 2005, 2316),
    ]
)


@pytest.fixture
def env(tmp_path, db):
    save = tmp_path / "downloads"
    library = tmp_path / "mount"
    review = tmp_path / "review"
    (library / "movies").mkdir(parents=True)
    (library / "shows").mkdir()
    organizer = Organizer(db, library, review, FINDER)
    return save, library, review, organizer


def make_files(save: Path, files: dict[str, int]) -> dict[int, str]:
    for rel, size in files.items():
        p = save / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    return dict(enumerate(files))


def tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_single_movie_with_extras_subs_and_junk(env):
    save, library, review, org = env
    names = make_files(
        save,
        {
            "The.Matrix.1999.1080p/The.Matrix.1999.1080p.mkv": 100,
            "The.Matrix.1999.1080p/The.Matrix.1999.1080p.en.srt": 5,
            "The.Matrix.1999.1080p/Subs/French.srt": 5,
            "The.Matrix.1999.1080p/Sample/sample.mkv": 10,
            "The.Matrix.1999.1080p/Featurettes/Making of.mkv": 20,
            "The.Matrix.1999.1080p/RARBG.txt": 1,
            "The.Matrix.1999.1080p/movie.nfo": 1,
        },
    )
    result = org.organize("h", str(save), names, list(names))
    assert tree(library) == {
        "movies/The Matrix (1999)/The.Matrix.1999.1080p.mkv",
        "movies/The Matrix (1999)/The.Matrix.1999.1080p.en.srt",
        "movies/The Matrix (1999)/The.Matrix.1999.1080p.French.srt",
        "movies/The Matrix (1999)/extras/Making of.mkv",
    }
    assert result.moved == 4 and result.deleted == 3 and result.review == []
    assert not (save / "The.Matrix.1999.1080p").exists()  # emptied dirs pruned
    assert save.exists()


def test_movie_pack(env):
    save, library, review, org = env
    names = make_files(
        save,
        {
            "200 Movies/Inception (2010) [1080p]/Inception.2010.1080p.mkv": 10,
            "200 Movies/Heat.1995.REMASTERED.720p.mkv": 10,
            "200 Movies/Mission.Impossible.1996.mkv": 10,
            "200 Movies/Totally.Unknown.Film.2003.mkv": 10,
        },
    )
    result = org.organize("h", str(save), names, list(names))
    assert tree(library) == {
        "movies/Inception (2010)/Inception.2010.1080p.mkv",
        "movies/Heat (1995)/Heat.1995.REMASTERED.720p.mkv",
        "movies/Mission - Impossible (1996)/Mission.Impossible.1996.mkv",
    }
    assert tree(review) == {"200 Movies/Totally.Unknown.Film.2003.mkv"}
    assert result.review[0][1].startswith("no confident TMDB match")


def test_multi_season_show_pack(env):
    save, library, review, org = env
    names = make_files(
        save,
        {
            "Breaking.Bad.S01-S05.1080p/Season 1/Breaking.Bad.S01E01.Pilot.1080p.mkv": 10,
            "Breaking.Bad.S01-S05.1080p/Season 3/Breaking.Bad.S03E07.One.Minute.1080p.mkv": 10,
            "Breaking.Bad.S01-S05.1080p/Subs/Breaking.Bad.S03E07.One.Minute.1080p/2_English.srt": 1,
            "The Office (US) Complete/Season 2/02x05 - Halloween.mkv": 10,
        },
    )
    org.organize("h", str(save), names, list(names))
    assert tree(library) == {
        "shows/Breaking Bad (2008)/Season 01/Breaking.Bad.S01E01.Pilot.1080p.mkv",
        "shows/Breaking Bad (2008)/Season 03/Breaking.Bad.S03E07.One.Minute.1080p.mkv",
        "shows/Breaking Bad (2008)/Season 03/Breaking.Bad.S03E07.One.Minute.1080p.2_English.srt",
        "shows/The Office (2005)/Season 02/02x05 - Halloween.mkv",
    }


def test_episode_without_season_goes_to_review(env):
    save, library, review, org = env
    names = make_files(save, {"[SubsPlease] Breaking Bad - 12 (1080p).mkv": 10})
    result = org.organize("h", str(save), names, list(names))
    assert tree(library) == set()
    assert tree(review) == {
        "[SubsPlease] Breaking Bad - 12 (1080p)/[SubsPlease] Breaking Bad - 12 (1080p).mkv"
    }
    assert "season" in result.review[0][1]


def test_unknown_types_reviewed_not_deleted(env):
    save, library, review, org = env
    names = make_files(save, {"Heat.1995/Heat.1995.mkv": 10, "Heat.1995/soundtrack.flac": 3})
    org.organize("h", str(save), names, list(names))
    assert tree(review) == {"Heat.1995/soundtrack.flac"}


def test_collisions(env):
    save, library, review, org = env
    dest = library / "movies/Heat (1995)"
    dest.mkdir(parents=True)
    (dest / "Heat.1995.mkv").write_bytes(b"x" * 10)  # same size → duplicate
    (dest / "Heat.1995.extended.mkv").write_bytes(b"x" * 99)  # different size → review
    names = make_files(
        save,
        {
            "Heat/Heat.1995.mkv": 10,
            "Heat/Heat.1995.extended.mkv": 10,
            "Heat/Heat.1995.2160p.mkv": 10,  # other quality → kept alongside
        },
    )
    result = org.organize("h", str(save), names, list(names))
    assert result.duplicates == 1 and result.moved == 1
    assert tree(review) == {"Heat/Heat.1995.extended.mkv"}
    assert (dest / "Heat.1995.2160p.mkv").exists()
    assert (dest / "Heat.1995.extended.mkv").stat().st_size == 99


def test_library_unavailable(env, tmp_path, db):
    save, library, review, _ = env
    org = Organizer(db, tmp_path / "not-mounted", review, FINDER)
    names = make_files(save, {"Heat.1995.mkv": 10})
    with pytest.raises(LibraryUnavailable):
        org.organize("h", str(save), names, list(names))
    assert (save / "Heat.1995.mkv").exists()


def test_rerun_is_idempotent(env):
    save, library, review, org = env
    names = make_files(save, {"Heat.1995/Heat.1995.mkv": 10})
    org.organize("h", str(save), names, list(names))
    result = org.organize("h", str(save), names, list(names))
    assert result.moved == 0 and result.review == []


def test_split_batches_pair_subtitles_across_batches(env):
    save, library, review, org = env
    files = {
        "Heat.1995/Heat.1995.mkv": 10,
        "Heat.1995/Heat.1995.en.srt": 1,
        "Inception.2010/Inception.2010.mkv": 10,
    }
    names = make_files(save, files)
    org.organize("h", str(save), names, [1])  # subtitle batch first; video comes later
    assert tree(library) == {"movies/Heat (1995)/Heat.1995.en.srt"}
    org.organize("h", str(save), names, [0, 2])
    assert len(tree(library)) == 3


def test_no_tmdb_means_review(env, db):
    save, library, review, _ = env
    org = Organizer(db, library, review, None)
    names = make_files(save, {"Heat.1995.mkv": 10})
    result = org.organize("h", str(save), names, list(names))
    assert result.review[0][1] == "TMDB not configured"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Mission: Impossible (1996)", "Mission - Impossible (1996)"),
        ('A/B "C"?', "AB C"),
        ("Dots...", "Dots"),
    ],
)
def test_sanitize(raw, clean):
    assert sanitize(raw) == clean
