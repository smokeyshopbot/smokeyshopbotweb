"""Search, API instructions and remaining customer-facing translations."""
import re

EXTRA_TEXT = {
    "en": {
        "btn_search": "🔎 Search products", "search_prompt": "🔎 Send a product name or a few words from its description. You can also use /search keyword.",
        "search_invalid": "Enter between 1 and 100 characters to search.", "search_results": "🔎 Results for ‘{keyword}’\n\n{count} products · Page {page}/{pages}",
        "search_empty": "No products match ‘{keyword}’. Try a different name or keyword.", "search_again": "🔎 New search",
        "search_help": "\n\n🔎 *Product search*\nUse /search keyword or the Search products button to find products by name or description.",
        "btn_api": "🔗 API access", "api_disabled": "*API access disabled*\n\nContact support to enable API access.",
        "api_title": "*Reseller API*", "api_intro": "Use this key from your bot or website to list products, check your USDT wallet and buy stock automatically.",
        "api_your_key": "*Your API key:*", "api_key_again": "Use /api to view this key again or regenerate it below.",
        "api_key_active": "*Your API key is active.*", "api_key_prefix": "Key prefix: {prefix}",
        "api_legacy_key": "Regenerate your older key below to get a visible key.", "api_no_key": "Tap Generate API key below to create your key.",
        "api_endpoints": "*Endpoints:*", "api_instructions": "Authenticate with the header shown above. Every purchase requires client_order_id: use a unique value for a new purchase and the same value when retrying it. Purchases use your main USDT wallet. Regenerating a key disables the old one.",
        "api_endpoint_url": "*Endpoint URL*", "api_endpoint_not_configured": "Not configured yet. Contact support for the public API URL.",
        "api_generate": "Generate API key", "api_regenerate": "Regenerate API key", "api_private": "Open a private chat with this bot to manage your API key.",
        "delivery_revoked_message": "🚫 This delivery was revoked by admin and can no longer be fetched from the bot.",
        "delivery_disabled_by_admin": "Disabled by admin", "status_revoked": "Revoked",
        "admin_cancelled_paid": "❌ Your paid order was cancelled by admin.\n\n🧾 Order ID: {order_id}\n📦 Product: {product} x{quantity}\n💰 Amount: {amount}",
        "admin_cancelled_pending": "❌ Your pending-stock order was cancelled by admin.\n\n🧾 Order ID: {order_id}\n📦 Product: {product} x{quantity}",
        "admin_note_notice": "📝 Admin note: {note}", "choose_refund_option": "Choose one of the available options below:",
        "contact_support_notice": "Contact support if you need help.",
        "refund_paid_notice": "✅ Your refund was marked as paid by admin.\n\n🧾 Order ID: {order_id}\n💰 Amount: {amount}",
    },
    "es": {
        "btn_search": "🔎 Buscar productos", "search_prompt": "🔎 Envía el nombre de un producto o palabras de su descripción. También puedes usar /search palabra.",
        "search_invalid": "Escribe entre 1 y 100 caracteres para buscar.", "search_results": "🔎 Resultados para «{keyword}»\n\n{count} productos · Página {page}/{pages}",
        "search_empty": "No hay productos que coincidan con «{keyword}». Prueba otro nombre o palabra.", "search_again": "🔎 Nueva búsqueda",
        "search_help": "\n\n🔎 *Búsqueda de productos*\nUsa /search palabra o el botón Buscar productos para buscar por nombre o descripción.",
        "btn_api": "🔗 Acceso API", "api_disabled": "*Acceso API desactivado*\n\nContacta con soporte para activarlo.",
        "api_title": "*API para revendedores*", "api_intro": "Usa esta clave desde tu bot o sitio web para listar productos, consultar tu billetera USDT y comprar stock automáticamente.",
        "api_your_key": "*Tu clave API:*", "api_key_again": "Usa /api para volver a consultar esta clave o regenerarla abajo.",
        "api_key_active": "*Tu clave API está activa.*", "api_key_prefix": "Prefijo de la clave: {prefix}",
        "api_legacy_key": "Regenera tu clave anterior abajo para obtener una clave visible.", "api_no_key": "Pulsa Generar clave API abajo para crear tu clave.",
        "api_endpoints": "*Rutas de la API:*", "api_instructions": "Autentícate con el encabezado mostrado arriba. Cada compra requiere client_order_id: usa un valor único para una compra nueva y el mismo valor al reintentarla. Las compras usan tu billetera USDT principal. Regenerar la clave desactiva la anterior.",
        "api_endpoint_url": "*URL del endpoint*", "api_endpoint_not_configured": "Todavía no está configurada. Contacta con soporte para obtener la URL pública de la API.",
        "api_generate": "Generar clave API", "api_regenerate": "Regenerar clave API", "api_private": "Abre un chat privado con este bot para gestionar tu clave API.",
        "delivery_revoked_message": "🚫 El admin revocó esta entrega y ya no puedes recuperarla desde el bot.",
        "delivery_disabled_by_admin": "Desactivado por el admin", "status_revoked": "Revocado",
        "admin_cancelled_paid": "❌ El admin canceló tu pedido pagado.\n\n🧾 ID del pedido: {order_id}\n📦 Producto: {product} x{quantity}\n💰 Importe: {amount}",
        "admin_cancelled_pending": "❌ El admin canceló tu pedido pendiente de stock.\n\n🧾 ID del pedido: {order_id}\n📦 Producto: {product} x{quantity}",
        "admin_note_notice": "📝 Nota del admin: {note}", "choose_refund_option": "Elige una de las opciones disponibles abajo:",
        "contact_support_notice": "Contacta con soporte si necesitas ayuda.",
        "refund_paid_notice": "✅ El admin marcó tu reembolso como pagado.\n\n🧾 ID del pedido: {order_id}\n💰 Importe: {amount}",
    },
}

