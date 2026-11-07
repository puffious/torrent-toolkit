from torrent_toolkit.qbit import Qbit, TorrentInfo, parse_tags


def test_parse_tags():
    assert parse_tags("split, organize,service") == {"split", "organize", "service"}
    assert parse_tags("") == frozenset()


def test_torrent_info_metadata_fallback():
    assert not TorrentInfo.from_api("h", {"state": "metaDL", "total_size": 0}).has_metadata
    assert TorrentInfo.from_api("h", {"state": "stoppedDL", "total_size": 5}).has_metadata
    assert not TorrentInfo.from_api("h", {"state": "stoppedDL", "has_metadata": False}).has_metadata


def test_sync_merges_partial_updates(monkeypatch):
    q = Qbit("http://localhost:1", "", "")
    responses = iter(
        [
            {
                "rid": 1,
                "full_update": True,
                "torrents": {
                    "a": {"name": "A", "state": "stoppedDL", "tags": "split", "total_size": 5},
                    "b": {"name": "B", "state": "uploading", "tags": "", "total_size": 7},
                },
            },
            {
                "rid": 2,
                "torrents": {"a": {"state": "downloading", "progress": 0.5}},
                "torrents_removed": ["b"],
            },
        ]
    )
    seen_rids = []

    def fake_sync(rid):
        seen_rids.append(rid)
        return next(responses)

    monkeypatch.setattr(q._client, "sync_maindata", fake_sync)
    q.poll()
    view = q.poll()
    assert seen_rids == [0, 1]
    assert set(view) == {"a"}
    assert view["a"].state == "downloading"
    assert view["a"].name == "A" and view["a"].progress == 0.5 and view["a"].tags == {"split"}
