"""Payment error text helpers; importing these never loads bot credentials."""
import re

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

