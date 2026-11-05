"""
Concurrency primitives shared by the Docker backend.

The Docker SDK for Python is **synchronous**.  Every call in
:mod:`unixnodes.docker_backend` is therefore dispatched onto a bounded thread
pool with :func:`run_in_thread`, and long-running operations (image pulls,
container creation) are additionally serialised by module-level semaphores so a
burst of Discord commands can never fan out into unlimited Docker traffic.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from . import config

logger = logging.getLogger("unixnodes.runtime")

T = TypeVar("T")

_POOL: ThreadPoolExecutor | None = None


def get_pool() -> ThreadPoolExecutor:
    """Lazily create the shared worker pool used for blocking Docker calls."""
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(
            max_workers=config.DOCKER_WORKER_THREADS,
            thread_name_prefix="docker-worker",
        )
    return _POOL


async def run_in_thread(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """
    Run a blocking callable without stalling the Discord event loop.

    Unlike a bare :func:`asyncio.to_thread` this uses a *bounded* pool, so a
    hundred simultaneous Docker calls queue instead of spawning a hundred
    threads.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(get_pool(), lambda: ctx.run(func, *args, **kwargs))


def shutdown_pool(wait: bool = False) -> None:
    global _POOL
    if _POOL is not None:
        _POOL.shutdown(wait=wait)
        _POOL = None


class AsyncSemaphoreRegistry:
    """Named asyncio semaphores (created lazily inside the running loop)."""

    def __init__(self) -> None:
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._limits: dict[str, int] = {}

    def register(self, name: str, limit: int) -> None:
        self._limits[name] = max(1, int(limit))
        self._semaphores.pop(name, None)

    def acquire(self, name: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(name)
        if sem is None:
            sem = asyncio.Semaphore(self._limits.get(name, 1))
            self._semaphores[name] = sem
        return sem

    class _Holder:
        def __init__(self, registry: "AsyncSemaphoreRegistry", name: str) -> None:
            self._sem = registry.acquire(name)

        async def __aenter__(self) -> None:
            await self._sem.acquire()

        async def __aexit__(self, *exc: Any) -> None:
            self._sem.release()

    def guard(self, name: str) -> "_Holder":
        return AsyncSemaphoreRegistry._Holder(self, name)


LIMITS = AsyncSemaphoreRegistry()
LIMITS.register("image_pull", config.DOCKER_MAX_CONCURRENT_PULLS)
LIMITS.register("container_create", config.DOCKER_MAX_CONCURRENT_CREATES)


class ThreadSemaphoreRegistry:
    """
    Counting guards for code that already runs on a worker thread.

    Provisioning and backups execute inside :func:`run_in_thread`, where an
    ``asyncio.Semaphore`` cannot be awaited.  These give the same "never more
    than N in flight" protection with plain threading primitives.
    """

    def __init__(self) -> None:
        self._semaphores: dict[str, threading.Semaphore] = {}
        self._limits: dict[str, int] = {}
        self._lock = threading.Lock()

    def register(self, name: str, limit: int) -> None:
        with self._lock:
            self._limits[name] = max(1, int(limit))
            self._semaphores[name] = threading.Semaphore(self._limits[name])

    def semaphore(self, name: str) -> threading.Semaphore:
        with self._lock:
            sem = self._semaphores.get(name)
            if sem is None:
                sem = threading.Semaphore(self._limits.get(name, 1))
                self._semaphores[name] = sem
            return sem

    class _Holder:
        def __init__(self, registry: "ThreadSemaphoreRegistry", name: str) -> None:
            self._sem = registry.semaphore(name)

        def __enter__(self) -> None:
            self._sem.acquire()

        def __exit__(self, *exc: Any) -> None:
            self._sem.release()

    def guard(self, name: str) -> "_Holder":
        return ThreadSemaphoreRegistry._Holder(self, name)


THREAD_LIMITS = ThreadSemaphoreRegistry()
THREAD_LIMITS.register("image_pull", config.DOCKER_MAX_CONCURRENT_PULLS)
THREAD_LIMITS.register("container_create", config.DOCKER_MAX_CONCURRENT_CREATES)


def reload_limits() -> None:
    """Re-read the concurrency limits after a configuration change."""
    LIMITS.register("image_pull", config.DOCKER_MAX_CONCURRENT_PULLS)
    LIMITS.register("container_create", config.DOCKER_MAX_CONCURRENT_CREATES)
    THREAD_LIMITS.register("image_pull", config.DOCKER_MAX_CONCURRENT_PULLS)
    THREAD_LIMITS.register("container_create", config.DOCKER_MAX_CONCURRENT_CREATES)

