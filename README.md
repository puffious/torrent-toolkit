# torrent-toolkit

A daemon that sits next to qBittorrent and acts on torrents by **tag**:

| Tag | What happens |
|---|---|
| `split` | Downloads a torrent bigger than your disk in batches of files that fit, offloading each batch before the next. |
| `organize` | Moves finished files into a Jellyfin layout (`movies/Title (Year)/`, `shows/Show (Year)/Season 01/`). |
| `spoof` | Once fully downloaded, removes the torrent from qBittorrent and keeps announcing to the tracker with a fake upload ([ratio-spoof](https://github.com/ap-pauloafonso/ratio-spoof) port). |

Tags combine: `split` + `organize` organizes each batch as it finishes; `split` + `spoof` spoofs after the last batch; `spoof` + `organize` organizes the data after hand-off.

While the toolkit works on a torrent it carries the `service` tag. Torrents with unidentifiable files get `tk-review`.

> **Ratio spoofing breaks the rules of most private trackers and can get your account banned.** Use at your own risk.

See [DESIGN.md](DESIGN.md) for the full behavior.

## Setup

1. **qBittorrent ≥ 4.5**, Web UI enabled. In *Options → Downloads*:
   - **Pre-allocate disk space for all files: off** (otherwise split torrents reserve their full size).
   - Uncheck **Start torrent** in the add dialog for tagged torrents (or for magnets: start with **Stop condition = Metadata received**). The toolkit starts them itself after setting file priorities.
2. Mount the same host folders at the **same paths** in qBittorrent and the toolkit (e.g. `/data` for downloads, `/mount` for the rclone mount).
3. Create `movies/` and `shows/` in the library root once. The toolkit refuses to organize if neither exists — that is how it detects a dead rclone mount.
4. Get a free [TMDB API key](https://www.themoviedb.org/settings/api) (v3 key or v4 read token).
5. Run it — see [`docker-compose.example.yml`](docker-compose.example.yml) (uses the released image `ghcr.io/puffious/torrent-toolkit`):

```sh
docker compose -f docker-compose.example.yml up -d
```

## Configuration

Environment variables (or the same keys in lowercase in `/config/config.yml`):

| Variable | Default | |
|---|---|---|
| `TK_QBIT_URL` | — | required, e.g. `http://qbittorrent:8080` |
| `TK_QBIT_USER`, `TK_QBIT_PASS` | | |
| `TK_LIBRARY_PATH` | `/mount` | contains `movies/` and `shows/` |
| `TK_REVIEW_PATH` | `/data/review` | unidentified files are dumped here |
| `TK_TMDB_API_KEY` | | without it every organized file goes to review |
| `TK_SPLIT_RESERVE` | `2GB` | free space always kept |
| `TK_SPLIT_HOOK` | | shell command for split batches **not** tagged `organize` |
| `TK_SPOOF_SPEED` | `1mbps` | default upload speed |
| `TK_SPOOF_JITTER` | `40%` | speed varies ± this much, re-rolled every announce |
| `TK_SPOOF_TARGET_RATIO` | `2.0` | stop when reached… |
| `TK_SPOOF_MAX_DURATION` | `14d` | …or after this long, whichever comes first |
| `TK_NOTIFY_URLS` | | [Apprise](https://github.com/caronc/apprise) URLs, comma-separated |
| `TK_SONARR_URL`, `TK_SONARR_KEY`, `TK_RADARR_URL`, `TK_RADARR_KEY` | | optional, see below |
| `TK_HTTP_PORT` | `8080` | status/control API |

Units: sizes are binary (`2GB` = 2 GiB). Speeds follow ratio-spoof: `kbps` = KiB/s, `mbps` = MiB/s.

### Spoof options per torrent

Add these tags next to `spoof` before the torrent finishes:

- `spoof-speed:2mbps` — override the speed (`spoof-speed:0kbps` reports no upload at all).
- `spoof-forever` — never stop on ratio/duration (e.g. with `0kbps` to only build seed time).

The spoofer copies the connected qBittorrent's version (peer id prefix, User-Agent) and listen port, continues from the torrent's real uploaded amount, and resumes after restarts.

### Split hook

For `split` torrents without `organize`, `TK_SPLIT_HOOK` runs after every batch via `sh -c`. `$TK_FILES` (also `$1`) is a file listing the completed files' absolute paths, one per line; `$TK_SAVE_PATH`, `$TK_TORRENT_NAME`, `$TK_TORRENT_HASH` are set too. Non-zero exit → notification and retry every 10 minutes.

```sh
TK_SPLIT_HOOK='rclone move --files-from-raw "$TK_FILES" / remote:incoming'
```

The image does not ship rclone; for a hook like this, extend the image (`FROM` it and install rclone) and mount your `rclone.conf`. Without a hook the toolkit waits until you free the space yourself.

### Sonarr / Radarr

When URL and key are set and the *arr already tracks the title, the toolkit uses **the *arr's folder name** (and Sonarr's season folder format) and triggers `RescanMovie` / `RescanSeries` afterwards, so the *arr sees the file instead of grabbing it again. Turn off *Analyse video files* in the *arrs to avoid reading files over the mount.

## API

```sh
curl localhost:8090/health
curl localhost:8090/status                      # torrents, split progress, organize results, spoofs
curl localhost:8090/spoof/<hash>
curl -X POST localhost:8090/spoof/<hash>/stop
curl -X PATCH localhost:8090/spoof/<hash> -H 'content-type: application/json' \
     -d '{"speed": "500kbps", "forever": true}'
```

## Development

```sh
uv sync
uv run pytest                                    # unit tests
docker compose -f docker-compose.test.yml up -d  # throwaway qBittorrent on :18080
uv run pytest -m integration
uv run ruff check . && uv run ruff format --check .
```

### Releasing

1. Bump `version` in `pyproject.toml`, run `uv lock`, commit.
2. `git tag v<version> && git push origin main --tags`

The `release` workflow checks the tag matches the version, runs the tests, publishes `ghcr.io/puffious/torrent-toolkit:<version>` / `:latest` (amd64 + arm64) and creates the GitHub Release with the commit list. The repo is private, so the server needs `docker login ghcr.io` with a token that has `read:packages`.

End-to-end checklist for a real server: [docs/E2E.md](docs/E2E.md).

ratio-spoof is MIT-licensed © ap-pauloafonso; the announce emulation here is a port of it.
