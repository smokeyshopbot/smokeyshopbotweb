"""The advertised public API origin, separate from the private admin path."""
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

DEFAULTS = {"buyer_api_endpoint_url": ""}


def normalize_endpoint_url(value):
    value = str(value or "").strip()
    if not value:
        return ""
    error = "API endpoint URL must be a public root URL, e.g. https://example.com (no admin path, API path, credentials, query or fragment). For local testing use http://127.0.0.1:8080."
    if len(value) > 2048 or re.search(r"[\s\\`<>]", value):
        raise ValueError(error)
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme.lower() not in {"https", "http"} or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError(error)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            domain = host.encode("idna").decode("ascii")
            if len(domain) > 253 or not all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part)
                                          for part in domain.split(".")):
                raise ValueError(error)
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), "", "", ""))
    except (ValueError, UnicodeError):
        raise ValueError(error) from None
