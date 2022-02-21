# Standard Library
import asyncio
import concurrent
import fcntl
import os
from datetime import datetime
from time import time

from structlog import getLogger
from structlog.contextvars import bind_contextvars

# Third Party Libraries
from git_cdn.aiolock import lock
from git_cdn.packet_line import PacketLineChunkParser
from git_cdn.util import get_subdir

log = getLogger()
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

# chunk size when reading the cache file
CHUNK_SIZE = 64 * 1024


class PackCache:
    """Upload pack cache
    when using a local cached repository, git upload-pack will recompress the whole repository,
    which is very CPU intensive.
    cache the binary pack content to disk
    """

    def __init__(self, hash):
        self.hash = hash
        self.dirname = get_subdir(os.path.join("pack_cache", self.hash[:2]))
        self.filename = os.path.join(self.dirname, self.hash)
        self.hit = True

    def read_lock(self):
        return lock(self.filename, mode=fcntl.LOCK_SH)

    def write_lock(self):
        return lock(self.filename, mode=fcntl.LOCK_EX)

    def delete(self):
        os.unlink(self.filename)

    def exists(self):
        if os.path.exists(self.filename) and os.stat(self.filename).st_size > 0:
            with open(self.filename, "rb") as f:
                f.seek(-4, os.SEEK_END)
                last_chunk = f.read(4)
                if last_chunk == b"0000":
                    return True
                log.warning("File in cache is corrupted", hash=self.hash)
        return False

    def size(self):
        return os.stat(self.filename).st_size

    async def send_pack(self, writer):
        status = "hit" if self.hit else "miss"
        bind_contextvars(
            upload_pack_status=status,
            cache={"size": self.size(), "filename": self.filename, "hit": self.hit},
        )
        # We always send the pack from the cache, even on cache Miss
        log.debug("Serving from pack cache", hash=self.hash, pack_hit=self.hit)
        with open(self.filename, "rb") as f:
            count = 0
            while True:
                data = f.read(CHUNK_SIZE)
                count += len(data)

                bind_contextvars(
                    upload_pack_progress={
                        "date": datetime.now().isoformat(),
                        "sent": count,
                    }
                )
                if not data:
                    if self.size() != count:
                        log.error("exiting on unfinished pack cache read")
                    break
                try:
                    await writer.write(data)
                except ConnectionResetError:
                    log.warning("connection reset while serving pack cache")
                    break
        # update mtime for LRU
        os.utime(self.filename, None)

    async def cache_pack(self, read_func):
        log.debug("Cache Miss, create new cache entry", hash=self.hash)
        self.hit = False
        pkt_parser = PacketLineChunkParser(read_func)
        with open(self.filename, "wb") as f:
            try:
                async for data in pkt_parser:
                    f.write(data)
            except Exception:
                log.exception(
                    "Aborting cache_pack", hash=self.hash, filename=self.filename
                )
                try:
                    os.unlink(self.filename)
                except FileNotFoundError:
                    pass


class FileLock:
    def __init__(self, filename):
        self.filename = filename
        self._f = None

    def exists(self):
        return os.path.exists(self.filename)

    def mtime(self):
        return os.stat(self.filename).st_mtime

    def __enter__(self):
        self._f = open(self.filename, "a+")
        fcntl.flock(self._f.fileno(), fcntl.LOCK_EX)
        os.utime(self.filename, None)
        return self

    def __exit__(self, exc_type, exc, tb):
        fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        self._f.close()
        self._f = None

    def delete(self):
        os.unlink(self.filename)


class PackCacheCleaner:
    def __init__(self):
        self.cache_dir = get_subdir("pack_cache")
        self.max_size = os.getenv("PACK_CACHE_SIZE_GB", "20")
        # Use cache size minus 512MB, to avoid exceeding the cache size too much.
        self.max_size = (int(self.max_size) * 1024 - 512) * 1024 * 1024
        self.lock = FileLock(os.path.join(self.cache_dir, "clean.lock"))

    def _clean_task(self):
        # When using os.scandir, DirEntry.stat() are cached (on Linux) and calling it
        # doesn't go through syscall
        subdirs = [d for d in os.scandir(self.cache_dir) if d.is_dir()]
        subdirs = [os.path.join(self.cache_dir, sub) for sub in subdirs]
        all_files = [f for sub in subdirs for f in os.scandir(sub) if f.is_file()]
        total_size = sum(f.stat().st_size for f in all_files)
        log.debug(
            "Pack Cache size is",
            size=total_size,
            max_size=self.max_size,
            n_entry=len(all_files),
        )

        if total_size < self.max_size:
            return 0

        all_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        rm_size = 0
        to_delete = []
        while total_size - rm_size >= self.max_size:
            f = all_files.pop()
            rm_size += f.stat().st_size
            to_delete.append(f)

        cache_duration = datetime.now() - datetime.fromtimestamp(
            to_delete[-1].stat().st_mtime
        )
        log.info(
            "Pack cache cleaning",
            size=total_size,
            max_size=self.max_size,
            rm_size=rm_size,
            rm_files=len(to_delete),
            cache_duration=cache_duration.total_seconds(),
        )
        for f in to_delete:
            with FileLock(f.path) as flock:
                log.debug("delete", hash=f.name, rm_size=f.stat().st_size)
                flock.delete()
        return len(to_delete)

    def clean_task(self):
        with self.lock:
            return self._clean_task()

    def clean(self):
        # only clean once per minute
        if self.lock.exists() and time() - self.lock.mtime() < 60:
            log.debug("No need to cleanup")
            return
        # This is a background task, so do not await it
        task = asyncio.get_event_loop().run_in_executor(executor, self.clean_task)

        return task
