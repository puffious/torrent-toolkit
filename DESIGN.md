# Torrent Toolkit — Design

A daemon that sits in front of qBittorrent and acts on torrents based on their tags:

1. **Split** — download a torrent larger than the disk in batches of files that fit.
2. **Organize** — move finished files into a Jellyfin-style library (on an rclone mount).
3. **Spoof** — hand a finished torrent to a built-in ratio-spoof port that keeps announcing to the tracker.

Out of scope for now: CLI, web UI, bulk "import existing data for seeding" (future feature).

> Ratio spoofing violates the rules of most private trackers and can get accounts banned. Used at the operator's own risk.

---

## Runtime

- Python 3.12, single package `torrent_toolkit`, one Docker image.
- Runs on the same host as qBittorrent, as a container sharing **identical volume mounts** (no path translation).
- Talks to qBittorrent only through the Web API (qBittorrent **≥ 4.5** required for `torrents/export`).
- Polls `sync/maindata` every 10 s.
- State in SQLite at `/config/toolkit.db`. The DB is the source of truth; everything resumes after a restart.
- One asyncio process: poll loop, split/organize workers, spoof announce tasks, HTTP API (FastAPI/uvicorn). The blocking qBittorrent client runs via `asyncio.to_thread`.
- Logs: structured, to stdout.

## Configuration

Environment variables (set in docker-compose) override `/config/config.yml`. The env list is kept short on purpose; rarely-changed constants (poll interval, thresholds) live only in code/`config.yml`.

```
TK_QBIT_URL, TK_QBIT_USER, TK_QBIT_PASS
TK_LIBRARY_PATH          e.g. /mount  → contains movies/ and shows/
TK_REVIEW_PATH           dump folder for unidentified files
TK_TMDB_API_KEY          without it, every organize file goes to review
TK_SPLIT_RESERVE=2GB
TK_SPLIT_HOOK            optional; run for split batches that are not organized
TK_SPOOF_SPEED=1mbps
TK_SPOOF_JITTER=40%
TK_SPOOF_TARGET_RATIO=2.0
TK_SPOOF_MAX_DURATION=14d
TK_NOTIFY_URLS           optional; Apprise URLs, comma-separated
TK_SONARR_URL, TK_SONARR_KEY, TK_RADARR_URL, TK_RADARR_KEY   optional
TK_HTTP_PORT=8080
```

## Tags

| Tag | Set by | Meaning |
|---|---|---|
| `split` | user | Download in disk-sized batches |
| `organize` | user | Move finished files into the library |
| `spoof` | user | Hand off to the spoofer once fully downloaded |
| `spoof-speed:<speed>` | user | Per-torrent upload speed override, e.g. `spoof-speed:2mbps`, `spoof-speed:0kbps` |
| `spoof-forever` | user | No ratio/duration stop condition |
| `service` | daemon | Daemon currently owns/works on this torrent |
| `tk-review` | daemon | Some files could not be identified and were dumped to review |

Tags combine freely. `split + spoof`: spoof starts only after the **entire** torrent has been downloaded (all batches).

## Takeover

User adds torrents **paused** with tags set (`.torrent`: "Start torrent" unchecked; magnet: started with stop condition "Metadata received"). qBittorrent's "Pre-allocate disk space" must be **off**.

On every poll, any torrent with `split`/`organize`/`spoof` that is not in the DB is taken over:

1. Wait until metadata (file list) is available.
2. Record it in the DB, add tag `service`.
3. `split`: set all files to "do not download", enable batch 1, start the torrent.
   Otherwise: start the torrent if paused.
4. If a `split` torrent is found already running, its files are immediately set to "do not download" and a warning is notified.

After takeover the daemon never restarts a torrent the user paused manually.

Restart reconciliation: torrents carrying `service` but unknown to the DB are reported (notification + log) rather than silently ignored; DB entries whose torrent vanished from qBittorrent are marked `gone`.

`service` is removed when the daemon is done: organize finished (torrent stays in qBittorrent), split finished, or spoof hand-off (torrent removed from qBittorrent).

---

## 1. Split

Downloads into the torrent's own save path; the daemon never changes it.

**Batch planning** (at the start of each batch):

- `capacity = measured_free_space(save_path) − TK_SPLIT_RESERVE` (`shutil.disk_usage`; real measurement handles the rclone VFS cache living on the same disk).
- Pick from remaining files using first-fit-decreasing (largest first that fits) until capacity is used → as few batches as possible.
- A file larger than the disk's total usable space is **skipped** and notified; all others still proceed.

**Batch loop:**

1. Set the batch's files to normal priority; everything else stays "do not download".
2. Wait until every file in the batch has progress 1.0.
3. Offload:
   - tagged `organize` → organize exactly those files (see §2);
   - else if `TK_SPLIT_HOOK` set → run it with the list of completed files (absolute paths, one per line in a temp file passed as `$TK_FILES` and `$1`); non-zero exit → notify and retry later;
   - else → wait for the user to free space.
   Only completed files are ever passed on; `*.parts` and `*.!qB` are never included.
4. Set the offloaded files to "do not download" (same torrent keeps going; boundary pieces go to `.parts`).
5. Wait for space to come back: start the next batch when `capacity ≥ remaining size`, or free space has stopped growing for 5 minutes and `capacity ≥ smallest remaining file`.

**After the last batch:** `spoof` → hand off (§3). Otherwise remove the torrent from qBittorrent (keep files) and delete leftover `.parts` files.

