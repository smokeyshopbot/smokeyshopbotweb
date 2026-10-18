"""Timed sale prices shared by shop, checkout, and reseller API."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def utc(value):
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def active_sale(product, at=None):
    sale = (product or {}).get('sale') or {}
    current = at or datetime.now(timezone.utc)
    start, end = utc(sale.get('starts_at')), utc(sale.get('ends_at'))
    return sale if sale.get('enabled') and start and end and start <= current < end else {}


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Enter a valid amount.') from None
    if not result.is_finite() or result <= 0 or result > Decimal('1000000000') or result != result.quantize(Decimal('0.01')):
        raise ValueError('Amounts must be positive, at most 1,000,000,000, and have at most two decimal places.')
    return float(result)


def apply_sale(product, at=None):
    result = dict(product)
    sale = active_sale(result, at)
    result['sale_active'] = False
    for currency in ('inr', 'usdt'):
        field = 'price_' + currency
        regular = float(result.get(field) or 0)
        result['regular_' + field] = regular
        value = sale.get(field)
        if value is not None:
            try:
                value = amount(value)
            except ValueError:
                continue
            if value < regular:
                result[field] = value
                result['sale_active'] = True
    if result['sale_active']:
        result['effective_price_source'] = 'sale'
        result['sale_ends_at'] = utc(sale['ends_at'])
    return result
