"""Avoid repeated user upserts within one update; never cache wallet reads."""
from contextvars import ContextVar
import asyncio
import time
import functools

upserts = ContextVar('shop_update_upserts', default=None)


class UpdateCache(dict):
    def __init__(self):
        super().__init__()
        self.owner = asyncio.current_task()
        self.timings = {}


def current_upserts():
    value = upserts.get()
    task = asyncio.current_task()
    return value if value is not None and getattr(value, 'owner', task) is task else None


def record_timing(stage, elapsed):
    # Timing may include parallel child reads; profile caching remains task-local.
    context = upserts.get()
    if context is not None and hasattr(context, 'timings'):
        total, count, peak = context.timings.get(stage, (0.0, 0, 0.0))
        context.timings[stage] = (total + elapsed, count + 1, max(peak, elapsed))


def timed(stage):
    def decorate(function):
        @functools.wraps(function)
        async def measured(*args, **kwargs):
            started = time.monotonic()
            try:
                return await function(*args, **kwargs)
            finally:
                record_timing(stage, time.monotonic() - started)
        return measured
    return decorate
