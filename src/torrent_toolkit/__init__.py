from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("torrent-toolkit")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0"
