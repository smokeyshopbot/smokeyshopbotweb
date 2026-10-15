"""Owner-configured hostname separation for one WebAdmin/API service."""
import ipaddress
import re
from urllib.parse import urlsplit

from utils.buyer_api_settings import normalize_endpoint_url

DEFAULTS = {"admin_panel_url": "", "domain_separation_enabled": "0"}
DOMAIN_KEYS = frozenset((*DEFAULTS, "buyer_api_endpoint_url"))
ROUTING_KEYS = DOMAIN_KEYS | {"admin_panel_path"}
RECOVERY_ENV = "ADMIN_DOMAIN_SEPARATION_DISABLED"


def hostname(authority):
    """Parse the actual Host authority, never a client-supplied forwarded header."""
    authority = str(authority or "")
    if not authority or re.search(r"[\s\\/@?#%,;`<>]", authority) or any(ord(c) < 32 for c in authority):
        raise ValueError("Invalid host")
    try:
        parsed = urlsplit("http://" + authority)
        host, port = parsed.hostname, parsed.port
        if not host or parsed.path or (port is not None and not 1 <= port <= 65535) or authority.endswith(":"):
            raise ValueError("Invalid host")
        try:
            return ipaddress.ip_address(host).compressed.lower()
        except ValueError:
            host = host.removesuffix(".").encode("idna").decode("ascii").lower()
            if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                          for label in host.split(".")):
                raise ValueError("Invalid host")
            return host
    except (ValueError, UnicodeError):
        raise ValueError("Invalid host") from None


def origin_hostname(origin):
    return hostname(urlsplit(origin).netloc)


def validate_settings(settings):
    enabled = str(settings.get("domain_separation_enabled", "0"))
    if enabled not in {"0", "1"}:
        raise ValueError("Domain separation must be enabled or disabled.")
    api_url = normalize_endpoint_url(settings.get("buyer_api_endpoint_url"))
    try:
        admin_url = normalize_endpoint_url(settings.get("admin_panel_url"))
    except ValueError as error:
        raise ValueError(str(error).replace("API endpoint URL", "Admin panel URL")) from None
    if enabled == "1":
        if not admin_url or not api_url:
            raise ValueError("Set both the Admin panel URL and API Endpoint URL before enabling separate domains.")
        if origin_hostname(admin_url) == origin_hostname(api_url):
            raise ValueError("Admin and API URLs must use different hostnames; different ports or schemes are not enough.")
    return {"domain_separation_enabled": enabled, "admin_panel_url": admin_url,
            "buyer_api_endpoint_url": api_url}


def routing_settings(db):
    doc = db.settings.find_one({"key": "secret_settings"},
                               {"value." + key: 1 for key in ROUTING_KEYS}) or {}
    return doc.get("value") or {}
