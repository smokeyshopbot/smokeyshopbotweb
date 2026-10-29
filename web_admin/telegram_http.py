"""One persistent Telegram HTTP session per calling thread; POSTs never auto-retry."""
import threading

import requests
from requests.adapters import HTTPAdapter

_local = threading.local()


def post(url, **kwargs):
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        # Retrying an uncertain sendMessage can create duplicate notifications.
        session.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0))
        _local.session = session
    return session.post(url, **kwargs)
