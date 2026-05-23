"""
utils/crypto.py — Unique 3-decimal amount generator for USDT payments.

The bot shows payment amounts with 3 decimals. For a $1.00 payment, it only
uses amounts from 1.001 to 1.099, so users see a simple 1.0xx amount while
pending payments still remain unique for exact matching.
"""

from decimal import Decimal, InvalidOperation, ROUND_CEILING
import random

from database import get_all_pending_unique_usdt_payments

USDT_PAYMENT_QUANT = Decimal("0.001")
USDT_CENT_QUANT = Decimal("0.01")
USDT_RANDOM_SUFFIX_START = 1
USDT_RANDOM_SUFFIX_END = 99


class UniqueUsdtAmountUnavailable(RuntimeError):
    """Raised when all 0.001–0.099 exact-match slots are already in use."""


def _ceil_to_cent(value: float | str | Decimal) -> Decimal:
    """Round the real order amount up to cents before adding a tiny suffix."""
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    if amount <= 0:
        return USDT_CENT_QUANT
    return amount.quantize(USDT_CENT_QUANT, rounding=ROUND_CEILING)


def _payment_amount(value: Decimal) -> Decimal:
    return value.quantize(USDT_PAYMENT_QUANT)


async def generate_unique_usdt_amount(base_amount: float) -> float:
    """
    Return a unique 3-decimal USDT amount for exact auto-verification.

    The amount always stays within base + 0.001 to base + 0.099.
    Examples:
      $1.00  -> 1.001 to 1.099
      $10.25 -> 10.251 to 10.349

    The suffix order is randomized so users do not see a predictable +0.001
    pattern. If all 99 same-total pending slots are already taken, this raises
    UniqueUsdtAmountUnavailable instead of moving outside the requested range.
    """
    existing = await get_all_pending_unique_usdt_payments()
    existing_amounts: set[Decimal] = set()
    for pending in existing:
        try:
            existing_amounts.add(_payment_amount(Decimal(str(pending.get("unique_usdt", 0)))))
        except (InvalidOperation, TypeError, ValueError):
            continue

    base_cent = _ceil_to_cent(base_amount)
    suffixes = list(range(USDT_RANDOM_SUFFIX_START, USDT_RANDOM_SUFFIX_END + 1))
    random.shuffle(suffixes)

    for suffix in suffixes:
        candidate = _payment_amount(base_cent + (USDT_PAYMENT_QUANT * suffix))
        if candidate not in existing_amounts:
            return float(candidate)

    raise UniqueUsdtAmountUnavailable(
        "All 99 exact-match USDT payment slots are already in use for this amount"
    )
