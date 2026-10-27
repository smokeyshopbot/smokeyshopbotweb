"""Local timing and background admission; no query values or credentials logged."""
import contextvars
import threading
import time
from collections import deque
from pymongo.monitoring import CommandListener

current = contextvars.ContextVar('shop_performance_profile', default=None)


class Profile:
    def __init__(self):
        self.commands = 0
        self.database_seconds = 0.0
        self.slowest = ('', 0.0)
        self.lock = threading.Lock()

    def add(self, name, seconds):
        with self.lock:
            self.commands += 1
            self.database_seconds += seconds
            if seconds > self.slowest[1]:
                self.slowest = (name, seconds)


class MongoMonitor(CommandListener):
    def __init__(self):
        self.lock = threading.Lock()
        self.running = {}
        self.recent = deque(maxlen=4096)

    def started(self, event):
        with self.lock:
            self.running[(event.connection_id, event.request_id)] = current.get()
            self.recent.append(time.monotonic())

    def _done(self, event):
        with self.lock:
            profile = self.running.pop((event.connection_id, event.request_id), None)
        if profile is not None:
            profile.add(event.command_name, event.duration_micros / 1_000_000)

    def succeeded(self, event):
        self._done(event)

    def failed(self, event):
        self._done(event)

    def background_busy(self):
        with self.lock:
            cutoff = time.monotonic() - 1
            while self.recent and self.recent[0] < cutoff:
                self.recent.popleft()
            return len(self.recent) >= 45


monitor = MongoMonitor()
