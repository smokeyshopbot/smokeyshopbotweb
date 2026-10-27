"""Short, bounded snapshots for catalogue display only; never financial state."""
import asyncio
import copy
import time
from collections import OrderedDict


class AsyncDisplayCache:
    def __init__(self, maximum=256):
        self.maximum = maximum
        self.loop = None
        self.rows = OrderedDict()
        self.pending = {}
        self.generation = 0

    def clear(self):
        self.rows.clear()
        self.generation += 1

    async def get(self, key, loader, seconds=2):
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            self.loop = loop
            self.clear()
            self.pending.clear()
        cached = self.rows.get(key)
        if cached and cached[0] > time.monotonic():
            self.rows.move_to_end(key)
            return copy.deepcopy(cached[1])
        task = self.pending.get(key)
        if task is None:
            generation = self.generation
            async def load():
                value = await loader()
                if generation == self.generation:
                    self.rows[key] = (time.monotonic() + seconds, copy.deepcopy(value))
                    self.rows.move_to_end(key)
                    while len(self.rows) > self.maximum:
                        self.rows.popitem(last=False)
                return value
            task = asyncio.create_task(load())
            self.pending[key] = task
            def done(finished):
                if self.pending.get(key) is finished:
                    self.pending.pop(key, None)
                if not finished.cancelled():
                    finished.exception()  # Observe failures if every waiter was cancelled.
            task.add_done_callback(done)
        return copy.deepcopy(await asyncio.shield(task))
