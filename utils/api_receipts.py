"""Plain-text API purchase receipts for the customer's shared USDT wallet."""
from decimal import Decimal
from utils.product_emojis import label as product_emoji_label


def money(value):
    return format(Decimal(str(value or 0)).quantize(Decimal("0.000001")), "f").rstrip("0").rstrip(".") or "0"


def purchase_receipt(order, language="en", product=None):
    response = order.get("api_purchase_response") or {}
    labels = (
        ("Compra API completada", "Producto", "Cantidad", "Pedido", "Descontado", "Saldo USDT restante")
        if language == "es" else
        ("API purchase completed", "Product", "Quantity", "Order ID", "Deducted", "Remaining USDT balance")
    )
    return "\n".join([
        "✅ " + labels[0], "",
        f"{labels[1]}: {product_emoji_label(product, order.get('product_name', ''))}",
        f"{labels[2]}: {order.get('quantity', 1)}",
        f"{labels[3]}: {order.get('order_id', '')}",
        f"{labels[4]}: {money(order.get('amount_usdt'))} USDT",
        f"{labels[5]}: {money(response.get('balance', order.get('api_balance_after', 0)))} USDT",
    ])
