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
        self.interactive = deque(maxlen=4096)
        self.latencies = deque(maxlen=256)

    def started(self, event):
        command = getattr(event, 'command', {}) or {}
        name = event.command_name
        collection = command.get('collection') if name == 'getMore' else command.get(name)
        label = name + '/' + collection[:80] if isinstance(collection, str) else name
        comment = command.get('comment')
        if isinstance(comment, str) and comment.startswith('history_page:'):
            label += ' [' + comment[:100] + ']'
        with self.lock:
            self.running[(event.connection_id, event.request_id)] = (current.get(), label)
            self.recent.append(time.monotonic())
            if current.get() is not None:
                self.interactive.append(time.monotonic())

    def _done(self, event):
        with self.lock:
            profile, label = self.running.pop((event.connection_id, event.request_id), (None, event.command_name))
            self.latencies.append((time.monotonic(), event.duration_micros / 1_000_000))
        if profile is not None:
            profile.add(label, event.duration_micros / 1_000_000)

    def succeeded(self, event):
        self._done(event)

    def failed(self, event):
        self._done(event)

    def background_busy(self):
        return bool(self.pressure()['reason'])

    def pressure(self):
        with self.lock:
            current_time = time.monotonic()
            cutoff = current_time - 1
            while self.recent and self.recent[0] < cutoff:
                self.recent.popleft()
            while self.interactive and self.interactive[0] < cutoff:
                self.interactive.popleft()
            while self.latencies and self.latencies[0][0] < current_time - 2:
                self.latencies.popleft()
            slow = len(self.latencies) >= 5 and sum(t for _, t in self.latencies) / len(self.latencies) >= .25
            reason = ('Interactive database traffic' if len(self.interactive) >= 45 else
                      'Database response time' if slow else
                      'Database command budget' if len(self.recent) >= 80 else '')
            return {'commands_per_second': len(self.recent), 'interactive_commands_per_second': len(self.interactive),
                    'reason': reason}


monitor = MongoMonitor()