Known risk, handled in testing: a peer requesting a piece of an offloaded file could put the torrent into an error state. Fallback if observed: force-recheck after each offload.

## 2. Organize

Triggered per file: for normal torrents when the torrent completes; for split torrents per finished batch.

**Identification (per file):**

- `guessit` on the file's full relative path inside the torrent (folder names give season/show context for packs).
- TMDB search (movie: title + year; TV: title [+ year]); results cached per `(type, title, year)`.
- Confident = normalized title similarity above threshold and year matches (±1) when known. Episodes additionally need season + episode numbers.
- Not confident → review.

**File classes:**

| Class | Action |
|---|---|
| Video | Move to library |
| Subtitle (`.srt .ass .ssa .sub .idx .vtt`) | Move next to its matching video |
| Extra (featurette, behind the scenes, deleted scenes, `extras/` folders) | Move to `<movie folder>/extras/` |
| Junk (samples, `.nfo .txt .jpg .png .exe .url` …) | Delete |

**Layout** (original filenames kept, names sanitized for the filesystem):

```
<TK_LIBRARY_PATH>/movies/Title (Year)/<original filename>
<TK_LIBRARY_PATH>/shows/Show (Year)/Season 01/<original filename>
```

**Move:** plain filesystem move into the mount — copy to `<dest>.tk-tmp`, rename to `<dest>`, delete source.

**Collisions:**

- Same name, same size → duplicate: delete the local file.
- Same name, different size → review.
- Different name in the same folder (other quality) → keep both.

**Review:** unidentified files move to `<TK_REVIEW_PATH>/<torrent name>/<original relative path>`; torrent gets `tk-review`; notification sent.

**qBittorrent afterwards:** non-spoofed organized torrents stay in qBittorrent pointing at the old path (expected to error eventually — accepted). Tag `service` removed.

**Sonarr/Radarr (optional, active only when URL+key set):** if the *arr already tracks the title (by TMDB id), use its folder path instead of the generated one, then trigger `RescanMovie`/`RescanSeries`. Recommend disabling "Analyse video files" in the *arrs.

## 3. Spoof

**Hand-off** (when a `spoof` torrent is fully downloaded; split torrents after the last batch):

1. `torrents/export` → save `/config/torrents/<infohash>.torrent`.
2. Snapshot: real `uploaded`, total size, tracker URLs (with passkey), tags (`spoof-speed:*`, `spoof-forever`).
3. Remove the torrent from qBittorrent, **keeping data** (stops qBittorrent's own announces).
4. Start the spoof task.
5. If also `organize`: organize the files from disk now (torrent is gone, so the daemon moves and may delete junk freely).

**Announcing** — a Python port of ratio-spoof's emulation/tracker logic:

- Identity copied from the connected qBittorrent: listen port (`app/preferences`), version (`app/version`) → peer_id prefix `-qBXYZ0-` and `User-Agent: qBittorrent/X.Y.Z`. Remaining announce details (param order, key format, headers) come from the nearest ratio-spoof profile (`qbit-4.3.3`).
- Continuous history: `downloaded = size`, `left = 0`, `uploaded` continues from the snapshot.
- HTTP(S) trackers only; first working tracker in tier order. UDP trackers skipped with a warning.
- Follows the tracker's `interval` from each response.
- Speed = tag override or `TK_SPOOF_SPEED`; re-rolled each interval as `speed × (1 ± random·JITTER)`. Speed 0 is never jittered.
- Stop: ratio ≥ `TK_SPOOF_TARGET_RATIO` **or** elapsed ≥ `TK_SPOOF_MAX_DURATION`, whichever first → send `event=stopped`. `spoof-forever` never stops on its own.
- Every announce persists `uploaded`, `last_announce`, `interval`, `status` to SQLite.
- Daemon restart: resume each active spoof from saved `uploaded` with `event=started`. Clean shutdown: `event=stopped`.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/status` | All tracked torrents: split progress, organize results, spoof state |
| POST | `/spoof/{hash}/stop` | Stop a spoof (sends `event=stopped`) |
| PATCH | `/spoof/{hash}` | Change speed / forever flag |

## Notifications

Apprise (`TK_NOTIFY_URLS`): skipped oversized files, review items, hook failures, split torrent found running, spoof finished/failed, unknown `service` torrents on restart.

## Data model (SQLite)

- `torrents(hash, name, save_path, tags, features, state, created_at, updated_at)`
- `split_files(hash, file_index, path, size, status)` — status: pending / batch / done / skipped
- `split_batches(hash, batch_no, started_at, finished_at)`
- `organized_files(hash, file_index, source, dest, action, status)`
- `spoofs(hash, torrent_path, size, uploaded, speed, forever, started_at, last_announce, interval, peer_id, key, status)`
- `tmdb_cache(type, title, year, result_json, fetched_at)`

## Project layout

```
pyproject.toml
src/torrent_toolkit/
  __main__.py      entrypoint (daemon)
  config.py        env + config.yml
  db.py            SQLite schema + access
  qbit.py          qBittorrent API wrapper
  daemon.py        poll loop, takeover, reconciliation
  split/           planner.py, runner.py
  organize/        classify.py, identify.py, tmdb.py, layout.py, mover.py
  spoof/           bencode.py, emulation.py, announcer.py, manager.py
  api.py           FastAPI app
  notify.py        Apprise wrapper
  arr.py           Sonarr/Radarr (optional)
tests/
Dockerfile
docker-compose.example.yml
```
