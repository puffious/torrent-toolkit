import gzip
import re
from pathlib import Path

import httpx
import pytest
import respx

from torrent_toolkit.spoof.announcer import (
    DEFAULT_INTERVAL,
    AnnounceError,
    announce,
    http_trackers,
    parse_response,
)
from torrent_toolkit.spoof.bencode import BencodeError, decode, encode, parse_torrent
from torrent_toolkit.spoof.emulation import (
    Identity,
    announce_url,
    build_query,
    peer_id_prefix,
    round_uploaded,
    url_encode_bytes,
)

DEBIAN = Path(__file__).parent / "debian-12.torrent"


def test_parse_real_torrent():
    m = parse_torrent(DEBIAN.read_bytes())
    assert m.name == "debian-12.0.0-amd64-DVD-1.iso"
    assert m.piece_length == 262144
    assert m.total_size == 3931095040
    assert m.trackers == ["http://bttracker.debian.org:6969/announce"]
    assert m.info_hash_hex == "b1680a55cfc8693c6c02de732dd17c33e251e8e5"


def test_parse_multi_file_and_announce_list():
    data = encode(
        {
            "announce": "http://a/announce",
            "announce-list": [["http://a/announce", "udp://b:1"], ["https://c/announce"]],
            "info": {
                "name": "x",
                "piece length": 16384,
                "pieces": b"\0" * 20,
                "files": [{"length": 5, "path": ["a"]}, {"length": 7, "path": ["b"]}],
            },
        }
    )
    m = parse_torrent(data)
    assert m.total_size == 12
    assert m.trackers == ["http://a/announce", "udp://b:1", "https://c/announce"]


def test_v2_only_rejected():
    with pytest.raises(BencodeError, match="v2"):
        parse_torrent(encode({"info": {"name": "x", "piece length": 1, "meta version": 2}}))


@pytest.mark.parametrize("bad", [b"", b"d3:foo", b"x", b"i12"])
def test_bencode_errors(bad):
    with pytest.raises(BencodeError):
        decode(bad)


@pytest.mark.parametrize(
    ("version", "prefix"),
    [("5.0.2", "-qB5020-"), ("4.3.3", "-qB4330-"), ("4.6.10", "-qB46A0-"), ("5.1", "-qB5100-")],
)
def test_peer_id_prefix(version, prefix):
    assert peer_id_prefix(version) == prefix


def test_identity_format():
    ident = Identity.generate("5.0.2", 51413)
    assert re.fullmatch(r"-qB5020-[A-Za-z0-9_~()!.*-]{12}", ident.peer_id)
    assert re.fullmatch(r"[0-9A-F]{8}", ident.key)
    assert ident.headers == {
        "User-Agent": "qBittorrent/5.0.2",
        "Accept-Encoding": "gzip",
        "Connection": "close",
    }


def test_query_matches_ratio_spoof_order():
    ident = Identity("-qB5020-abcdefghijkl", "DEADBEEF", 51413, "qBittorrent/5.0.2")
    q = build_query(
        ident, bytes.fromhex("b1680a55cfc8693c6c02de732dd17c33e251e8e5"), 1000, 2000, 0, "started"
    )
    assert q == (
        "info_hash=%b1h%0aU%cf%c8i%3cl%02%des-%d1%7c3%e2Q%e8%e5&peer_id=-qB5020-abcdefghijkl"
        "&port=51413&uploaded=1000&downloaded=2000&left=0&corrupt=0&key=DEADBEEF"
        "&event=started&numwant=200&compact=1&no_peer_id=1&supportcrypto=1&redundant=0"
    )
    assert "event=" not in build_query(ident, b"\0" * 20, 1, 2, 0, None)


def test_url_helpers():
    assert url_encode_bytes(b"a.-_~ \xff") == "a.-_~%20%ff"
    assert announce_url("http://t/a?passkey=x", "q=1") == "http://t/a?passkey=x&q=1"
    assert announce_url("http://t/a", "q=1") == "http://t/a?q=1"
    assert round_uploaded(16 * 1024 * 3 + 5) == 16 * 1024 * 3
    assert http_trackers(["udp://a", "http://b", "HTTPS://c", "wss://d"]) == [
        "http://b",
        "HTTPS://c",
    ]


def test_parse_response():
    r = parse_response(encode({"interval": 900, "complete": 5, "incomplete": 2, "peers": b""}))
    assert (r.interval, r.seeders, r.leechers) == (900, 5, 2)
    assert parse_response(encode({"peers": b""})).interval == DEFAULT_INTERVAL
    assert parse_response(gzip.compress(encode({"interval": 60}))).interval == 60
    with pytest.raises(AnnounceError, match="unregistered"):
        parse_response(encode({"failure reason": "unregistered torrent"}))
    with pytest.raises(AnnounceError):
        parse_response(b"<html>oops</html>")


@respx.mock
async def test_announce_falls_back_to_next_tracker():
    respx.get(url__startswith="http://down/").mock(side_effect=httpx.ConnectError("refused"))
    ok = respx.get(url__startswith="https://up/").mock(
        return_value=httpx.Response(200, content=encode({"interval": 120}))
    )
    ident = Identity("-qB5020-abcdefghijkl", "DEADBEEF", 1, "qBittorrent/5.0.2")
    async with httpx.AsyncClient() as client:
        client.headers.clear()
        resp, used = await announce(
            client, ["http://down/a?pk=SECRET", "https://up/a"], ident, "x=1"
        )
    assert used == "https://up/a" and resp.interval == 120
    sent = ok.calls[0].request
    assert sent.headers["user-agent"] == "qBittorrent/5.0.2"
    assert "accept" not in sent.headers  # no httpx defaults leak


@respx.mock
async def test_announce_error_hides_passkey():
    respx.get(url__startswith="http://t/").mock(return_value=httpx.Response(500))
    ident = Identity("-qB5020-abcdefghijkl", "DEADBEEF", 1, "qBittorrent/5.0.2")
    async with httpx.AsyncClient() as client:
        with pytest.raises(AnnounceError) as e:
            await announce(client, ["http://t/announce?passkey=SECRET"], ident, "x=1")
    assert "SECRET" not in str(e.value) and "HTTP 500" in str(e.value)
