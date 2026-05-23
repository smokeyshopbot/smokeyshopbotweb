"""
utils/bscscan.py — USDT on-chain transaction verification helpers.

Auto verification checks incoming USDT transfers to the wallet address stored
in MongoDB payment settings and matches the unique decimal amount shown to the
buyer.

Supported networks:
- BNB Smart Chain / BEP20 (method: usdt)
- Polygon PoS (method: polygon)

Why multiple sources?
- Legacy explorer APIs can fail/deprecate/rate-limit.
- Etherscan API V2 is the newer multichain API, but can require the correct key/tier.
- RPC log scanning is the best no-explorer fallback, but many public RPCs disable
  eth_getLogs. This file supports multiple RPC URLs and tries them in chunks.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp

from config import (
    BSCSCAN_API_KEY, POLYGONSCAN_API_KEY, ETHERSCAN_API_KEY,
    BSC_RPC_URL, BSC_RPC_URLS, POLYGON_RPC_URL, POLYGON_RPC_URLS,
    USDT_LOOKBACK_SECONDS, BSC_RPC_BLOCK_CHUNK_SIZE, POLYGON_RPC_BLOCK_CHUNK_SIZE,
    BEP20_REQUIRED_CONFIRMATIONS, POLYGON_REQUIRED_CONFIRMATIONS,
)

logger = logging.getLogger(__name__)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Etherscan V2 multichain endpoint.
ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"

USDT_QUANT = Decimal("0.001")
USDT_LEGACY_QUANT = Decimal("0.000001")
LOOKBACK_SECONDS = int(USDT_LOOKBACK_SECONDS or 3600)


@dataclass(frozen=True)
class UsdtNetworkConfig:
    key: str
    display_name: str
    chainid: str
    contract: str
    decimals: int
    legacy_base_url: str
    legacy_api_key: str
    rpc_urls: list[str]
    rpc_block_chunk_size: int
    required_confirmations: int
    estimated_block_time_seconds: float


@dataclass
class UsdtCheckResult:
    found: bool = False
    source: str | None = None
    tx: dict | None = None
    errors: list[str] = field(default_factory=list)

    def short_error_text(self) -> str:
        if not self.errors:
            return "No matching USDT transfer found yet."
        # Keep logs useful, but avoid repeated provider errors becoming huge.
        cleaned: list[str] = []
        for err in self.errors:
            value = str(err or "").strip()
            if value and value not in cleaned:
                cleaned.append(value)
        return " | ".join(cleaned[-3:])[:700]

    def public_error_text(self) -> str:
        return public_usdt_error_text(self.errors)


def extract_usdt_received_amount_from_error(errors: list[str] | tuple[str, ...] | str | None) -> str:
    """Return the first received USDT amount mentioned in an amount-mismatch error."""
    if errors is None:
        return ""
    if isinstance(errors, str):
        combined = errors
    else:
        combined = " | ".join(str(item or "").strip() for item in errors if str(item or "").strip())
    for match in re.finditer(r"amount does not match\.?\s*Received:\s*([^|]+?)\s*USDT", combined, flags=re.I):
        values = [v.strip() for v in re.split(r",", match.group(1)) if v.strip()]
        for value in values:
            if re.fullmatch(r"\d+(?:\.\d+)?", value):
                return value
    return ""


def public_usdt_error_text(errors: list[str] | tuple[str, ...] | str | None) -> str:
    """Return a short admin-safe reason for a manual TxHash auto-check failure.

    RPC providers can return long/duplicated technical errors, and a submitted
    tx hash is checked across several RPC endpoints. This helper turns those
    raw errors into one compact reason for WebAdmin and payment reviews while
    preserving the detailed errors in logs.
    """
    if errors is None:
        return "Auto-check could not verify this transaction. Admin review required."
    if isinstance(errors, str):
        raw_items = [part.strip() for part in re.split(r"\s*\|\s*", errors) if part.strip()]
    else:
        raw_items = [str(item or "").strip() for item in errors if str(item or "").strip()]
    if not raw_items:
        return "No matching USDT transfer found yet."

    combined = " | ".join(raw_items)

    # Most useful case: the tx exists and pays the right wallet/token, but the
    # amount is outside the manual tolerance. De-duplicate repeated RPC results.
    received_values: list[str] = []
    for match in re.finditer(r"amount does not match\.?\s*Received:\s*([^|]+?)\s*USDT", combined, flags=re.I):
        values = [v.strip() for v in re.split(r",", match.group(1)) if v.strip()]
        for value in values:
            if value and value not in received_values:
                received_values.append(value)
    if received_values:
        shown = ", ".join(received_values[:3])
        more = "…" if len(received_values) > 3 else ""
        return f"Amount does not match. Received: {shown}{more} USDT."

    priority_checks = [
        (r"already linked to another payment", "This TxHash is already linked to another payment."),
        (r"invalid transaction hash", "Invalid transaction hash format."),
        (r"failed on-chain", "Transaction exists but failed on-chain."),
        (r"not mined", "Transaction exists but is not mined yet."),
        (r"older than this payment request", "Transaction is older than this payment request."),
        (r"could not verify transaction time", "Could not verify the transaction time."),
        (r"not a .*transfer to your payment wallet", "Transaction is not a USDT transfer to your payment wallet."),
        (r"invalid or missing .*wallet address", "Payment wallet is missing or invalid in Payment Settings."),
        (r"did not find this tx", "Transaction was not found on the selected network."),
    ]
    for pattern, message in priority_checks:
        if re.search(pattern, combined, flags=re.I):
            return message

    confirmation_match = re.search(
        r"has\s+(\d+)\s+confirmation\(s\);\s*requires\s+(\d+)",
        combined,
        flags=re.I,
    )
    if confirmation_match:
        return f"Transaction needs more confirmations ({confirmation_match.group(1)}/{confirmation_match.group(2)})."

    # Hide noisy provider/API errors from the audit table. They are useful in
    # server logs, but not helpful as a giant yellow block in the UI.
    if re.search(r"receipt lookup failed|unauthorized|api key|rpc|http \d+|blocknumber|getlogs|ankr|etherscan", combined, flags=re.I):
        return "Auto-check could not verify this transaction through the chain RPC. Admin review required."

    first = re.sub(r"\s+", " ", raw_items[-1]).strip()
    first = re.sub(r"\{.*?\}", "", first).strip(" |:;,.{}[]'")
    if not first:
        return "Auto-check could not verify this transaction. Admin review required."
    if len(first) > 180:
        first = first[:177].rstrip() + "…"
    return first


def _split_rpc_urls(value: str, defaults: list[str]) -> list[str]:
    urls = []
    for url in str(value or ",".join(defaults)).split(","):
        clean = url.strip().rstrip("/")
        if clean and clean not in urls:
            urls.append(clean)
    return urls or defaults


_DEFAULT_BSC_RPCS = [
    BSC_RPC_URL,
    "https://bsc-rpc.publicnode.com",
    "https://bsc.drpc.org",
    "https://rpc.ankr.com/bsc",
]
_DEFAULT_POLYGON_RPCS = [
    POLYGON_RPC_URL,
    "https://polygon-rpc.com",
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
]

NETWORKS: dict[str, UsdtNetworkConfig] = {
    "bep20": UsdtNetworkConfig(
        key="bep20",
        display_name="USDT (BEP20)",
        chainid="56",
        contract="0x55d398326f99059fF775485246999027B3197955",
        decimals=18,
        legacy_base_url="https://api.bscscan.com/api",
        legacy_api_key=BSCSCAN_API_KEY,
        rpc_urls=_split_rpc_urls(BSC_RPC_URLS, _DEFAULT_BSC_RPCS),
        rpc_block_chunk_size=max(10, int(BSC_RPC_BLOCK_CHUNK_SIZE or 450)),
        required_confirmations=max(1, int(BEP20_REQUIRED_CONFIRMATIONS or 3)),
        estimated_block_time_seconds=3.0,
    ),
    "polygon": UsdtNetworkConfig(
        key="polygon",
        display_name="USDT (POLYGON)",
        chainid="137",
        # Tether USD (USDT) on Polygon PoS.
        contract="0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        decimals=6,
        legacy_base_url="https://api.polygonscan.com/api",
        legacy_api_key=POLYGONSCAN_API_KEY,
        rpc_urls=_split_rpc_urls(POLYGON_RPC_URLS, _DEFAULT_POLYGON_RPCS),
        rpc_block_chunk_size=max(10, int(POLYGON_RPC_BLOCK_CHUNK_SIZE or 500)),
        required_confirmations=max(1, int(POLYGON_REQUIRED_CONFIRMATIONS or 20)),
        estimated_block_time_seconds=2.0,
    ),
}

NETWORK_ALIASES = {
    "": "bep20",
    "usdt": "bep20",
    "usdt_bep20": "bep20",
    "bep20": "bep20",
    "bsc": "bep20",
    "bnb": "bep20",
    "binance": "bep20",
    "polygon": "polygon",
    "matic": "polygon",
    "polygon_pos": "polygon",
    "usdt_polygon": "polygon",
    "polygon_usdt": "polygon",
}


def normalize_usdt_network(network: str | None = None) -> str:
    return NETWORK_ALIASES.get(str(network or "").strip().lower(), "bep20")


def get_usdt_network_label(network: str | None = None) -> str:
    return NETWORKS[normalize_usdt_network(network)].display_name


def get_usdt_required_confirmations(network: str | None = None) -> int:
    return NETWORKS[normalize_usdt_network(network)].required_confirmations


async def check_usdt_received(
    expected_amount: float | str | Decimal,
    lookback: int = LOOKBACK_SECONDS,
    *,
    wallet_address: str | None = None,
    network: str | None = None,
) -> bool:
    """Backward-compatible boolean check used by existing payment flow."""
    result = await check_usdt_received_detailed(expected_amount, lookback, wallet_address=wallet_address, network=network)
    return result.found


async def check_usdt_received_detailed(
    expected_amount: float | str | Decimal,
    lookback: int = LOOKBACK_SECONDS,
    *,
    wallet_address: str | None = None,
    network: str | None = None,
) -> UsdtCheckResult:
    """Return detailed verification status for an expected incoming USDT transfer."""
    cfg = NETWORKS[normalize_usdt_network(network)]
    result = UsdtCheckResult()
    expected = _to_decimal(expected_amount)
    if expected is None:
        result.errors.append(f"Invalid expected amount: {expected_amount!r}")
        logger.warning("USDT %s check skipped: invalid expected amount %r", cfg.key, expected_amount)
        return result

    wallet_address = (wallet_address or "").strip()
    if not _address_to_topic(wallet_address):
        result.errors.append(f"Invalid or missing {cfg.display_name} wallet address in Payment Settings")
        logger.warning("USDT %s check skipped: invalid payment settings wallet %r", cfg.key, wallet_address)
        return result

    # 1) Try the legacy chain-specific explorer endpoint.
    tx, error = await _details_via_legacy_explorer(cfg, expected, lookback, wallet_address)
    if tx:
        result.found = True
        result.source = tx.get("source", f"legacy_{cfg.key}_explorer")
        result.tx = tx
        return result
    if error:
        result.errors.append(error)

    # 2) Try Etherscan V2 if an API key exists.
    if ETHERSCAN_API_KEY:
        tx, error = await _details_via_etherscan_v2(cfg, expected, lookback, wallet_address)
        if tx:
            result.found = True
            result.source = tx.get("source", "etherscan_v2")
            result.tx = tx
            return result
        if error:
            result.errors.append(error)
    else:
        result.errors.append("ETHERSCAN_API_KEY missing; skipping Etherscan V2")

    # 3) Fallback: read Transfer logs directly from RPC providers.
    tx, rpc_errors = await _details_via_rpc_logs(cfg, expected, lookback, wallet_address)
    if tx:
        result.found = True
        result.source = tx.get("source", f"{cfg.key}_rpc_logs")
        result.tx = tx
        return result
    result.errors.extend(rpc_errors)

    logger.info(
        "USDT auto-check not found. network=%s expected=%s wallet=%s errors=%s",
        cfg.key, expected, wallet_address, result.short_error_text(),
    )
    return result


async def get_usdt_tx_details(
    expected_amount: float | str | Decimal,
    *,
    wallet_address: str | None = None,
    network: str | None = None,
) -> dict | None:
    """Return a matching tx/log dict if found, otherwise None."""
    result = await check_usdt_received_detailed(expected_amount, LOOKBACK_SECONDS, wallet_address=wallet_address, network=network)
    return result.tx if result.found else None


async def verify_usdt_tx_hash_detailed(
    txn_hash: str,
    expected_amount: float | str | Decimal,
    *,
    wallet_address: str | None = None,
    network: str | None = None,
    min_timestamp: float | int | None = None,
    amount_tolerance: float | str | Decimal = Decimal("0.01"),
) -> UsdtCheckResult:
    """Verify one submitted USDT tx hash against the expected payment.

    This helper is intentionally separate from auto-verification. Auto-checking
    still requires an exact unique amount. Manual-hash auto verification allows
    a small configured tolerance after validating the exact transaction hash,
    token contract, receiver wallet, confirmations, and payment time.
    """
    cfg = NETWORKS[normalize_usdt_network(network)]
    result = UsdtCheckResult()
    normalized_hash = _normalize_tx_hash(txn_hash)
    if not normalized_hash:
        result.errors.append("Invalid transaction hash format")
        return result

    expected = _to_decimal(expected_amount)
    if expected is None:
        result.errors.append(f"Invalid expected amount: {expected_amount!r}")
        return result

    tolerance = _to_decimal(amount_tolerance) or Decimal("0")
    if tolerance < 0:
        tolerance = Decimal("0")

    wallet_address = (wallet_address or "").strip()
    wallet_topic = _address_to_topic(wallet_address)
    if not wallet_topic:
        result.errors.append(f"Invalid or missing {cfg.display_name} wallet address in Payment Settings")
        return result

    for rpc_url in cfg.rpc_urls:
        try:
            latest_hex = await _rpc_call(rpc_url, "eth_blockNumber", [])
            latest_block = int(latest_hex, 16)
            receipt = await _rpc_call(rpc_url, "eth_getTransactionReceipt", [normalized_hash])
        except Exception as exc:
            result.errors.append(f"{_safe_rpc_name(rpc_url)} receipt lookup failed: {exc}")
            continue

        if not receipt:
            result.errors.append(f"{_safe_rpc_name(rpc_url)} did not find this tx on {cfg.display_name}")
            continue

        tx, error = await _match_receipt_transfer(
            cfg=cfg,
            receipt=receipt,
            expected=expected,
            tolerance=tolerance,
            wallet_address=wallet_address,
            wallet_topic=wallet_topic,
            latest_block=latest_block,
            rpc_url=rpc_url,
            min_timestamp=min_timestamp,
        )
        if tx:
            result.found = True
            result.source = tx.get("source", f"{cfg.key}_rpc_receipt")
            result.tx = tx
            return result
        if error:
            result.errors.append(error)

    logger.info(
        "USDT manual tx-hash auto-check not verified. network=%s hash=%s expected=%s wallet=%s errors=%s",
        cfg.key, normalized_hash, expected, wallet_address, result.short_error_text(),
    )
    return result


# ───────────────────────── API SOURCES ─────────────────────────

async def _details_via_legacy_explorer(
    cfg: UsdtNetworkConfig,
    expected: Decimal,
    lookback: int,
    wallet_address: str,
) -> tuple[dict | None, str | None]:
    params = {
        "module": "account",
        "action": "tokentx",
        "contractaddress": cfg.contract,
        "address": wallet_address,
        "sort": "desc",
        "offset": "100",
        "page": "1",
    }
    if cfg.legacy_api_key:
        params["apikey"] = cfg.legacy_api_key

    try:
        data = await _http_get_json(cfg.legacy_base_url, params=params)
    except Exception as exc:
        error = f"Legacy {cfg.display_name} explorer failed: {exc}"
        logger.warning(error)
        return None, error

    if data.get("status") != "1":
        msg = str(data.get("message") or "NOTOK")
        res = str(data.get("result") or "")[:160]
        error = f"Legacy {cfg.display_name} explorer returned {msg}: {res}"
        logger.warning(error)
        return None, error

    tx = _match_api_transfer(cfg, data.get("result", []), expected, lookback, wallet_address)
    if tx:
        tx["source"] = f"legacy_{cfg.key}_explorer"
        tx["network"] = cfg.key
    return tx, None


async def _details_via_etherscan_v2(
    cfg: UsdtNetworkConfig,
    expected: Decimal,
    lookback: int,
    wallet_address: str,
) -> tuple[dict | None, str | None]:
    params = {
        "chainid": cfg.chainid,
        "module": "account",
        "action": "tokentx",
        "contractaddress": cfg.contract,
        "address": wallet_address,
        "sort": "desc",
        "offset": "100",
        "page": "1",
        "apikey": ETHERSCAN_API_KEY,
    }

    try:
        data = await _http_get_json(ETHERSCAN_V2_BASE, params=params)
    except Exception as exc:
        error = f"Etherscan V2 {cfg.display_name} failed: {exc}"
        logger.warning(error)
        return None, error

    if data.get("status") != "1":
        msg = str(data.get("message") or "NOTOK")
        res = str(data.get("result") or "")[:160]
        error = f"Etherscan V2 {cfg.display_name} returned {msg}: {res}"
        logger.warning(error)
        return None, error

    tx = _match_api_transfer(cfg, data.get("result", []), expected, lookback, wallet_address)
    if tx:
        tx["source"] = f"etherscan_v2:{cfg.key}"
        tx["network"] = cfg.key
    return tx, None


async def _details_via_rpc_logs(
    cfg: UsdtNetworkConfig,
    expected: Decimal,
    lookback: int,
    wallet_address: str,
) -> tuple[dict | None, list[str]]:
    wallet_topic = _address_to_topic(wallet_address)
    if not wallet_topic:
        return None, [f"Invalid or missing {cfg.display_name} wallet address in Payment Settings"]

    errors: list[str] = []

    for rpc_url in cfg.rpc_urls:
        try:
            latest_hex = await _rpc_call(rpc_url, "eth_blockNumber", [])
            latest_block = int(latest_hex, 16)
        except Exception as exc:
            errors.append(f"{_safe_rpc_name(rpc_url)} blockNumber failed: {exc}")
            continue

        block_lookback = max(
            int(lookback / max(0.5, cfg.estimated_block_time_seconds)) + 300,
            cfg.rpc_block_chunk_size * 2,
        )
        from_block = max(0, latest_block - block_lookback)

        try:
            tx = await _scan_rpc_logs_in_chunks(
                cfg=cfg,
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
    cfg: UsdtNetworkConfig,
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
        start = max(from_block, end - cfg.rpc_block_chunk_size + 1)
        logs = await _rpc_call(rpc_url, "eth_getLogs", [{
            "address": cfg.contract,
            "fromBlock": hex(start),
            "toBlock": hex(end),
            "topics": [TRANSFER_TOPIC, None, wallet_topic],
        }])

        if not isinstance(logs, list):
            raise RuntimeError(f"unexpected log payload: {logs!r}")

        # Newest block chunk first; inside the chunk, reverse logs so newest first.
        for log in reversed(logs):
            raw_value = log.get("data", "0x0")
            value = _raw_token_value_to_decimal(raw_value, cfg.decimals)
            match = _amount_match_details(value, expected) if value is not None else None
            if match is not None:
                log_block = _parse_int(log.get("blockNumber"))
                confirmations = (latest_block - log_block + 1) if log_block is not None else 0
                if confirmations < cfg.required_confirmations:
                    logger.info(
                        "Matching %s transfer found but waiting for confirmations. tx=%s confirmations=%s required=%s",
                        cfg.display_name, log.get("transactionHash"), confirmations, cfg.required_confirmations,
                    )
                    continue
                token_decimals = Decimal(10) ** cfg.decimals
                return {
                    "hash": log.get("transactionHash"),
                    "txhash": log.get("transactionHash"),
                    "to": wallet_address,
                    "value": str(int(Decimal(value) * token_decimals)),
                    "value_usdt": str(value),
                    "tokenDecimal": str(cfg.decimals),
                    "contractAddress": cfg.contract,
                    "network": cfg.key,
                    "match_actual_usdt": str(match["actual"]),
                    "match_expected_usdt": str(match["expected"]),
                    "match_difference_usdt": str(match["difference"]),
                    "match_type": match["type"],
                    "source": f"{cfg.key}_rpc_logs:{_safe_rpc_name(rpc_url)}",
                    "blockNumber": log.get("blockNumber"),
                    "confirmations": confirmations,
                }
        end = start - 1

    return None


async def _match_receipt_transfer(
    cfg: UsdtNetworkConfig,
    receipt: dict,
    expected: Decimal,
    tolerance: Decimal,
    wallet_address: str,
    wallet_topic: str,
    latest_block: int,
    rpc_url: str,
    min_timestamp: float | int | None = None,
) -> tuple[dict | None, str | None]:
    status = str(receipt.get("status") or "").lower()
    if status and status not in {"0x1", "1"}:
        return None, "Transaction exists but failed on-chain"

    block_number = _parse_int(receipt.get("blockNumber"))
    if block_number is None:
        return None, "Transaction exists but is not mined yet"

    confirmations = latest_block - block_number + 1
    if confirmations < cfg.required_confirmations:
        return None, (
            f"Transaction found but has {confirmations} confirmation(s); "
            f"requires {cfg.required_confirmations}"
        )

    tx_timestamp = await _receipt_block_timestamp(rpc_url, receipt.get("blockNumber"))
    if min_timestamp is not None:
        try:
            min_ts = float(min_timestamp)
        except (TypeError, ValueError):
            min_ts = None
        if min_ts is not None:
            if tx_timestamp is None:
                return None, "Could not verify transaction time"
            if tx_timestamp < min_ts:
                return None, "Transaction is older than this payment request"

    contract = cfg.contract.lower()
    actual_values: list[str] = []
    for log in receipt.get("logs") or []:
        if bool(log.get("removed")):
            continue
        if str(log.get("address") or "").lower() != contract:
            continue
        topics = [str(topic or "").lower() for topic in (log.get("topics") or [])]
        if len(topics) < 3:
            continue
        if topics[0] != TRANSFER_TOPIC.lower():
            continue
        if topics[2] != wallet_topic.lower():
            continue

        value = _raw_token_value_to_decimal(str(log.get("data") or "0x0"), cfg.decimals)
        if value is None:
            continue
        actual_values.append(str(value))
        match = _amount_match_details_with_tolerance(value, expected, tolerance)
        if match is None:
            continue

        token_decimals = Decimal(10) ** cfg.decimals
        return {
            "hash": receipt.get("transactionHash"),
            "txhash": receipt.get("transactionHash"),
            "transactionHash": receipt.get("transactionHash"),
            "to": wallet_address,
            "value": str(int(Decimal(value) * token_decimals)),
            "value_usdt": str(value),
            "tokenDecimal": str(cfg.decimals),
            "contractAddress": cfg.contract,
            "network": cfg.key,
            "match_actual_usdt": str(match["actual"]),
            "match_expected_usdt": str(match["expected"]),
            "match_difference_usdt": str(match["difference"]),
            "match_type": match["type"],
            "source": f"{cfg.key}_rpc_receipt:{_safe_rpc_name(rpc_url)}",
            "blockNumber": receipt.get("blockNumber"),
            "confirmations": confirmations,
            "timeStamp": str(int(tx_timestamp)) if tx_timestamp is not None else "",
        }, None

    if actual_values:
        return None, f"Transaction found, but amount does not match. Received: {', '.join(actual_values[:3])} USDT"
    return None, f"Transaction found, but it is not a {cfg.display_name} transfer to your payment wallet"


async def _receipt_block_timestamp(rpc_url: str, block_number) -> int | None:
    if not block_number:
        return None
    try:
        block = await _rpc_call(rpc_url, "eth_getBlockByNumber", [block_number, False])
        if not isinstance(block, dict):
            return None
        return _parse_int(block.get("timestamp"))
    except Exception:
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


def _match_api_transfer(cfg: UsdtNetworkConfig, transfers: list[dict], expected: Decimal, lookback: int, wallet_address: str) -> dict | None:
    cutoff = int(time.time()) - lookback
    wallet = wallet_address.lower()
    contract = cfg.contract.lower()

    for tx in transfers:
        try:
            if int(tx.get("timeStamp", "0") or 0) < cutoff:
                continue
        except ValueError:
            continue

        if tx.get("to", "").lower() != wallet:
            continue

        if tx.get("contractAddress", cfg.contract).lower() != contract:
            continue

        try:
            decimals = int(tx.get("tokenDecimal", str(cfg.decimals)) or cfg.decimals)
            value = Decimal(tx.get("value", "0")) / (Decimal(10) ** decimals)
        except (InvalidOperation, ValueError):
            continue

        match = _amount_match_details(value, expected)
        if match is not None:
            if not _api_tx_has_required_confirmations(cfg, tx):
                logger.info(
                    "Matching %s transfer found but waiting for confirmations. hash=%s confirmations=%s required=%s",
                    cfg.display_name, tx.get("hash") or tx.get("txhash"), tx.get("confirmations"), cfg.required_confirmations,
                )
                continue
            matched_tx = dict(tx)
            matched_tx.update({
                "network": cfg.key,
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
    except (InvalidOperation, TypeError, ValueError):
        return None


def _normalize_tx_hash(txn_hash: str | None) -> str:
    raw = str(txn_hash or "").strip()
    match = re.search(r"0x[a-fA-F0-9]{64}", raw)
    return match.group(0).lower() if match else ""


def _expected_decimal(expected: Decimal) -> Decimal:
    """Use 3 decimals for new payments, but preserve old 6-decimal pending amounts."""
    if expected == expected.quantize(USDT_QUANT):
        return expected.quantize(USDT_QUANT)
    return expected.quantize(USDT_LEGACY_QUANT)


def _raw_token_value_to_decimal(raw_value: str, decimals: int) -> Decimal | None:
    try:
        return Decimal(int(raw_value, 16)) / (Decimal(10) ** int(decimals))
    except Exception:
        return None


def _parse_int(value) -> int | None:
    try:
        if isinstance(value, str) and value.startswith("0x"):
            return int(value, 16)
        return int(value)
    except Exception:
        return None


def _api_tx_has_required_confirmations(cfg: UsdtNetworkConfig, tx: dict) -> bool:
    confirmations = _parse_int(tx.get("confirmations"))
    if confirmations is None:
        # Most explorer token-transfer APIs include confirmations. If missing, do
        # not block a valid result because the RPC fallback also enforces the rule.
        return True
    return confirmations >= cfg.required_confirmations


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


def _amount_match_details_with_tolerance(actual: Decimal, expected: Decimal, tolerance: Decimal) -> dict | None:
    """Return match metadata when actual is within manual verification tolerance."""
    try:
        expected_q = _expected_decimal(expected)
        diff = abs(actual - expected_q).quantize(USDT_LEGACY_QUANT)
        if diff > tolerance:
            return None
    except Exception:
        return None

    return {
        "actual": actual,
        "expected": expected_q,
        "difference": diff,
        "type": "exact" if diff == 0 else "manual_tolerance",
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
