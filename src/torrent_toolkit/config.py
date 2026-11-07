"""Settings: `<data_dir>/config.yml`, overridden by `TK_*` environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


_SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_size(value: str | int) -> int:
    """`2GB`, `500mb`, `1.5TiB`, `1024` → bytes (binary units)."""
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgt]?)(i?b)?\s*", str(value), re.IGNORECASE)
    if not m:
        raise ConfigError(f"invalid size: {value!r}")
    return int(float(m.group(1)) * _SIZE_UNITS[m.group(2).lower()])


def parse_speed(value: str | int) -> int:
    """`1mbps`, `800kbps`, `0kbps` → bytes/second.

    Follows ratio-spoof: `kbps` = KiB/s, `mbps` = MiB/s.
    """
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"\s*([\d.]+)\s*([km]?)(bps|b/s)?\s*", str(value), re.IGNORECASE)
    if not m:
        raise ConfigError(f"invalid speed: {value!r}")
    return int(float(m.group(1)) * _SIZE_UNITS[m.group(2).lower()])


def parse_percent(value: str | float) -> float:
    """`40%` or `0.4` → 0.4."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s[:-1]) / 100 if s.endswith("%") else float(s)
    except ValueError:
        raise ConfigError(f"invalid percentage: {value!r}") from None


def parse_duration(value: str | int) -> int:
    """`14d`, `1d12h`, `90s`, `3600` → seconds."""
    if isinstance(value, int):
        return value
    s = str(value).strip().lower()
    if s.isdigit():
        return int(s)
    parts = re.findall(r"(\d+)\s*([smhdw])", s)
    if not parts or "".join(n + u for n, u in parts) != s.replace(" ", ""):
        raise ConfigError(f"invalid duration: {value!r}")
    return sum(int(n) * _DURATION_UNITS[u] for n, u in parts)


def _parse_list(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [v.strip() for v in str(value).split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    qbit_url: str = ""
    qbit_user: str = ""
    qbit_pass: str = ""
    library_path: Path = Path("/mount")
    review_path: Path = Path("/data/review")
    tmdb_api_key: str = ""
    split_reserve: int = field(default=2 * 1024**3, metadata={"parse": parse_size})
    split_hook: str = ""
    spoof_speed: int = field(default=1024**2, metadata={"parse": parse_speed})
    spoof_jitter: float = field(default=0.4, metadata={"parse": parse_percent})
    spoof_target_ratio: float = 2.0
    spoof_max_duration: int = field(default=14 * 86400, metadata={"parse": parse_duration})
    notify_urls: list[str] = field(default_factory=list, metadata={"parse": _parse_list})
    sonarr_url: str = ""
    sonarr_key: str = ""
    radarr_url: str = ""
    radarr_key: str = ""
    http_port: int = 8080
    data_dir: Path = Path("/config")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "toolkit.db"

    @property
    def torrents_dir(self) -> Path:
        return self.data_dir / "torrents"

    def validate(self) -> None:
        if not self.qbit_url:
            raise ConfigError("TK_QBIT_URL is required")
        if not 0 <= self.spoof_jitter < 1:
            raise ConfigError("TK_SPOOF_JITTER must be between 0% and 100%")
        if self.spoof_target_ratio <= 0:
            raise ConfigError("TK_SPOOF_TARGET_RATIO must be positive")


def _coerce(f, raw: Any) -> Any:
    if (parse := f.metadata.get("parse")) is not None:
        return parse(raw)
    if f.type in ("Path", Path):
        return Path(raw)
    if f.type in ("int", int):
        return int(raw)
    if f.type in ("float", float):
        return float(raw)
    return str(raw)


def load(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ) if env is None else env
    data_dir = Path(env.get("TK_DATA_DIR", "/config"))

    raw: dict[str, Any] = {"data_dir": data_dir}
    config_file = data_dir / "config.yml"
    if config_file.is_file():
        loaded = yaml.safe_load(config_file.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{config_file} must be a mapping")
        raw.update({str(k).lower(): v for k, v in loaded.items()})

    for f in fields(Settings):
        if (value := env.get(f"TK_{f.name.upper()}")) is not None:
            raw[f.name] = value

    known = {f.name: f for f in fields(Settings)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")

    return Settings(**{k: _coerce(known[k], v) for k, v in raw.items()})
