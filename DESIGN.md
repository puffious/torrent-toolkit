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
- One asyncio process: poll loop, spoof announce tasks, HTTP API (FastAPI/uvicorn). Each poll runs in a single worker thread (blocking qBittorrent client); split offloads and organize jobs run in their own threads so long moves never block polling.
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
3. `split`: set all files to "do not download"; the split runner then enables batch 1 and starts the torrent when it is first in the queue (one split torrent at a time, oldest first).
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
   - else if `TK_SPLIT_HOOK` set → run it via `sh -c` with the list of completed files (absolute paths, one per line, in a temp file passed as `$TK_FILES` and `$1`; also `$TK_SAVE_PATH`, `$TK_TORRENT_NAME`, `$TK_TORRENT_HASH`); non-zero exit → notify and retry after 10 minutes;
   - else → wait for the user to free space.
   Only completed files are ever passed on; `*.parts` and `*.!qB` are never included.
4. Set the offloaded files to "do not download" (same torrent keeps going; boundary pieces go to `.parts`).
5. Wait for space to come back: start the next batch when `capacity ≥ remaining size`, or free space has stopped growing for 5 minutes and `capacity ≥ smallest remaining file`. If nothing fits once space has settled, notify once and keep waiting.

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

**Move:** plain filesystem move into the mount — copy to `<dest>.tk-tmp`, rename to `<dest>`, delete source. Before moving, `movies/` or `shows/` must already exist in the library; otherwise the mount is assumed down and organize is retried later (create both folders once).

Subtitles are renamed `<video stem>.<subtitle stem>.<ext>` unless already prefixed with the video name, because Jellyfin links subtitles by filename. Unknown file types (not video/subtitle/junk) go to review, never deleted.

**Collisions:**

- Same name, same size → duplicate: delete the local file.
- Same name, different size → review.
- Different name in the same folder (other quality) → keep both.

**Review:** unidentified files move to `<TK_REVIEW_PATH>/<original relative path>` (single-file torrents get a folder named after the file); torrent gets `tk-review`; notification sent.

**qBittorrent afterwards:** non-spoofed organized torrents stay in qBittorrent pointing at the old path (expected to error eventually — accepted). Tag `service` removed.

**Sonarr/Radarr (optional, active only when URL+key set):** if the *arr already tracks the title (by TMDB id), use **its folder name** (only the name, never its full path, so the *arr containers may mount the library elsewhere) and, for Sonarr, its `seasonFolderFormat` / season-folder setting; then trigger `RescanMovie`/`RescanSeries`. If the *arr is unreachable, the toolkit's own naming is used. Recommend disabling "Analyse video files" in the *arrs.

## 3. Spoof

**Hand-off** (when a `spoof` torrent is fully downloaded; split torrents after the last batch):

1. `torrents/export` → parse; the v1 info hash must equal qBittorrent's hash (v2-only torrents are rejected). Saved to `/config/torrents/<infohash>.torrent`.
2. Snapshot: real `uploaded`, total size, HTTP(S) tracker URLs from qBittorrent (with passkey; falls back to the `.torrent`), tags (`spoof-speed:*`, `spoof-forever`), qBittorrent version and listen port.
3. Record the spoof as `pending`, remove the torrent from qBittorrent **keeping data**, then mark it `active`. A `pending` spoof whose torrent is already gone (crash in between) is activated on the next poll; one whose torrent is still there is handed off again.
4. If also `organize`: the completed file list is stored and the files are organized from disk (torrent state `detached` → `done`).

If the hand-off fails (no HTTP tracker, unreadable/mismatched `.torrent`), a notification is sent, `spoof` is dropped for that torrent and it stays in qBittorrent (organize still runs if tagged).

**Announcing** — a Python port of ratio-spoof's emulation/tracker logic:

