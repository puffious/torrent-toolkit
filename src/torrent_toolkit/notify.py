"""Apprise notifications; a no-op when no URLs are configured."""

from __future__ import annotations

import logging

import apprise

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, urls: list[str]):
        self._apprise: apprise.Apprise | None = None
        if urls:
            self._apprise = apprise.Apprise()
            for url in urls:
                if not self._apprise.add(url):
                    log.warning("invalid notification url ignored", extra={"url": url})

    def send(self, title: str, body: str) -> None:
        """Blocking; failures are logged, never raised."""
        log.info("notify", extra={"title": title, "body": body})
        if self._apprise is None:
            return
        try:
            if not self._apprise.notify(title=f"torrent-toolkit: {title}", body=body):
                log.warning("notification failed", extra={"title": title})
        except Exception:
            log.exception("notification error", extra={"title": title})
