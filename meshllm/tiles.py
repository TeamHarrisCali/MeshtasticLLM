"""A small on-disk cache for the map background.

The map pages ask this bridge for tiles (/tiles/z/x/y.png). A tile is fetched from openstreetmap.org the first time it is
looked at and kept in tile_cache/, so panning is smooth, tiles already seen work offline, and the browser itself never
contacts openstreetmap.org. It only ever fetches a tile somebody is actually looking at (no bulk downloading, which the
OpenStreetMap tile policy forbids), identifies itself, limits how many fetches run at once, and keeps the folder under a
size cap by deleting the oldest tiles. Deleting the folder is always safe.
"""
import os
import threading
import time
from pathlib import Path

import requests

UPSTREAM = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "MeshLLM-Bridge/1.0 (personal local mesh dashboard; on-demand tile cache)"
MAX_ZOOM = 19
MAX_TILE_BYTES = 400_000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class TileCache:
    """Disk-backed OpenStreetMap tile cache, safe to call from many request threads at once.

    Tiles live at <folder>/<z>/<x>/<y>.png. `fetch` (z, x, y -> bytes) can be replaced, e.g. in tests, so no network is needed.
    `parallel` caps simultaneous upstream downloads; `retry_after_s` is how long a failed tile is left alone before retrying.
    """

    def __init__(self, folder, max_bytes=500 * 1024 * 1024, max_age_s=30 * 86400, fetch=None, parallel=4, retry_after_s=60):
        self.dir = Path(folder)
        self.max_bytes, self.max_age, self.retry_after = max_bytes, max_age_s, retry_after_s
        self._fetch = fetch or self._download
        self._slots = threading.Semaphore(parallel)
        self._locks, self._locks_guard = {}, threading.Lock()
        self._failed = {}                       # (z, x, y) -> when it last failed, so a dead connection isn't hammered
        self._writes = 0
        self.fetched = self.served_from_disk = 0

    @staticmethod
    def valid(z, x, y):
        """True if z/x/y are real ints (not bools) inside the tile grid; also what keeps odd values out of the file path."""
        return all(isinstance(v, int) and not isinstance(v, bool) for v in (z, x, y)) and 0 <= z <= MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z

    def _path(self, z, x, y):
        """Where a tile is kept on disk (callers must have passed valid() first)."""
        return self.dir / str(z) / str(x) / f"{y}.png"

    @staticmethod
    def _download(z, x, y):
        """Fetch one tile from the upstream server; raises OSError on any non-200 answer."""
        r =requests.get(UPSTREAM.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT}, timeout=10)
        if r.status_code != 200:
            raise OSError(f"tile server answered {r.status_code}")
        return r.content

    def get(self, z, x, y):
        """PNG bytes for a tile, or None if it isn't cached and can't be fetched."""
        if not self.valid(z, x, y):
            return None
        path = self._path(z, x, y)
        stale = None
        try:
            age = time.time() - path.stat().st_mtime
            data = path.read_bytes()
            if age < self.max_age:
                self.served_from_disk += 1
                return data
            stale = data                        # old: try to refresh, fall back to it
        except OSError:
            pass
        key = (z, x, y)
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:                              # several requests for one tile fetch it once
            if path.exists() and stale is None:      # someone else just fetched it
                try:
                    return path.read_bytes()
                except OSError:
                    pass
            if time.time() - self._failed.get(key, 0) < self.retry_after:
                return stale
            with self._slots:
                try:
                    data = self._fetch(z, x, y)
                    # only store something that really is a small PNG, so a captive-portal page or error body never lands in the cache
                    if not (isinstance(data, bytes) and data.startswith(PNG_MAGIC) and len(data) <= MAX_TILE_BYTES):
                        raise OSError("not a tile image")
                except Exception:
                    self._failed[key] = time.time()
                    return stale
            self._failed.pop(key, None)
            self._store(path, data)
            self.fetched += 1
            return data

    def _store(self, path, data):
        """Write a tile atomically; a failed write is ignored (the tile was still served). Prunes every 200 writes."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + f".{threading.get_ident()}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)               # never leave a half-written tile where a reader could see it
        except OSError:
            return
        self._writes += 1
        if self._writes % 200 == 0:
            self.prune()

    def stats(self):
        """Number of cached tiles, their total size in bytes, and the size cap in bytes."""
        count = size = 0
        if self.dir.exists():
            for f in self.dir.rglob("*.png"):
                try:
                    size += f.stat().st_size; count += 1
                except OSError:
                    pass
        return {"tiles": count, "bytes": size, "max_bytes": self.max_bytes}

    def prune(self):
        """Delete the oldest tiles until the folder is under 80% of the cap."""
        files = []
        for f in self.dir.rglob("*.png"):
            try:
                st = f.stat(); files.append((st.st_mtime, st.st_size, f))
            except OSError:
                pass
        total = sum(s for _, s, _ in files)
        if total <= self.max_bytes:
            return 0
        removed = 0
        for _, size, f in sorted(files):
            if total <= self.max_bytes * 0.8:
                break
            try:
                f.unlink(); total -= size; removed += 1
            except OSError:
                pass
        return removed

    def clear(self):
        """Delete every cached tile and return how many were removed."""
        n = 0
        for f in list(self.dir.rglob("*.png")):
            try:
                f.unlink(); n += 1
            except OSError:
                pass
        return n
