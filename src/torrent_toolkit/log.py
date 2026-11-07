"""Structured key=value logging to stdout.

Usage: `log.info("batch started", extra={"hash": h, "batch": 2})`
"""

from __future__ import annotations

import logging
import sys
import time

_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


def _fmt_value(value: object) -> str:
    s = str(value)
    return f'"{s}"' if not s or any(c in s for c in ' ="') else s


class KeyValueFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ")
        parts = [ts, record.levelname, record.name, _fmt_value(record.getMessage())]
        parts += [
            f"{k}={_fmt_value(v)}" for k, v in vars(record).items() if k not in _STANDARD_ATTRS
        ]
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(KeyValueFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