API_ERRORS_ES = {
    "Invalid API key": "Clave API no válida", "API access is disabled": "El acceso API está desactivado",
    "API access disabled": "Acceso API desactivado", "User is blocked": "El usuario está bloqueado",
    "Too many requests. Try again later.": "Demasiadas solicitudes. Inténtalo de nuevo más tarde.",
    "Send a JSON object": "Envía un objeto JSON", "Product not found": "Producto no encontrado", "Order not found": "Pedido no encontrado",
    "product_id is required": "Se requiere product_id", "quantity must be a positive whole number": "quantity debe ser un número entero positivo",
    "client_order_id must be a string of at most 120 characters": "client_order_id debe ser un texto de hasta 120 caracteres",
    "client_order_id is required for every purchase": "Se requiere client_order_id para cada compra",
    "client_order_id already belongs to a different purchase": "client_order_id ya corresponde a otra compra",
    "Shop is under maintenance. Try again later.": "La tienda está en mantenimiento. Inténtalo de nuevo más tarde.",
    "Product USDT price is not configured": "El precio USDT del producto no está configurado", "Inventory not enough": "No hay suficiente stock",
    "Wallet or API access changed during purchase": "La billetera o el acceso API cambió durante la compra",
    "Insufficient wallet balance": "Saldo insuficiente en la billetera",
    "API access is disabled": "El acceso API está desactivado",
    "Wallet balance is not enough or API access is disabled": "El saldo de la billetera es insuficiente o el acceso API está desactivado",
    "Another purchase is processing. Retry using the same client_order_id.": "Hay otra compra en proceso. Reintenta con el mismo client_order_id.",
    "Order conflict. Please retry.": "Conflicto de pedido. Inténtalo de nuevo.",
    "Purchase failed; wallet deduction was reversed.": "La compra falló; se devolvió el importe descontado de la billetera.",
    "Purchase failed. Retry using the same client_order_id.": "La compra falló. Reintenta con el mismo client_order_id.",
    "Service temporarily unavailable. Retry using the same client_order_id.": "Servicio temporalmente no disponible. Reintenta con el mismo client_order_id.",
    "Order is processing or requires review. Do not submit it with a new client_order_id.": "El pedido está en proceso o requiere revisión. No lo envíes con un client_order_id nuevo.",
    "Supplier response is uncertain; wallet deduction was reversed. Contact support with this order ID.": "La respuesta del proveedor no está confirmada; se devolvió el importe descontado. Contacta con soporte e indica este ID de pedido.",
}


