# Build Plan

Each milestone ends with tests passing and one commit (`git commit --no-gpg-sign`). See `DESIGN.md` for behavior.

## M0 — Scaffold
1. `pyproject.toml` (uv, Python 3.12): deps `qbittorrent-api`, `guessit`, `httpx`, `apprise`, `fastapi`, `uvicorn`, `pyyaml`; dev deps `pytest`, `pytest-asyncio`, `respx`, `ruff`.
2. `src/torrent_toolkit/` skeleton matching the DESIGN layout, `__main__.py` that starts and logs.
3. `config.py`: load `config.yml` then env overrides; parse sizes (`2GB`), speeds (`1mbps`), percentages, durations (`14d`).
4. Structured logging to stdout.
5. `Dockerfile`, `docker-compose.example.yml` (qBittorrent + toolkit, shared mounts), `.gitignore`.
6. Tests: config parsing.

**Done when:** `docker build` works, container starts and exits cleanly; tests pass.
**Commit:** `M0: project scaffold, config, Docker`

## M1 — qBittorrent core + daemon loop
1. `db.py`: schema + migrations-on-start, small repository functions.
2. `qbit.py`: login, `sync/maindata` incremental poll, files, set file priority, start/stop (handle 4.x pause/resume vs 5.x stop/start), tags add/remove, export, delete, preferences, version.
3. `daemon.py`: poll loop; takeover (metadata wait, DB record, `service` tag, start); never re-start user-paused torrents; restart reconciliation.
4. `api.py`: `/health`, `/status` (basic).
5. `notify.py`: Apprise wrapper (no-op when unset).
6. Tests: takeover logic against a fake qBit client; integration test against a real qBittorrent container (`docker-compose.test.yml`, marked `integration`).

**Done when:** tagging a paused torrent makes the daemon tag it `service` and start it; state survives restart.
**Commit:** `M1: qBittorrent client, state DB, daemon takeover loop`

## M2 — Split
1. `split/planner.py`: FFD batch selection given capacity; oversized-file detection. Pure functions.
2. `split/runner.py`: per-torrent state machine — plan → enable batch → wait complete → offload (hook / wait for user) → disable offloaded files → wait for space → next batch → finish (remove torrent, clean `.parts`).
3. Free-space measurement + "space settled" rule.
4. Hook runner (`$TK_FILES` temp file, retry on failure, notify).
5. One-at-a-time FIFO queue for split torrents.
6. Tests: planner unit tests (edge cases: one huge file, all fit, many tiny files); runner state machine with fake qBit + fake disk.

**Done when:** a multi-file test torrent larger than a small test volume downloads fully through the hook in batches.
**Commit:** `M2: split downloads in disk-sized batches`

## M3 — Organize
1. `organize/classify.py`: video / subtitle / extra / junk.
2. `organize/tmdb.py`: search movie/TV, SQLite cache.
3. `organize/identify.py`: guessit on full relative path + TMDB confidence rules.
4. `organize/layout.py`: destination paths, filename sanitizing, subtitle pairing, extras folder.
5. `organize/mover.py`: tmp-copy → rename → delete; collision rules; review dump; `tk-review` tag.
6. Wire in: on completion for normal torrents; per batch for split torrents (replaces the hook).
7. Tests: identification fixtures (single movie, 200-movie pack, multi-season show pack, anime-style names, extras/samples), layout + collision tests on a temp dir, TMDB via `respx`.

**Done when:** fixture packs produce the expected library tree; unknowns land in review.
**Commit:** `M3: organize into Jellyfin layout`

## M4 — Spoof
1. `spoof/bencode.py`: decoder keeping the raw `info` span → info_hash.
2. `spoof/emulation.py`: identity from qBit (port, version → peer_id prefix, User-Agent) + ratio-spoof `qbit-4.3.3` details (key, params, headers).
3. `spoof/announcer.py`: announce request/response (interval, failure reason), tracker tier selection, HTTP(S) only.
4. `spoof/manager.py`: hand-off (export, snapshot, remove from qBit), per-torrent asyncio task, jitter, stop conditions, `forever`, persistence, resume with `event=started`, `event=stopped` on stop/shutdown.
5. Hand-off wiring incl. `split + spoof` (after last batch) and `spoof + organize` (organize after removal).
6. Tests: bencode/info_hash against known torrents; announce URL byte-for-byte vs. ratio-spoof output; manager with a fake tracker server (stop conditions, resume, jitter bounds).

**Done when:** a spoofed torrent leaves qBit and announces to a local fake tracker with continuous stats, surviving a restart.
**Commit:** `M4: ratio spoofer daemon`

## M5 — Control API + notifications
1. `/status` full detail; `POST /spoof/{hash}/stop`; `PATCH /spoof/{hash}`.
2. Notifications wired for every event listed in DESIGN.
3. Tests: API via FastAPI test client.

**Commit:** `M5: control API and notifications`

## M6 — Sonarr/Radarr (optional integration)
1. `arr.py`: lookup existing path by TMDB id; rescan command. Active only when URL+key set.
2. Organize uses *arr path when found.
3. Tests with `respx`.

**Commit:** `M6: optional Sonarr/Radarr path lookup and rescan`

## M7 — Docs + end-to-end
1. `README.md`: setup, compose example, qBittorrent settings (pre-allocation off, add paused, magnet stop condition), tags, env vars.
2. Manual E2E checklist on the real server: split on a small disk, organize into the rclone mount, spoof a torrent, restart mid-everything; verify the offloaded-piece error risk (DESIGN §1).

**Commit:** `M7: README and E2E checklist`