- Identity copied from the connected qBittorrent: listen port (`app/preferences`), version (`app/version`) → peer_id prefix `-qBXYZ0-` + 12 chars from ratio-spoof's alphabet, `User-Agent: qBittorrent/X.Y.Z`. One peer id/key per daemon session. Query parameter order, `key` (8 upper-case hex), `numwant=200` and headers come from ratio-spoof's `qbit-4.3.3` profile.
- Continuous history: `downloaded = size`, `left = 0`, `uploaded` continues from the snapshot.
- Uploaded growth per announce (ratio-spoof formula): `rate × elapsed + piece_length × rand(1..9)`, rounded down to 16 KiB; nothing is added at speed 0.
- `event=started` on the first announce of a session, no `event` on regular announces (ratio-spoof sends `started` every time; real clients don't), `event=stopped` with `numwant=0` when stopping.
- HTTP(S) trackers only, tried in order; the one that answers moves to the front.
- Follows the tracker's `interval` (1800 s if missing). Failures retry with 30 s → 900 s backoff; one notification per failure streak; after 24 h of failures the spoof is marked `failed`.
- Speed = tag override or `TK_SPOOF_SPEED`; re-rolled each interval as `speed × (1 ± random·JITTER)`. Speed 0 is never jittered.
- Stop: ratio ≥ `TK_SPOOF_TARGET_RATIO` **or** elapsed ≥ `TK_SPOOF_MAX_DURATION`, whichever first → `event=stopped`, status `finished`. `spoof-forever` never stops on its own.
- Every successful announce persists `uploaded`, `last_announce`, `interval`, seeders/leechers to SQLite.
- Daemon restart: resume each active spoof from saved `uploaded` with `event=started`. Clean shutdown: `event=stopped` (spoofs stay `active`).

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/status` | All tracked torrents (split progress, organize counts) and spoofs (passkeys redacted) |
| GET | `/spoof/{hash}` | One spoof |
| POST | `/spoof/{hash}/stop` | Stop a spoof (sends `event=stopped`, status `stopped`) |
| PATCH | `/spoof/{hash}` | `{"speed": "2mbps", "forever": true}` |

## Notifications

Apprise (`TK_NOTIFY_URLS`): oversized files skipped, split stalled for space, split finished, split offload/hook failures, organize failures (once per streak), review items, split torrent found running, spoof hand-off failed, spoof announce failing / failed / finished, unknown `service` torrents.

## Data model (SQLite, migrations via `PRAGMA user_version`)

- `torrents(hash, name, save_path, features, state, created_at, updated_at)` — state: metadata / active / done / detached / orphan / gone
- `split_files(hash, file_index, size, status, batch_no)` — status: pending / batch / done / skipped
- `split_state(hash, phase, batch_no, last_free, last_growth_at, retry_at, stall_notified)` — phase: queued / downloading / offloading / waiting_space / finished
- `organized_files(hash, file_index, source, dest, action, reason, done_at)` — makes organize idempotent
- `tmdb_cache(kind, title, year, result, fetched_at)` — hits 30 days, misses 3 days
- `spoofs(hash, name, torrent_path, info_hash, trackers, size, piece_length, uploaded, speed, forever, port, client_version, status, stop_reason, started_at, last_announce, interval, seeders, leechers, last_error)`
- `detached_organize(hash, name, save_path, files, created_at)`

## Project layout

```
pyproject.toml
src/torrent_toolkit/
  __main__.py      entrypoint: daemon + spoofer + HTTP API in one asyncio loop
  config.py        env + config.yml
  db.py            SQLite schema + access
  qbit.py          qBittorrent API wrapper
  daemon.py        poll loop, takeover, reconciliation, hand-off, organize jobs
  jobs.py          background jobs with retry cool-down
  split/           planner.py, runner.py
  organize/        classify.py, identify.py, tmdb.py, layout.py, mover.py, organizer.py
  spoof/           bencode.py, emulation.py, announcer.py, manager.py
  api.py           FastAPI app
  notify.py        Apprise wrapper
  arr.py           Sonarr/Radarr (optional)
tests/
Dockerfile
docker-compose.example.yml
docker-compose.test.yml
```