def api_message(message, lang):
    if lang != "es": return message
    match = re.fullmatch(r"Quantity must be between (\d+) and (\d+)", str(message))
    if match: return f"La cantidad debe estar entre {match[1]} y {match[2]}"
    return API_ERRORS_ES.get(message, message)


CATALOG_TEXT = {'en': {'api_catalog_button': '📋 View API products',
        'api_title_plain': 'Reseller API',
        'api_catalog_generate': 'Open /api and generate an API key before browsing API products.',
        'api_catalog_title': '📋 API products · Page {page}/{pages}',
        'api_catalog_hint': 'Tap or hold an ID to copy it. Search with /apiproducts keyword. '
                            'Prices are in USDT and use your pricing group.',
        'api_catalog_id': 'Product ID:',
        'api_catalog_price': 'Price: {price} USDT',
        'api_catalog_stock': 'Available: {stock}',
        'api_catalog_empty': 'No API products match this search.',
        'api_catalog_refresh': '↻ Refresh',
        'channel_buy_product': '🛒 Buy this product'},
 'es': {'api_catalog_button': '📋 Ver productos API',
        'api_title_plain': 'API para revendedores',
        'api_catalog_generate': 'Abre /api y genera una clave API antes de consultar los '
                                'productos.',
        'api_catalog_title': '📋 Productos API · Página {page}/{pages}',
        'api_catalog_hint': 'Toca o mantén pulsado un ID para copiarlo. Busca con /apiproducts '
                            'palabra. Los precios son en USDT y corresponden a tu grupo.',
        'api_catalog_id': 'ID del producto:',
        'api_catalog_price': 'Precio: {price} USDT',
        'api_catalog_stock': 'Disponibles: {stock}',
        'api_catalog_empty': 'No hay productos API que coincidan con esta búsqueda.',
        'api_catalog_refresh': '↻ Actualizar',
        'channel_buy_product': '🛒 Comprar este producto'}}
for _lang, _entries in CATALOG_TEXT.items():
    EXTRA_TEXT[_lang].update(_entries)

for _lang, _entries in {
    'en': {'api_balance_alerts_on': 'Low balance alerts: ON', 'api_balance_alerts_off': 'Low balance alerts: OFF',
           'api_failed_balance': 'Failure reason: Insufficient main wallet balance. No balance was deducted. Top up and submit a new API purchase with a new client_order_id.'},
    'es': {'api_balance_alerts_on': 'Avisos de saldo bajo: ACTIVADOS', 'api_balance_alerts_off': 'Avisos de saldo bajo: DESACTIVADOS',
           'api_failed_balance': 'Motivo del fallo: saldo insuficiente en la billetera principal. No se descontó saldo. Recarga y envía una nueva compra API con un client_order_id nuevo.'}
}.items():
    EXTRA_TEXT[_lang].update(_entries)

EXTRA_TEXT['en']['stock_checkout_active'] = 'You already have an unpaid checkout for this product. Complete it or wait for its payment window to expire before starting another.'
EXTRA_TEXT['es']['stock_checkout_active'] = 'Ya tienes un pago pendiente para este producto. Complétalo o espera a que venza su plazo antes de iniciar otro.'

EXTRA_TEXT['en']['api_failed_attempt_hint'] = 'An insufficient_balance response is a final failed attempt. It includes orderCode for later lookup. After topping up, use a NEW client_order_id. Repeating the old ID returns its saved failure; it never places the order automatically.'
EXTRA_TEXT['es']['api_failed_attempt_hint'] = 'Una respuesta insufficient_balance es un intento fallido definitivo e incluye orderCode para consultarlo después. Tras recargar, usa un client_order_id NUEVO. Repetir el ID anterior devuelve el fallo guardado y nunca realiza el pedido automáticamente.'
