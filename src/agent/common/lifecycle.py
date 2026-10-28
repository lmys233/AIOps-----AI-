"""Explicit ownership, worker-first shutdown and bounded cleanup; never deletes data."""
import asyncio
import inspect
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Resource:
    name: str
    get: object
    method: str | None = None
    worker: bool = False


class Resources:
    def __init__(self, *, timeout=5., total_timeout=20.):
        self.timeout, self.total_timeout = timeout, total_timeout
        self.resources, self.starters, self.errors = [], [], []
        self.closed = False
        self._closing = None

    def own(self, name, resource, *, method=None, worker=False, lazy=False):
        if self.closed: raise RuntimeError('resource owner already closed')
        self.resources.append(Resource(name, resource if lazy else lambda: resource, method, worker))

    def start(self, callback): self.starters.append(callback)

    async def bounded(self, callback, seconds):
        result = callback()
        if not inspect.isawaitable(result): return
        task = asyncio.ensure_future(result)
        # Enter async close even after the overall deadline, so its cancellation
        # finalizer can terminate an owned pool rather than leaving it unopened.
        await asyncio.sleep(0)
        done, _ = await asyncio.wait({task}, timeout=max(0., seconds))
        if done: return await task
        task.cancel()
        # A faulty close must not hold every other owner's resources hostage.
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        raise TimeoutError('resource close deadline')

    async def _close(self):
        deadline = time.monotonic() + self.total_timeout
        seen = set()
        for entry in sorted(reversed(self.resources), key=lambda e: not e.worker):
            try:
                resource = entry.get()
                if resource is None: continue
                key = (id(resource), entry.method or 'close')
                if key in seen: continue
                seen.add(key)
                method = entry.method or ('aclose' if hasattr(resource, 'aclose') else 'close')
                callback = getattr(resource, method, None)
                if callback:
                    await self.bounded(callback, min(self.timeout, max(0., deadline-time.monotonic())))
            except Exception as exc:
                self.errors.append({'resource': entry.name, 'error': type(exc).__name__})
                log.warning('Resource shutdown incomplete: %s (%s)', entry.name, type(exc).__name__)

    async def close(self):
        if self._closing is None:
            self.closed = True
            self._closing = asyncio.create_task(self._close())
        await asyncio.shield(self._closing)

    @asynccontextmanager
    async def lifespan(self, app):
        if self.closed:raise RuntimeError('application resources already closed; construct a fresh app')
        try:
            for callback in self.starters: await callback()
            yield
        finally:
            await self.close()


async def stop_tasks(tasks):
    pending = {task for task in tasks if task is not None and not task.done()}
    for task in pending:
        if not task.cancelling():task.cancel()
    if pending: await asyncio.gather(*pending, return_exceptions=True)


async def close_pool(pool, timeout=3.):
    if pool is None: return
    try: await asyncio.wait_for(pool.close(), timeout)
    except BaseException:
        # asyncpg terminate is synchronous; only this owner's connections are affected.
        pool.terminate()
        raise
