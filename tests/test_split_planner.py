from torrent_toolkit.split.planner import oversized, plan_batch

GB = 1024**3


def test_all_fit():
    assert plan_batch([(0, 1), (1, 2), (2, 3)], 10) == [0, 1, 2]


def test_largest_first_fills_capacity():
    # FFD picks 6 then 4 (=10) rather than 1+2+3 in order
    files = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 6)]
    assert plan_batch(files, 10) == [3, 4]


def test_nothing_fits():
    assert plan_batch([(0, 5 * GB)], 4 * GB) == []


def test_empty():
    assert plan_batch([], 100) == []


def test_many_tiny_files_and_one_huge():
    files = [(0, 90)] + [(i, 1) for i in range(1, 21)]
    assert plan_batch(files, 100) == [0] + list(range(1, 11))


def test_batches_cover_everything():
    files = dict(enumerate([7, 5, 4, 3, 3, 2, 2, 1, 1, 1]))
    batches = []
    while files:
        batch = plan_batch(files.items(), 10)
        assert batch, "no progress"
        batches.append(batch)
        for i in batch:
            del files[i]
    assert len(batches) == 3  # total 29 → at least 3 batches of 10


def test_oversized():
    assert oversized([(0, 5), (1, 50), (2, 11)], 10) == [1, 2]
