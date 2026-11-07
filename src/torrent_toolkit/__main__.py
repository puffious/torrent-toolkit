from __future__ import annotations

import logging
import os
import sys

from torrent_toolkit import __version__, config
from torrent_toolkit import log as tklog

log = logging.getLogger("torrent_toolkit")


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
        extra={
            "version": __version__,
            "qbit_url": settings.qbit_url,
            "data_dir": settings.data_dir,
            "library": settings.library_path,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
