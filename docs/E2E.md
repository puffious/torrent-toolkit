# End-to-end checklist

Run on the real server after deploying. Watch `docker logs -f torrent-toolkit` and `curl localhost:8090/status`.

## 0. Preflight

- [ ] `curl localhost:8090/health` → `ok`.
- [ ] Logs show no `qBittorrent unavailable` warnings.
- [ ] qBittorrent: pre-allocation **off**.
- [ ] `<library>/movies` and `<library>/shows` exist inside the toolkit container (`docker exec torrent-toolkit ls /mount`).
- [ ] A test notification arrives (tag a torrent with a bogus name + `organize` → review notification).

## 1. Organize

- [ ] Add a single-movie torrent **paused** with `organize`. It gets `service`, starts, downloads.
- [ ] After completion: file is in `movies/Title (Year)/`, subs renamed next to it, samples/nfo deleted, `service` removed, torrent still in qBittorrent.
- [ ] A season pack lands in `shows/Show (Year)/Season NN/`.
- [ ] Something unidentifiable lands in the review folder, torrent tagged `tk-review`, notification received.
- [ ] Stop the rclone mount, organize something → `organize failed` notification once, files untouched; restart the mount → retried within 10 min.

## 2. Split

Use a torrent with several files totalling more than the free space (or temporarily set `TK_SPLIT_RESERVE` high to fake a small disk).

- [ ] Add paused with `split` (+ `organize`). Only the first batch's files are set to download.
- [ ] Batch completes → files organized/hooked → set to "do not download" → next batch starts once space is back.
- [ ] **Offloaded-piece risk:** while later batches download, watch whether qBittorrent puts the torrent in an *error* state (peers requesting pieces of files that were moved away). If it happens, note it — fallback is a force-recheck after each offload (DESIGN §1).
- [ ] After the last batch the torrent is removed from qBittorrent and the `.<hash>.parts` file is gone.
- [ ] Restart the toolkit mid-batch → it continues with the same batch (check `/status`).
- [ ] Add a second `split` torrent while the first runs → it waits (all files "do not download") until the first finishes.

## 3. Spoof

Use a torrent from a tracker where you accept the risk.

- [ ] Add with `spoof` (+ `spoof-speed:500kbps`). On completion it disappears from qBittorrent; data stays on disk; `/status` shows the spoof `active` with the real uploaded value.
- [ ] Tracker shows the torrent seeding with the same client version as your qBittorrent.
- [ ] `PATCH` the speed → next announces grow at the new rate.
- [ ] Restart the toolkit → the spoof resumes from its last uploaded value (`event=started`).
- [ ] `POST /spoof/<hash>/stop` → status `stopped`, tracker no longer lists you.
- [ ] `spoof` + `organize` → data organized after hand-off.
- [ ] `split` + `spoof` → hand-off only after the last batch.
