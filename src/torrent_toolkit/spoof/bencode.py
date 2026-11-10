"""Minimal bencode decoder that remembers where the `info` dict sits (for the info hash)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


class BencodeError(ValueError):
    pass


class _Decoder:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.info_span: tuple[int, int] | None = None

    def decode(self, depth: int = 0):
        if self.pos >= len(self.data):
            raise BencodeError("unexpected end of data")
        c = self.data[self.pos : self.pos + 1]
        if c == b"i":
            end = self.data.index(b"e", self.pos)
            value = int(self.data[self.pos + 1 : end])
            self.pos = end + 1
            return value
        if c == b"l":
            self.pos += 1
            items = []
            while self.data[self.pos : self.pos + 1] != b"e":
                items.append(self.decode(depth + 1))
            self.pos += 1
            return items
        if c == b"d":
            self.pos += 1
            result: dict[bytes, object] = {}
            while self.data[self.pos : self.pos + 1] != b"e":
                key = self.decode(depth + 1)
                if not isinstance(key, bytes):
                    raise BencodeError("dict key must be a string")
                start = self.pos
                result[key] = self.decode(depth + 1)
                if depth == 0 and key == b"info":
                    self.info_span = (start, self.pos)
            self.pos += 1
            return result
        if c.isdigit():
            colon = self.data.index(b":", self.pos)
            length = int(self.data[self.pos : colon])
            start = colon + 1
            self.pos = start + length
            if self.pos > len(self.data):
                raise BencodeError("string overruns data")
            return self.data[start : self.pos]
        raise BencodeError(f"invalid token {c!r} at {self.pos}")


def decode(data: bytes):
    try:
        return _Decoder(data).decode()
    except (ValueError, IndexError) as e:
        raise BencodeError(str(e)) from e


@dataclass(frozen=True)
class TorrentMeta:
    info_hash: bytes  # v1 SHA-1 of the bencoded info dict
    name: str
    piece_length: int
    total_size: int
    trackers: list[str]  # announce-list in tier order, deduplicated

    @property
    def info_hash_hex(self) -> str:
        return self.info_hash.hex()


def parse_torrent(data: bytes) -> TorrentMeta:
    d = _Decoder(data)
    try:
        root = d.decode()
    except (ValueError, IndexError) as e:
        raise BencodeError(str(e)) from e
    if not isinstance(root, dict) or d.info_span is None:
        raise BencodeError("not a torrent file")
    info = root[b"info"]
    if b"pieces" not in info:
        raise BencodeError("v2-only torrents are not supported")

    if b"length" in info:
        total = info[b"length"]
    else:
        total = sum(f[b"length"] for f in info[b"files"])

    trackers: list[str] = []
    for tier in root.get(b"announce-list") or [[root[b"announce"]]] if b"announce" in root else []:
        for url in tier:
            u = url.decode("utf-8", "replace")
            if u not in trackers:
                trackers.append(u)
    if b"announce" in root and not trackers:
        trackers.append(root[b"announce"].decode("utf-8", "replace"))

    start, end = d.info_span
    return TorrentMeta(
        info_hash=hashlib.sha1(data[start:end]).digest(),
        name=info.get(b"name", b"").decode("utf-8", "replace"),
        piece_length=info[b"piece length"],
        total_size=total,
        trackers=trackers,
    )


def encode(value) -> bytes:
    """For tests and fixtures."""
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(encode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in value.items())
        return b"d" + b"".join(encode(k) + encode(v) for k, v in items) + b"e"
    raise TypeError(type(value))
