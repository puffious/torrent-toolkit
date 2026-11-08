from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import uvicorn

from torrent_toolkit import __version__, config
from torrent_toolkit import log as tklog
from torrent_toolkit.api import create_app
from torrent_toolkit.daemon import Daemon
from torrent_toolkit.db import Database
from torrent_toolkit.notify import Notifier
from torrent_toolkit.qbit import Qbit

log = logging.getLogger("torrent_toolkit")


async def run(settings: config.Settings) -> None:
    db = Database(settings.db_path)
    qbit = Qbit(settings.qbit_url, settings.qbit_user, settings.qbit_pass)
    daemon = Daemon(db, qbit, Notifier(settings.notify_urls), settings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(db),
            host="0.0.0.0",
            port=settings.http_port,
            log_config=None,
            access_log=False,
        )
    )
    server.install_signal_handlers = lambda: None  # we own signal handling
    api_task = asyncio.create_task(server.serve())

    await daemon.run(stop)
    server.should_exit = True
    await api_task
    db.close()
    log.info("stopped")


def main() -> int:
    tklog.setup(os.environ.get("TK_LOG_LEVEL", "INFO"))
    try:
        settings = config.load()
        settings.validate()
    except config.ConfigError as e:
        log.error("invalid configuration", extra={"error": str(e)})
        return 2

    log.info(
        "torrent-toolkit starting",
        extra={"version": __version__, "qbit_url": settings.qbit_url, "port": settings.http_port},
    )
    asyncio.run(run(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
