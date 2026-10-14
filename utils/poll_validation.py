"""Shared validation for native Telegram survey polls."""
import re


def validate_poll(question, options, multiple=False, *, max_option_length=100):
    question = str(question or "").strip()
    options = [str(option).strip() for option in options if str(option).strip()]
    if not 1 <= len(question.encode('utf-16-le')) // 2 <= 300:
        raise ValueError("Question must contain 1–300 characters.")
    if not 2 <= len(options) <= 10:
        raise ValueError("Enter 2–10 separate answer options.")
    if any(len(option.encode('utf-16-le')) // 2 > max_option_length for option in options):
        raise ValueError(f"Each option must contain at most {max_option_length} characters.")
    if len({option.casefold() for option in options}) != len(options):
        raise ValueError("Options must be different from each other.")
    return question, options, bool(multiple)


def audience_query(audience):
    query = {"blocked": {"$ne": True}}
    language = audience.get("language", "all")
    if language not in {"all", "en", "es"}:
        raise ValueError("Choose All, English, or Spanish.")
    if language == "es":
        query["language"] = "es"
    elif language == "en":
        query["language"] = {"$ne": "es"}
    group = audience.get("pricing_group", "all")
    if group not in {"all", "normal", "vip", "reseller", "wholesale"}:
        raise ValueError("Invalid pricing group.")
    if group == "normal":
        query["pricing_group"] = {"$in": ["normal", None, ""]}
    elif group != "all":
        query["pricing_group"] = group
    ids = audience.get("user_ids") or []
    if ids:
        query["user_id"] = {"$in": ids}
    return query


def parse_user_ids(value):
    if not str(value or "").strip():
        return []
    parts = re.split(r"[\s,]+", str(value).strip())
    if any(not part.isdigit() or int(part) <= 0 for part in parts):
        raise ValueError("User IDs must be positive numbers separated by spaces or commas.")
    if len(parts) > 1000:
        raise ValueError("Enter at most 1,000 specific user IDs.")
    return sorted({int(part) for part in parts})
