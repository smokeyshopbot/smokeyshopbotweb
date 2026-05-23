"""Binance Pay history polling helpers.

This uses the regular Binance SAPI Pay Trade History endpoint:
GET /sapi/v1/pay/transactions

It is intentionally read-only. The bot matches positive incoming USDT Pay
transactions against the exact unique amount assigned to a pending order/wallet
load.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import aiohttp

from config import (
    BINANCE_API_BASE_URL,
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    BINANCE_PAY_HISTORY_LOOKBACK_SECONDS,
    BINANCE_RECV_WINDOW_MS,
)

logger = logging.getLogger(__name__)

USDT_QUANT = Decimal("0.001")
USDT_LEGACY_QUANT = Decimal("0.000001")
_SERVER_TIME_OFFSET_MS: int | None = None
_SERVER_TIME_OFFSET_EXPIRES_AT = 0.0


@dataclass(slots=True)
class BinancePayHistoryResult:
    transactions: list[dict[str, Any]]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class BinancePayCheckResult:
    found: bool
    transaction: dict[str, Any] | None = None
    error: str | None = None

    def short_error_text(self) -> str:
        if self.found:
            tx_id = (self.transaction or {}).get("transactionId", "")
            return f"found tx {tx_id}" if tx_id else "found"
        return self.error or "not found"


@dataclass(slots=True)
class _RuntimeBinanceConfig:
    api_key: str
    api_secret: str
    base_url: str
    recv_window_ms: int
    lookback_seconds: int


def _amount_key(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _expected_amount_key(expected: Decimal) -> Decimal:
    """Use 3 decimals for new payments, but preserve old 6-decimal pending amounts."""
    if expected == expected.quantize(USDT_QUANT):
        return expected.quantize(USDT_QUANT)
    return expected.quantize(USDT_LEGACY_QUANT)


def _amount_match_details(actual: Decimal, expected: Decimal) -> dict | None:
    """Return match metadata only when the actual USDT amount exactly matches."""
    try:
        expected_q = _expected_amount_key(expected)
        # Do not round/truncate the actual Binance Pay transaction amount.
        if actual != expected_q:
            return None
        diff = abs(actual - expected_q).quantize(USDT_LEGACY_QUANT)
    except Exception:
        return None

    return {
        "actual": actual,
        "expected": expected_q,
        "difference": diff,
        "type": "exact",
    }


def _best_amount_match(amounts: list[Decimal], expected: Decimal) -> dict | None:
    matches = [m for amount in amounts if (m := _amount_match_details(amount, expected)) is not None]
    if not matches:
        return None
    # Exact match wins for this transaction row.
    return sorted(matches, key=lambda m: (m["type"] != "exact", m["difference"]))[0]


def _ms_from_created_at(created_at: Any) -> int:
    try:
        seconds = float(created_at)
    except (TypeError, ValueError):
        seconds = time.time()
    return max(0, int(seconds * 1000))


def _int_setting(value: Any, default: int, *, minimum: int | None = None) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = int(default)
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


async def _load_runtime_config() -> _RuntimeBinanceConfig:
    """Load Binance API settings live from MongoDB, with config.py values as fallback.

    Earlier builds loaded these once at process startup. That meant saving or
    correcting Binance credentials in WebAdmin did not affect the running bot
    until a restart, and the background watcher could keep polling with empty or
    old credentials. Reloading here makes Check Payment and auto-polling use the
    latest WebAdmin secret settings.
    """
    api_key = (BINANCE_API_KEY or "").strip()
    api_secret = (BINANCE_API_SECRET or "").strip()
    base_url = (BINANCE_API_BASE_URL or "https://api.binance.com").strip() or "https://api.binance.com"
    recv_window_ms = _int_setting(BINANCE_RECV_WINDOW_MS, 5000, minimum=1000)
    lookback_seconds = _int_setting(BINANCE_PAY_HISTORY_LOOKBACK_SECONDS, 3600, minimum=60)

    try:
        import database as db  # Imported lazily to avoid import-time cycles.

        settings = await db.get_setting("secret_settings", {})
        if isinstance(settings, dict):
            api_key = str(settings.get("binance_api_key") or api_key).strip()
            api_secret = str(settings.get("binance_api_secret") or api_secret).strip()
            base_url = str(settings.get("binance_api_base_url") or base_url).strip() or "https://api.binance.com"
            recv_window_ms = _int_setting(settings.get("binance_recv_window_ms", recv_window_ms), recv_window_ms, minimum=1000)
            lookback_seconds = _int_setting(settings.get("binance_pay_history_lookback_seconds", lookback_seconds), lookback_seconds, minimum=60)
    except Exception as exc:
        logger.warning("Could not reload Binance settings from DB; using startup settings: %s", exc)

    return _RuntimeBinanceConfig(
        api_key=api_key,
        api_secret=api_secret,
        base_url=base_url.rstrip("/"),
        recv_window_ms=recv_window_ms,
        lookback_seconds=lookback_seconds,
    )


def _signed_query(params: dict[str, Any], api_secret: str) -> str:
    query = urlencode(params)
    signature = hmac.new(
        api_secret.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{query}&signature={signature}"


async def _get_server_time_offset_ms(base_url: str, *, force_refresh: bool = False) -> int:
    """Return Binance server-time offset to avoid -1021 timestamp errors."""
    global _SERVER_TIME_OFFSET_MS, _SERVER_TIME_OFFSET_EXPIRES_AT

    now = time.time()
    if not force_refresh and _SERVER_TIME_OFFSET_MS is not None and now < _SERVER_TIME_OFFSET_EXPIRES_AT:
        return _SERVER_TIME_OFFSET_MS

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{base_url}/api/v3/time") as response:
                payload = await response.json(content_type=None)
                server_time = int(payload.get("serverTime"))
                local_time = int(time.time() * 1000)
                _SERVER_TIME_OFFSET_MS = server_time - local_time
                _SERVER_TIME_OFFSET_EXPIRES_AT = now + 600
                return _SERVER_TIME_OFFSET_MS
    except Exception as exc:
        logger.warning("Could not sync Binance server time, using local clock: %s", exc)
        _SERVER_TIME_OFFSET_MS = 0
        _SERVER_TIME_OFFSET_EXPIRES_AT = now + 60
        return 0


async def fetch_binance_pay_history(
    start_time_ms: int,
    end_time_ms: int | None = None,
    *,
    limit: int = 100,
) -> BinancePayHistoryResult:
    """Fetch Binance Pay history from the authenticated account.

    Binance returns both incoming and outgoing Pay activity. Positive amounts are
    income, so matching code below ignores negative amounts.
    """
    cfg = await _load_runtime_config()
    if not cfg.api_key or not cfg.api_secret:
        return BinancePayHistoryResult([], "Binance API key/secret is not configured in WebAdmin Secret Settings")

    now_ms = int(time.time() * 1000)
    end_ms = int(end_time_ms or now_ms)
    lookback_ms = max(60, int(cfg.lookback_seconds or 3600)) * 1000

    requested_start_ms = int(start_time_ms or (end_ms - lookback_ms))
    # Do not scan older than the configured lookback window; this keeps the
    # high-weight Binance endpoint fast and avoids stale rows causing huge ranges.
    safe_start_ms = max(0, min(requested_start_ms, end_ms), end_ms - lookback_ms)

    # Keep a small safety lookback for clock skew / delayed row creation.
    safe_start_ms = max(0, safe_start_ms - 120_000)

    async def _request_once(*, force_time_sync: bool = False) -> tuple[int, Any]:
        offset_ms = await _get_server_time_offset_ms(cfg.base_url, force_refresh=force_time_sync)
        params = {
            "startTime": safe_start_ms,
            "endTime": end_ms,
            "limit": max(1, min(int(limit or 100), 100)),
            "recvWindow": max(1000, int(cfg.recv_window_ms or 5000)),
            "timestamp": int(time.time() * 1000) + offset_ms,
        }
        url = f"{cfg.base_url}/sapi/v1/pay/transactions?{_signed_query(params, cfg.api_secret)}"
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"X-MBX-APIKEY": cfg.api_key}) as response:
                payload = await response.json(content_type=None)
                return response.status, payload

    try:
        status, payload = await _request_once()
        code = str(payload.get("code") if isinstance(payload, dict) else "")
        # Binance returns -1021 when the local server clock differs too much.
        if status >= 400 and code == "-1021":
            status, payload = await _request_once(force_time_sync=True)
        if status >= 400:
            return BinancePayHistoryResult([], f"Binance API HTTP {status}: {payload}")
    except Exception as exc:
        logger.exception("Binance Pay history request failed: %s", exc)
        return BinancePayHistoryResult([], f"Binance API request failed: {exc}")

    if not isinstance(payload, dict):
        return BinancePayHistoryResult([], "Binance API returned an unexpected response")

    success = payload.get("success")
    code = str(payload.get("code") or "")
    if success is False or (code and code != "000000"):
        return BinancePayHistoryResult([], f"Binance API error {code or 'unknown'}: {payload.get('message') or payload}")

    data = payload.get("data") or []
    if not isinstance(data, list):
        return BinancePayHistoryResult([], "Binance API response did not include a transaction list")

    return BinancePayHistoryResult([tx for tx in data if isinstance(tx, dict)])


def _transaction_usdt_amounts(tx: dict[str, Any]) -> list[Decimal]:
    """Return positive USDT amounts that could identify the payment."""
    amounts: list[Decimal] = []

    currency = str(tx.get("currency") or "").upper().strip()
    if currency == "USDT":
        amount = _amount_key(tx.get("amount"))
        if amount is not None and amount > 0:
            amounts.append(amount)

    # Some Binance Pay rows can include per-asset details. Use this as a
    # fallback so a row is still matchable when top-level metadata is unusual.
    funds_detail = tx.get("fundsDetail")
    if isinstance(funds_detail, list):
        for item in funds_detail:
            if not isinstance(item, dict):
                continue
            if str(item.get("currency") or "").upper().strip() != "USDT":
                continue
            amount = _amount_key(item.get("amount"))
            if amount is not None and amount > 0 and amount not in amounts:
                amounts.append(amount)

    return amounts


def find_matching_binance_pay_transaction(
    transactions: list[dict[str, Any]],
    expected_usdt: Any,
    created_at: Any,
    *,
    used_transaction_ids: set[str] | None = None,
) -> BinancePayCheckResult:
    expected_amount = _amount_key(expected_usdt)
    if expected_amount is None or expected_amount <= 0:
        return BinancePayCheckResult(False, error="invalid expected Binance Pay amount")

    created_ms = _ms_from_created_at(created_at)
    used_transaction_ids = used_transaction_ids or set()

    # Oldest first is safer if several rows are returned with similar amounts.
    sorted_transactions = sorted(transactions, key=lambda tx: int(tx.get("transactionTime") or 0))

    for tx in sorted_transactions:
        tx_id = str(tx.get("transactionId") or tx.get("tranId") or "").strip()
        if not tx_id or tx_id in used_transaction_ids:
            continue

        order_type = str(tx.get("orderType") or "").upper().strip()
        # Personal Binance Pay sends usually appear as C2C. Merchant-style rows
        # can be PAY. Keep C2C_HOLDING too because Binance documents it as a
        # transfer to a new Binance user; refunds/payouts/remittance are ignored.
        if order_type and order_type not in {"C2C", "PAY", "C2C_HOLDING"}:
            continue

        match = _best_amount_match(_transaction_usdt_amounts(tx), expected_amount)
        if match is None:
            continue

        try:
            tx_time = int(tx.get("transactionTime") or 0)
        except (TypeError, ValueError):
            tx_time = 0
        if tx_time and tx_time + 30_000 < created_ms:
            continue

        matched_tx = dict(tx)
        matched_tx.update({
            "match_actual_usdt": str(match["actual"]),
            "match_expected_usdt": str(match["expected"]),
            "match_difference_usdt": str(match["difference"]),
            "match_type": match["type"],
        })
        return BinancePayCheckResult(True, transaction=matched_tx)

    return BinancePayCheckResult(False, error="matching incoming USDT Binance Pay transaction not found")


async def check_binance_pay_received_detailed(
    expected_usdt: Any,
    created_at: Any,
    *,
    used_transaction_ids: set[str] | None = None,
) -> BinancePayCheckResult:
    start_ms = _ms_from_created_at(created_at)
    history = await fetch_binance_pay_history(start_ms)
    if not history.ok:
        return BinancePayCheckResult(False, error=history.error)
    return find_matching_binance_pay_transaction(
        history.transactions,
        expected_usdt,
        created_at,
        used_transaction_ids=used_transaction_ids,
    )
