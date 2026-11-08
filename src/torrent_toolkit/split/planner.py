"""Pure batch planning: which files to download next."""

from __future__ import annotations

from collections.abc import Iterable


def plan_batch(files: Iterable[tuple[int, int]], capacity: int) -> list[int]:
    """First-fit decreasing: take the largest files that still fit into `capacity`.

    `files` are `(index, size)` pairs. Returns the chosen indexes in torrent order.
    Applied batch after batch, this keeps the number of batches low.
    """
    chosen: list[int] = []
    left = capacity
    for index, size in sorted(files, key=lambda f: (-f[1], f[0])):
        if size <= left:
            chosen.append(index)
            left -= size
    return sorted(chosen)


def oversized(files: Iterable[tuple[int, int]], limit: int) -> list[int]:
    """Files that can never fit, even on an otherwise empty disk."""
    return sorted(index for index, size in files if size > limit)
