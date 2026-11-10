"""HTTP(S) tracker announces."""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass

import httpx

from torrent_toolkit.spoof.bencode import BencodeError, decode
from torrent_toolkit.spoof.emulation import Identity, announce_url

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 1800


class AnnounceError(Exception):
    pass


@dataclass(frozen=True)
class AnnounceResponse:
    interval: int
    min_interval: int | None
    seeders: int | None
    leechers: int | None


def http_trackers(urls: list[str]) -> list[str]:
    return [u for u in urls if u.lower().startswith(("http://", "https://"))]


def parse_response(body: bytes) -> AnnounceResponse:
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    try:
        d = decode(body)
    except BencodeError as e:
        raise AnnounceError(f"invalid tracker response: {e}") from e
    if not isinstance(d, dict):
        raise AnnounceError("invalid tracker response")
    if reason := d.get(b"failure reason"):
        raise AnnounceError(f"tracker: {reason.decode('utf-8', 'replace')}")
    interval = d.get(b"interval")
    return AnnounceResponse(
        interval=interval if isinstance(interval, int) and interval > 0 else DEFAULT_INTERVAL,
        min_interval=d.get(b"min interval"),
        seeders=d.get(b"complete"),
        leechers=d.get(b"incomplete"),
    )


async def announce(
    client: httpx.AsyncClient, trackers: list[str], identity: Identity, query: str
) -> tuple[AnnounceResponse, str]:
    """Try trackers in order; returns the response and the tracker that answered."""
    errors = []
    for base in trackers:
        try:
            resp = await client.get(announce_url(base, query), headers=identity.headers)
            if resp.status_code != 200:
                raise AnnounceError(f"HTTP {resp.status_code}")
            return parse_response(resp.content), base
        except (httpx.HTTPError, AnnounceError, OSError) as e:
            errors.append(f"{_redact(base)}: {e}")
    raise AnnounceError("; ".join(errors) or "no HTTP tracker")


def _redact(url: str) -> str:
    """Hide passkeys in logs/notifications."""
    u = httpx.URL(url)
    return f"{u.scheme}://{u.host}{':' + str(u.port) if u.port else ''}/…"
