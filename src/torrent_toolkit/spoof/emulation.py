"""Announce identity and query format, ported from ratio-spoof's `qbit-4.3.3` profile.

The client version and port are copied from the connected qBittorrent; everything else
(parameter order, key format, peer-id alphabet, headers, rounding) follows ratio-spoof.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

# ratio-spoof: -qB4330-[A-Za-z0-9_~\(\)\!\.\*-]{12}
PEER_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_~()!.*-"
NUMWANT = 200
UPLOAD_ROUNDING = 16 * 1024
_UNRESERVED = re.compile(rb"[A-Za-z0-9.\-_~]")


def _version_char(n: int) -> str:
    """libtorrent fingerprint digit: 0-9, then A-Z for 10+."""
    return str(n) if n < 10 else chr(ord("A") + n - 10)


def peer_id_prefix(version: str) -> str:
    parts = [int(p) for p in re.findall(r"\d+", version)[:3]]
    parts += [0] * (3 - len(parts))
    return "-qB" + "".join(_version_char(p) for p in parts) + "0-"


@dataclass(frozen=True)
class Identity:
    peer_id: str
    key: str
    port: int
    user_agent: str

    @classmethod
    def generate(cls, version: str, port: int) -> Identity:
        suffix = "".join(secrets.choice(PEER_ID_ALPHABET) for _ in range(12))
        return cls(
            peer_id=peer_id_prefix(version) + suffix,
            key=secrets.token_hex(4).upper(),
            port=port,
            user_agent=f"qBittorrent/{version}",
        )

    @property
    def headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept-Encoding": "gzip", "Connection": "close"}


def url_encode_bytes(raw: bytes) -> str:
    return "".join(chr(b) if _UNRESERVED.fullmatch(bytes([b])) else f"%{b:02x}" for b in raw)


def build_query(
    identity: Identity,
    info_hash: bytes,
    uploaded: int,
    downloaded: int,
    left: int,
    event: str | None,
    numwant: int = NUMWANT,
) -> str:
    """Same parameter order as ratio-spoof; `event` is omitted on regular announces."""
    parts = [
        f"info_hash={url_encode_bytes(info_hash)}",
        f"peer_id={identity.peer_id}",
        f"port={identity.port}",
        f"uploaded={uploaded}",
        f"downloaded={downloaded}",
        f"left={left}",
        "corrupt=0",
        f"key={identity.key}",
    ]
    if event:
        parts.append(f"event={event}")
    parts += [f"numwant={numwant}", "compact=1", "no_peer_id=1", "supportcrypto=1", "redundant=0"]
    return "&".join(parts)


def announce_url(base: str, query: str) -> str:
    return f"{base}&{query}" if "?" in base else f"{base}?{query}"


def round_uploaded(value: int) -> int:
    return value - value % UPLOAD_ROUNDING
