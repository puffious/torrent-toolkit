from pathlib import Path

import pytest

from torrent_toolkit import config
from torrent_toolkit.config import (
    ConfigError,
    parse_duration,
    parse_percent,
    parse_size,
    parse_speed,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2GB", 2 * 1024**3),
        ("500mb", 500 * 1024**2),
        ("1.5TiB", int(1.5 * 1024**4)),
        ("1024", 1024),
        ("10 kb", 10240),
        (4096, 4096),
    ],
)
def test_parse_size(raw, expected):
    assert parse_size(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1mbps", 1024**2), ("800kbps", 800 * 1024), ("0kbps", 0), ("2.5MBps", int(2.5 * 1024**2))],
)
def test_parse_speed(raw, expected):
    assert parse_speed(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [("40%", 0.4), ("0.25", 0.25), (0.1, 0.1)])
def test_parse_percent(raw, expected):
    assert parse_percent(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("14d", 14 * 86400), ("1d12h", 129600), ("90s", 90), ("3600", 3600), ("2w", 1209600)],
)
def test_parse_duration(raw, expected):
    assert parse_duration(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "12x", "1d junk", ""])
def test_parse_duration_invalid(raw):
    with pytest.raises(ConfigError):
        parse_duration(raw)


@pytest.mark.parametrize("parse", [parse_size, parse_speed])
def test_parse_size_speed_invalid(parse):
    with pytest.raises(ConfigError):
        parse("lots")


def test_defaults(tmp_path):
    s = config.load({"TK_DATA_DIR": str(tmp_path)})
    assert s.split_reserve == 2 * 1024**3
    assert s.spoof_speed == 1024**2
    assert s.spoof_jitter == 0.4
    assert s.spoof_max_duration == 14 * 86400
    assert s.notify_urls == []
    assert s.db_path == tmp_path / "toolkit.db"


def test_env_overrides_yaml(tmp_path):
    (tmp_path / "config.yml").write_text(
        "qbit_url: http://yaml:8080\nsplit_reserve: 5GB\nnotify_urls: [ntfy://a, ntfy://b]\n"
    )
    s = config.load(
        {
            "TK_DATA_DIR": str(tmp_path),
            "TK_QBIT_URL": "http://env:8080",
            "TK_LIBRARY_PATH": "/lib",
            "TK_HTTP_PORT": "9000",
        }
    )
    assert s.qbit_url == "http://env:8080"
    assert s.split_reserve == 5 * 1024**3
    assert s.notify_urls == ["ntfy://a", "ntfy://b"]
    assert s.library_path == Path("/lib")
    assert s.http_port == 9000


def test_notify_urls_from_env(tmp_path):
    s = config.load({"TK_DATA_DIR": str(tmp_path), "TK_NOTIFY_URLS": "discord://x, ntfy://y"})
    assert s.notify_urls == ["discord://x", "ntfy://y"]


def test_unknown_yaml_key(tmp_path):
    (tmp_path / "config.yml").write_text("bogus: 1\n")
    with pytest.raises(ConfigError, match="bogus"):
        config.load({"TK_DATA_DIR": str(tmp_path)})


def test_validate(tmp_path):
    with pytest.raises(ConfigError, match="TK_QBIT_URL"):
        config.load({"TK_DATA_DIR": str(tmp_path)}).validate()
    with pytest.raises(ConfigError, match="JITTER"):
        config.load(
            {
                "TK_DATA_DIR": str(tmp_path),
                "TK_QBIT_URL": "http://q",
                "TK_SPOOF_JITTER": "150%",
            }
        ).validate()
