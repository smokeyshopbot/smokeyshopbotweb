"""utils/qr.py — Dynamic UPI QR code generator.

UPI deep-link format: upi://pay?pa=<payee>&pn=<name>&am=<amount>&cu=INR

The UPI ID/name are passed in from MongoDB-backed payment settings. They are
not loaded from .env, so stale environment values cannot be shown to users.
"""

import io
from urllib.parse import quote

import qrcode


def build_upi_url(amount: float, upi_id: str, upi_name: str, note: str = "") -> str:
    """Build a UPI deep-link URL for the given amount and configured payee."""
    upi_id = (upi_id or "").strip()
    upi_name = (upi_name or "Merchant").strip() or "Merchant"
    url = (
        f"upi://pay?pa={quote(upi_id)}"
        f"&pn={quote(upi_name)}"
        f"&am={amount:.2f}"
        f"&cu=INR"
    )
    if note:
        url += f"&tn={quote(note)}"
    return url


async def generate_upi_qr(amount: float, upi_id: str, upi_name: str, note: str = "") -> bytes:
    """Generate a UPI QR code PNG locally and return the image bytes."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(build_upi_url(amount, upi_id, upi_name, note))
    qr.make(fit=True)

    image = qr.make_image(fill_color="black", back_color="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
