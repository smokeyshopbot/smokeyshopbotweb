"""
utils/bscscan.py — USDT BEP20 transaction verification helpers.

Auto verification checks incoming USDT BEP20 transfers to the wallet address
stored in MongoDB payment settings and matches the unique decimal amount shown to the buyer.

Why multiple sources?
- Legacy BscScan-style APIs can fail/deprecate/rate-limit.
- Etherscan API V2 is the newer multichain API, but BNB Chain can require the
  correct API key/tier.
- BSC RPC log scanning is the best no-explorer fallback, but many public RPCs
  disable eth_getLogs. This file supports multiple RPC URLs and tries them in
  small block chunks.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp

from config import (
    BSCSCAN_API_KEY, ETHERSCAN_API_KEY, BSC_RPC_URL, BSC_RPC_URLS,
    USDT_LOOKBACK_SECONDS, BSC_RPC_BLOCK_CHUNK_SIZE, BEP20_REQUIRED_CONFIRMATIONS,
)

logger = logging.getLogger(__name__)

USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Legacy BscScan-style endpoint. Kept for older keys/setups.
BSCSCAN_BASE = "https://api.bscscan.com/api"

# Etherscan V2 multichain endpoint. For BNB Smart Chain use chainid=56.
ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"

# RPC URLs are managed in WebAdmin → Secret Settings.
_DEFAULT_RPC_URLS = [
    BSC_RPC_URL,
    "https://bsc-rpc.publicnode.com",
    "https://bsc.drpc.org",
    "https://rpc.ankr.com/bsc",
]
BSC_RPC_URLS_LIST = [
    url.strip().rstrip("/")
    for url in (BSC_RPC_URLS or ",".join(_DEFAULT_RPC_URLS)).split(",")
    if url.strip()
]

LOOKBACK_SECONDS = int(USDT_LOOKBACK_SECONDS or 3600)
USDT_DECIMALS = Decimal("1000000000000000000")
RPC_BLOCK_CHUNK_SIZE = max(10, int(BSC_RPC_BLOCK_CHUNK_SIZE or 450))
REQUIRED_CONFIRMATIONS = max(1, int(BEP20_REQUIRED_CONFIRMATIONS or 3))
USDT_QUANT = Decimal("0.001")
USDT_LEGACY_QUANT = Decimal("0.000001")


@dataclass
class UsdtCheckResult:
    found: bool = False
    source: str | None = None
    tx: dict | None = None
    errors: list[str] = field(default_factory=list)

    def short_error_text(self) -> str:
        if not self.errors:
            return "No matching USDT BEP20 transfer found yet."
        return " | ".join(self.errors[-3:])


async def check_usdt_received(
    expected_amount: float | str | Decimal,
    lookback: int = LOOKBACK_SECONDS,
    *,
    wallet_address: str | None = None,
) -> bool:
    """Backward-compatible boolean check used by existing payment flow."""
    result = await check_usdt_received_detailed(expected_amount, lookback, wallet_address=wallet_address)
    return result.found


async def check_usdt_received_detailed(
    expected_amount: float | str | Decimal,
    lookback: int = LOOKBACK_SECONDS,
    *,
    wallet_address: str | None = None,
) -> UsdtCheckResult:
    """
    Return detailed verification status for an expected incoming USDT BEP20 amount.
    """
    result = UsdtCheckResult()
    expected = _to_decimal(expected_amount)
    if expected is None:
        result.errors.append(f"Invalid expected amount: {expected_amount!r}")
        logger.warning("USDT check skipped: invalid expected amount %r", expected_amount)
        return result

    wallet_address = (wallet_address or "").strip()
    if not _address_to_topic(wallet_address):
        result.errors.append("Invalid or missing BEP20 wallet address in Payment Settings")
        logger.warning("USDT check skipped: invalid payment settings wallet %r", wallet_address)
        return result

    # 1) Try legacy BscScan endpoint.
    tx, error = await _details_via_legacy_bscscan(expected, lookback, wallet_address)
    if tx:
        result.found = True
        result.source = tx.get("source", "legacy_bscscan")
        result.tx = tx
        return result
    if error:
        result.errors.append(error)

    # 2) Try Etherscan V2 if an API key exists.
    if ETHERSCAN_API_KEY:
        tx, error = await _details_via_etherscan_v2(expected, lookback, wallet_address)
        if tx:
            result.found = True
            result.source = tx.get("source", "etherscan_v2")
            result.tx = tx
            return result
        if error:
            result.errors.append(error)
    else:
        result.errors.append("ETHERSCAN_API_KEY/BSCSCAN_API_KEY missing; skipping Etherscan V2")

    # 3) Fallback: read BEP20 Transfer logs directly from BSC RPC providers.
    tx, rpc_errors = await _details_via_bsc_rpc_logs(expected, lookback, wallet_address)
    if tx:
        result.found = True
        result.source = tx.get("source", "bsc_rpc_logs")
        result.tx = tx
        return result
    result.errors.extend(rpc_errors)

    logger.info(
        "USDT auto-check not found. expected=%s wallet=%s errors=%s",
        expected, wallet_address, result.short_error_text(),
    )
    return result


async def get_usdt_tx_details(expected_amount: float | str | Decimal, *, wallet_address: str | None = None) -> dict | None:
    """Return a matching tx/log dict if found, otherwise None."""
    result = await check_usdt_received_detailed(expected_amount, LOOKBACK_SECONDS, wallet_address=wallet_address)
    return result.tx if result.found else None


# ───────────────────────── API SOURCES ─────────────────────────

async def _details_via_legacy_bscscan(expected: Decimal, lookback: int, wallet_address: str) -> tuple[dict | None, str | None]:
    params = {
        "module": "account",
        "action": "tokentx",
        "contractaddress": USDT_CONTRACT,
        "address": wallet_address,
        "sort": "desc",
        "offset": "100",
        "page": "1",
    }
    if BSCSCAN_API_KEY:
        params["apikey"] = BSCSCAN_API_KEY

    try:
        data = await _http_get_json(BSCSCAN_BASE, params=params)
    except Exception as exc:
        error = f"Legacy BscScan failed: {exc}"
        logger.warning(error)
        return None, error

    if data.get("status") != "1":
        msg = str(data.get("message") or "NOTOK")
        res = str(data.get("result") or "")[:160]
        error = f"Legacy BscScan returned {msg}: {res}"
        logger.warning(error)
        return None, error

    tx = _match_api_transfer(data.get("result", []), expected, lookback, wallet_address)
    if tx:
        tx["source"] = "legacy_bscscan"
    return tx, None


async def _details_via_etherscan_v2(expected: Decimal, lookback: int, wallet_address: str) -> tuple[dict | None, str | None]:
    params = {
        "chainid": "56",  # BNB Smart Chain mainnet
        "module": "account",
        "action": "tokentx",
        "contractaddress": USDT_CONTRACT,
        "address": wallet_address,
        "sort": "desc",
        "offset": "100",
        "page": "1",
        "apikey": ETHERSCAN_API_KEY,
    }

    try:
        data = await _http_get_json(ETHERSCAN_V2_BASE, params=params)
    except Exception as exc:
        error = f"Etherscan V2 failed: {exc}"
        logger.warning(error)
        return None, error

    if data.get("status") != "1":
        msg = str(data.get("message") or "NOTOK")
        res = str(data.get("result") or "")[:160]
        error = f"Etherscan V2 returned {msg}: {res}"
        logger.warning(error)
        return None, error

    tx = _match_api_transfer(data.get("result", []), expected, lookback, wallet_address)
    if tx:
        tx["source"] = "etherscan_v2"
    return tx, None


async def _details_via_bsc_rpc_logs(expected: Decimal, lookback: int, wallet_address: str) -> tuple[dict | None, list[str]]:
    wallet_topic = _address_to_topic(wallet_address)
    if not wallet_topic:
        return None, ["Invalid or missing BEP20 wallet address in Payment Settings"]

    errors: list[str] = []

    for rpc_url in BSC_RPC_URLS_LIST:
        try:
            latest_hex = await _rpc_call(rpc_url, "eth_blockNumber", [])
            latest_block = int(latest_hex, 16)
        except Exception as exc:
            errors.append(f"{_safe_rpc_name(rpc_url)} blockNumber failed: {exc}")
            continue

        # BSC block time is usually around 3 seconds. Use a buffer so slow blocks
        # and small timestamp differences do not cause missed payments.
        block_lookback = max(1200, int(lookback / 3) + 300)
        from_block = max(0, latest_block - block_lookback)

        try:
            tx = await _scan_rpc_logs_in_chunks(
                rpc_url=rpc_url,
                from_block=from_block,
                to_block=latest_block,
                wallet_topic=wallet_topic,
                wallet_address=wallet_address,
                expected=expected,
                latest_block=latest_block,
            )
            if tx:
                return tx, errors
        except Exception as exc:
            errors.append(f"{_safe_rpc_name(rpc_url)} getLogs failed: {exc}")
            continue

    return None, errors


async def _scan_rpc_logs_in_chunks(
    rpc_url: str,
    from_block: int,
    to_block: int,
    wallet_topic: str,
    wallet_address: str,
    expected: Decimal,
    latest_block: int,
) -> dict | None:
    # Scan newest chunks first so recent payments are detected faster.
    end = to_block
    while end >= from_block:
        start = max(from_block, end - RPC_BLOCK_CHUNK_SIZE + 1)
        logs = await _rpc_call(rpc_url, "eth_getLogs", [{
            "address": USDT_CONTRACT,
            "fromBlock": hex(start),
            "toBlock": hex(end),
            "topics": [TRANSFER_TOPIC, None, wallet_topic],
        }])

        if not isinstance(logs, list):
            raise RuntimeError(f"unexpected log payload: {logs!r}")

        # Newest block chunk first; inside the chunk, reverse logs so newest first.
        for log in reversed(logs):
            raw_value = log.get("data", "0x0")
            value = _raw_token_value_to_decimal(raw_value)
            match = _amount_match_details(value, expected) if value is not None else None
            if match is not None:
                log_block = _parse_int(log.get("blockNumber"))
                confirmations = (latest_block - log_block + 1) if log_block is not None else 0
                if confirmations < REQUIRED_CONFIRMATIONS:
                    logger.info(
                        "Matching USDT transfer found but waiting for confirmations. tx=%s confirmations=%s required=%s",
                        log.get("transactionHash"), confirmations, REQUIRED_CONFIRMATIONS,
                    )
                    continue
                return {
                    "hash": log.get("transactionHash"),
                    "txhash": log.get("transactionHash"),
                    "to": wallet_address,
                    "value": str(int(Decimal(value) * USDT_DECIMALS)),
                    "value_usdt": str(value),
                    "match_actual_usdt": str(match["actual"]),
                    "match_expected_usdt": str(match["expected"]),
                    "match_difference_usdt": str(match["difference"]),
                    "match_type": match["type"],
                    "source": f"bsc_rpc_logs:{_safe_rpc_name(rpc_url)}",
                    "blockNumber": log.get("blockNumber"),
                    "confirmations": confirmations,
                }
        end = start - 1

    return None


# ───────────────────────── HELPERS ─────────────────────────

async def _http_get_json(url: str, params: dict[str, Any]) -> dict:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
            return await resp.json(content_type=None)


async def _rpc_call(rpc_url: str, method: str, params: list[Any]) -> Any:
    payload = {"jsonrpc": "2.0", "id": int(time.time() * 1000), "method": method, "params": params}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(rpc_url, json=payload) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
            data = await resp.json(content_type=None)

    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result")


def _match_api_transfer(transfers: list[dict], expected: Decimal, lookback: int, wallet_address: str) -> dict | None:
    cutoff = int(time.time()) - lookback
    wallet = wallet_address.lower()

    for tx in transfers:
        try:
            if int(tx.get("timeStamp", "0") or 0) < cutoff:
                continue
        except ValueError:
            continue

        if tx.get("to", "").lower() != wallet:
            continue

        # Confirm it is the real USDT BEP20 contract.
        if tx.get("contractAddress", USDT_CONTRACT).lower() != USDT_CONTRACT.lower():
            continue

        try:
            decimals = int(tx.get("tokenDecimal", "18") or 18)
            value = Decimal(tx.get("value", "0")) / (Decimal(10) ** decimals)
        except (InvalidOperation, ValueError):
            continue

        match = _amount_match_details(value, expected)
        if match is not None:
            if not _api_tx_has_required_confirmations(tx):
                logger.info(
                    "Matching USDT transfer found but waiting for confirmations. hash=%s confirmations=%s required=%s",
                    tx.get("hash") or tx.get("txhash"), tx.get("confirmations"), REQUIRED_CONFIRMATIONS,
                )
                continue
            matched_tx = dict(tx)
            matched_tx.update({
                "match_actual_usdt": str(match["actual"]),
                "match_expected_usdt": str(match["expected"]),
                "match_difference_usdt": str(match["difference"]),
                "match_type": match["type"],
            })
            return matched_tx

    return None


def _to_decimal(value: float | str | Decimal) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _expected_decimal(expected: Decimal) -> Decimal:
    """Use 3 decimals for new payments, but preserve old 6-decimal pending amounts."""
    if expected == expected.quantize(USDT_QUANT):
        return expected.quantize(USDT_QUANT)
    return expected.quantize(USDT_LEGACY_QUANT)


def _raw_token_value_to_decimal(raw_value: str) -> Decimal | None:
    try:
        return Decimal(int(raw_value, 16)) / USDT_DECIMALS
    except Exception:
        return None


def _parse_int(value) -> int | None:
    try:
        if isinstance(value, str) and value.startswith("0x"):
            return int(value, 16)
        return int(value)
    except Exception:
        return None


def _api_tx_has_required_confirmations(tx: dict) -> bool:
    confirmations = _parse_int(tx.get("confirmations"))
    if confirmations is None:
        # Most explorer token-transfer APIs include confirmations. If missing, do
        # not block a valid result because the RPC fallback also enforces the rule.
        return True
    return confirmations >= REQUIRED_CONFIRMATIONS


def _amount_match_details(actual: Decimal, expected: Decimal) -> dict | None:
    """Return match metadata only when the actual USDT amount exactly matches."""
    try:
        expected_q = _expected_decimal(expected)
        # Do not quantize the actual transaction amount before comparing.
        # This keeps auto-verification exact while payment pages show 3 decimals.
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


def _amount_matches(actual: Decimal, expected: Decimal) -> bool:
    return _amount_match_details(actual, expected) is not None


def _address_to_topic(address: str) -> str | None:
    addr = (address or "").lower().replace("0x", "")
    if len(addr) != 40 or any(c not in "0123456789abcdef" for c in addr):
        return None
    return "0x" + ("0" * 24) + addr


def _safe_rpc_name(rpc_url: str) -> str:
    return rpc_url.replace("https://", "").replace("http://", "").split("/")[0]
