import pytest

from torrent_toolkit.organize.classify import FileClass, classify
from torrent_toolkit.organize.identify import Guess, guess


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("The.Matrix.1999.1080p/The.Matrix.1999.1080p.mkv", FileClass.VIDEO),
        ("The.Matrix.1999.1080p/Sample/sample-matrix.mkv", FileClass.JUNK),
        ("The.Matrix.1999.1080p/the.matrix.1999.sample.mkv", FileClass.JUNK),
        ("The.Matrix.1999.1080p/Featurettes/Making of.mkv", FileClass.EXTRA),
        ("The.Matrix.1999.1080p/Behind The Scenes/x.mkv", FileClass.EXTRA),
        ("Movie.2010/Movie.2010-trailer.mp4", FileClass.EXTRA),
        ("The.Matrix.1999.1080p/Subs/English.srt", FileClass.SUBTITLE),
        ("The.Matrix.1999.1080p/RARBG.txt", FileClass.JUNK),
        ("The.Matrix.1999.1080p/movie.nfo", FileClass.JUNK),
        ("The.Matrix.1999.1080p/poster.jpg", FileClass.JUNK),
        ("The.Matrix.1999.1080p/setup.exe", FileClass.JUNK),
        ("Album/track.flac", FileClass.OTHER),
        ("Samples Of Life (2020)/Samples.Of.Life.2020.mkv", FileClass.VIDEO),
    ],
)
def test_classify(path, expected):
    assert classify(path) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "The.Matrix.1999.1080p.BluRay/The.Matrix.1999.1080p.BluRay.mkv",
            Guess("movie", "The Matrix", 1999),
        ),
        (
            "200 Movies Pack/Inception (2010) [1080p]/Inception.2010.1080p.mkv",
            Guess("movie", "Inception", 2010),
        ),
        ("200 Movies Pack/Heat.1995.REMASTERED.720p.mkv", Guess("movie", "Heat", 1995)),
        ("Heat.1995.mkv", Guess("movie", "Heat", 1995)),
        (
            "Breaking.Bad.S01-S05.1080p/Season 3/Breaking.Bad.S03E07.One.Minute.1080p.mkv",
            Guess("episode", "Breaking Bad", None, 3, 7),
        ),
        (
            "The Office (US) Complete/Season 2/02x05 - Halloween.mkv",
            Guess("episode", "The Office", None, 2, 5, country="US"),
        ),
        ("Show.Name.S02.1080p/Show.Name.S02E01E02.mkv", Guess("episode", "Show Name", None, 2, 1)),
        ("Some.Show.S01.1080p/01.mkv", Guess("episode", "Some Show", None, 1, 1)),
        (
            "[SubsPlease] Frieren - 12 (1080p) [ABCD].mkv",
            Guess("episode", "Frieren", None, None, 12),
        ),
    ],
)
def test_guess(path, expected):
    assert guess(path) == expected
