"""Small, process-wide pacing helper for concurrent product notices."""

import threading
import time


class DeliveryPacer:
    def __init__(self, per_second: int = 20):
        self.interval = 1.0 / max(1, per_second)
        self.lock = threading.Lock()
        self.next_send = 0.0
        self.paused_until = 0.0

    def wait(self) -> None:
        # Recheck after sleeping: another worker may have received a Telegram
        # retry_after response while this worker was waiting for its slot.
        while True:
            with self.lock:
                now = time.monotonic()
                delay = max(self.next_send, self.paused_until) - now
                if delay <= 0:
                    self.next_send = now + self.interval
                    return
            time.sleep(delay)

    def defer(self, seconds: float) -> None:
        with self.lock:
            self.paused_until = max(self.paused_until, time.monotonic() + max(0, seconds))
