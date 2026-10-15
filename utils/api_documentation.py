"""Read-only API help with escaped, copyable examples and no real credentials."""
import html
import json

from utils.buyer_api_settings import normalize_endpoint_url
from utils.i18n import tr

def text(summary=None, lang="en"):
    summary = summary or {}
    try:
        endpoint = normalize_endpoint_url(summary.get("endpoint_url"))
    except ValueError:
        endpoint = ""
    root = endpoint or "https://YOUR_SHOP_DOMAIN"
    def code(value):
        return "<code>" + html.escape(str(value), quote=False) + "</code>"
    def example(value):
        return "<pre>" + html.escape(str(value), quote=False) + "</pre>"
    def route(method, path):
        return code(method) + "\n" + code(root + "/api/telegram-buyer/" + path)
    separator = "─" * 24
    payload = json.dumps({"product_id": "PRODUCT_ID", "quantity": 1,
                          "client_order_id": "YOUR_ORDER_ID"}, indent=2)
    sections = [tr(lang, "api_docs_title"), tr(lang, "api_docs_intro")]
    if not endpoint:
        sections.append(tr(lang, "api_docs_endpoint_placeholder"))
    sections.extend([
        separator, tr(lang, "api_docs_auth_title"), tr(lang, "api_docs_auth_help"),
        code("Authorization: Bearer YOUR_API_KEY"),
        separator, tr(lang, "api_docs_products_title"), route("GET", "products"),
        separator, tr(lang, "api_docs_balance_title"), route("GET", "balance"),
        separator, tr(lang, "api_docs_order_title"), route("POST", "purchase"),
        tr(lang, "api_docs_json_title"), example(payload), tr(lang, "api_docs_order_note"),
        separator, tr(lang, "api_docs_lookup_title"), route("GET", "orders/ORDER_ID"),
        tr(lang, "api_docs_lookup_note"), tr(lang, "api_failed_attempt_hint"), tr(lang, "api_docs_uncertain_note"),
    ])
    return "\n\n".join(sections)
