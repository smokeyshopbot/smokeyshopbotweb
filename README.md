# 🛍 Telegram Shop Bot

A fully-featured Telegram shop bot with USDT BEP20 auto-verification, manual UPI/Binance Pay verification, stock counts on product buttons, a dual-currency wallet system, support contact button, and complete admin management.

---

## 📁 Project Structure

```
telegram-shop-bot/
├── admin_panel.py          # WebAdmin entry point — run this
├── bot.py                  # Safety stub; do not run WebAdmin as polling bot
├── config.py               # Loads MongoDB bootstrap + WebAdmin Secret Settings
├── database.py             # MongoDB helpers (users, products, orders, payments)
├── handlers/
│   ├── admin.py            # All admin commands & UPI approval flow
│   ├── user.py             # Shopping flow (browse → quantity → pay)
│   ├── payment.py          # USDT & UPI payment logic + delivery
│   └── wallet.py           # Wallet top-up & balance
├── utils/
│   ├── bscscan.py          # USDT BEP20 verification via explorer APIs + RPC fallbacks
│   ├── crypto.py           # Unique decimal USDT amount generator
│   ├── qr.py               # Dynamic UPI QR code generator
│   └── crypto.py           # Unique decimal USDT amount generator
├── .env.example            # Copy this to .env and fill in values
└── requirements.txt
```

---

## ⚙️ Setup

### 1. Clone / Download the project

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file
```bash
cp .env.example .env
```
Fill in only the MongoDB bootstrap values in `.env`:

| Variable | Description |
|---|---|
| `MONGO_URI` | MongoDB Atlas connection string. Bot and WebAdmin must use the exact same database. |
| `DB_NAME` | MongoDB database name. Bot and WebAdmin must match exactly. |

Do **not** put the Telegram bot token in the bot service `.env`. The bot token, admin/tester IDs, support usernames, explorer API keys, timing settings, and other secrets are managed from **WebAdmin → Secret Settings** and stored in MongoDB. Restart the bot after changing the bot token or timing settings.

### 4. Create MongoDB indexes once

This makes dashboard/orders/products much faster on MongoDB Atlas Free:

```bash
python init_indexes.py
```

### 5. Run WebAdmin
```bash
python admin_panel.py
```

For Render/Railway, use this start command:

```bash
gunicorn admin_panel:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120
```

The WebAdmin UI is tuned for free hosts by default: sidebar/live checks are cached for a few seconds and full-page auto refresh is disabled unless you set `ADMIN_LIVE_FULL_REFRESH=1`.

Run the Telegram polling bot only from `Bot/sellingbot-main` with `python bot.py`. Do not run `WebAdmin/sellingbot-main/bot.py`.

---

## 👤 User Commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/commands` | Show user commands |
| `/shop` | Browse products |
| `/wallet` | Check INR & USDT balance |
| `/loadwallet` | Top up wallet via UPI, USDT BEP20, or Binance Pay |
| `/wallethistory` | View wallet top-up logs with Payment Method and status, 10 per page |
| `/orders` | View your previous orders, 10 per page, and get tappable `/getorderORDERID` shortcuts |
| `/support` | Contact support using the configured Telegram support username |

---

## 🛠 Admin Commands

| Command | Description |
|---|---|
| WebAdmin only | Telegram admin commands removed. Use the website admin panel. |
| `/addproduct <name> <price_inr> <price_usdt>` | Add a new product |
| `/removeproduct <name>` | Remove a product |
| `/setprice <name> <price_inr> <price_usdt>` | Update product prices |
| `/addstock <name>` | Start bulk stock addition |
| `/removestock <name>` | Remove specific stock items |
| `/cancel` | Abort active stock add/remove session |
| `/clearstock <name>` | Wipe all stock for a product |
| `/disableproduct <name>` | Hide a product from `/shop` without deleting it |
| `/enableproduct <name>` | Show a disabled product in `/shop` again |
| `/listproducts` | List products with stock counts, 10 per page |
| `/listusers` | List all users with wallet balances, 10 per page |
| `/pendingorders` | View paid orders waiting for stock |
| `/findorder <order_id>` | Search one order and show full details |
| `/stats` | Show overall bot users/orders/revenue/stock dashboard |
| `/ranking` | Show top buyers, 10 per page with Next/Previous buttons |
| `/userstats <user_id>` | Show wallet and order stats for one user |
| `/userorders <user_id>` | Show a specific user’s orders, 10 per page, with `/getorderORDERID` shortcuts |
| `/userwallethistory <user_id>` | Show a specific user’s wallet top-up logs with Payment Method and status, 10 per page |
| `/maintenance <on\|off\|status>` | Turn maintenance mode on/off or check status |
| `/addbalance <user_id> <inr\|usdt> <amount> [note]` | Add wallet balance for a user manually |
| `/removebalance <user_id> <inr\|usdt> <amount> [note]` | Remove wallet balance from a user manually |
| `/blockuser <user_id>` | Block a user |
| `/unblockuser <user_id>` | Unblock a user |
| `/broadcast <message>` | Send a message to all users |
| `/recentorders` | View recent orders, 10 per page with Previous/Next buttons |

---

## 🏆 Buyer Ranking

Admins can use `/ranking` to see top buyers, 10 users per page with Previous/Next buttons.

Each ranking entry shows:

- User ID
- Username
- Total orders
- Total order value in INR and USDT

Only paid-value orders are counted: delivered orders and paid orders waiting for stock. Pending/unpaid, failed, and expired orders are not counted.

## 📦 User Order History

Users can send `/orders` anytime to see their own recent orders, 10 per page with Previous/Next buttons. Each order entry shows:

- Order ID
- Order date and time
- Product and quantity
- Current status
- A tappable `/getorderORDERID` shortcut

Example:

```
Order ID: AB12CD34
Date/Time: 2026-05-16 09:30 UTC
Product: Netflix x1
Status: ✅ Delivered
Fetch again: /getorderAB12CD34
```

The `/getorderORDERID` shortcut is handled by the bot but is not shown as a public command in BotFather. Users can tap it from the `/orders` message to receive their delivered items again.

For admins, `/recentorders` shows all recent bot orders in a clean multi-line layout, 10 per page, with Previous/Next buttons. Each order includes a `/getorderORDERID` shortcut so admin can fetch delivered stock/items when needed. `/orders` and the My Orders button always show the admin's own personal order history.

---

## ⏳ Paid Orders Waiting for Stock

If payment is confirmed but the product has no stock left, the order is not failed or refunded automatically. It is saved as:

```
Paid — Waiting for Stock
```

The user receives a message with their Order ID and a Contact Support button. When admin later adds stock with `/addstock ProductName`, the bot automatically delivers waiting paid orders first, oldest first, before the stock is available for new buyers.

Admin's `/addstock` reply shows how many pending orders were auto-delivered and how much stock remains available for new buyers.

---

## 🛍 Product Buttons

Product buttons in `/shop` now show live stock count directly, for example:

```
✅ Netflix | Stock: 12 | ₹100 / $1.2000 USDT
❌ Spotify | Sold Out | ₹80 / $1.0000 USDT
```

---

## 📦 Adding Bulk Stock

After `/addstock ProductName`, send your stock separated by `---`. Send `/cancel` to abort before uploading.
Each block between `---` = **one unit of stock**.

**Single-line items:**
```
key1abc123
---
key2def456
---
key3ghi789
```

**Multi-line items (e.g. ID + Password):**
```
username1
password1
---
username2
password2
---
username3
password3
```

**Mixed:**
```
singlelinekey
---
user4
pass4
---
https://somelink.com/abc
```

---


## 🧰 Admin Tools Added

### Pending paid orders

Use `/pendingorders` to see orders that are already paid but waiting for stock. These orders are delivered oldest-first when `/addstock` is used.

### Product visibility

Use `/disableproduct ProductName` to hide a product from the shop without deleting stock or order history. Use `/enableproduct ProductName` to show it again.

### Order search and stats

- `/findorder ORDERID` shows one order with user, product, amount, status, and delivered items.
- `/stats` shows total users, orders, revenue value, and stock. Revenue counts only delivered orders and paid orders waiting for stock, using the actual payment currency only.
- `/userstats USER_ID` shows one user's wallet balances and order totals. Paid value uses only the actual payment currency for each order.
- `/userorders USER_ID` shows that user's orders cleanly, 10 per page, with `/getorderORDERID` shortcuts. Admins can use `/getorderORDERID` to fetch delivered stock/items for any order.
- `/userwallethistory USER_ID` shows that user's wallet top-up logs cleanly with Payment Method and status, 10 per page.

### Maintenance mode

Use `/maintenance on` to temporarily stop non-admin users from using the bot. Admin commands still work. Turn it off with `/maintenance off`.

### Low stock alerts

The bot alerts admin when a product drops below `LOW_STOCK_ALERT_THRESHOLD`. Default: 10 units.

## 💳 Payment Flows



### Payment reminder and expiry

Unpaid payment sessions now send a reminder after `PAYMENT_REMINDER_MINUTES` minutes. With the default values, the user gets a reminder after 20 minutes and the payment expires 10 minutes later.

If the payment is still not completed at `PAYMENT_TIMEOUT_MINUTES`, the bot deletes the payment-details message and sends the user an expiry confirmation:

```text
⏰ Order Expired
Order ID: XXXXXXXX
The payment was not completed within 30 minutes.
Please create a new order.
```

For wallet top-ups, the same flow is used with a Wallet Top-up ID instead of Order ID.

### BEP20 check behavior

USDT BEP20 auto-verification waits for `BEP20_REQUIRED_CONFIRMATIONS` BSC block confirmations before confirming a payment. The default is `3`, which is usually fast on BNB Smart Chain while avoiding instant/unstable detection. The verifier requires the exact unique amount shown to the user.

If a USDT payment row is no longer waiting, the Check Payment button now shows the actual state instead of only saying “already processed or expired.” If a wallet top-up is confirmed, it credits the wallet, deletes the payment message, and sends the wallet confirmation. If an order is paid but not delivered, it retries delivery; if the product is out of stock, it tells the user the order is waiting for stock. Expired USDT sessions are checked one more time when the user presses Check Payment.

On bot restart, waiting USDT BEP20 payments are resumed automatically so auto-check continues without requiring the user to press Check Payment.

### USDT BEP20

Recommended BEP20 settings:

```env
USDT_MANUAL_VERIFY_DELAY_MINUTES=5
BEP20_REQUIRED_CONFIRMATIONS=3
```

1. Bot generates a 3-decimal unique amount (e.g. `15.253 USDT`) specific to this order
2. User sends the exact amount to your BEP20 wallet
3. Bot polls automatically and the user can press **🔄 Check Payment**
4. Auto-check uses explorer APIs first, then BSC RPC `eth_getLogs` fallbacks in small chunks
5. **Manual Verify** unlocks after 5 minutes if auto-check does not complete; user submits **TxHash + screenshot**
6. Admin receives manual USDT proof with **Approve / Reject** buttons

6. On detection or approval: items delivered → order complete ✅

### UPI (Manual Verification)
1. Bot sends dynamic QR + UPI ID for exact amount
2. User pays and presses **✅ I've Paid**
3. Bot asks for **payer/account holder name**, **UTR/transaction ID**, then **payment screenshot**
4. Admin receives payment details + screenshot + **Approve / Reject** buttons
5. On approval: items delivered + user notified ✅
6. On rejection: user is notified ❌

### Binance Pay (Auto + Manual Fallback)
1. Bot generates a unique Binance Pay USDT amount, e.g. `15.253 USDT`
2. User sends the exact amount to your Binance Pay ID/name
3. Bot polls Binance Pay trade history automatically and the user can press **🔄 Check Payment**
4. If detected, the order is delivered or the wallet is credited automatically
5. **Manual Verify** unlocks after `USDT_MANUAL_VERIFY_DELAY_MINUTES` minutes; user submits **Binance account name + screenshot**
6. Admin receives manual Binance proof with **Approve / Reject** buttons
7. On rejection: user is notified ❌

Binance Pay auto-verification credentials are stored in WebAdmin → Secret Settings:

- `binance_api_key`
- `binance_api_secret`
- `binance_api_base_url` default `https://api.binance.com`
- `binance_recv_window_ms` default `5000`
- `binance_pay_history_lookback_seconds` default `3600`

Use a read-only Binance API key and do not enable withdrawals.

### Wallet (Instant)
1. User tops up wallet via UPI, USDT BEP20, or Binance Pay
2. Wallet credited after payment verification. Binance Pay top-ups are added to the USDT wallet balance
3. At checkout, wallet balance shown as payment option
4. Wallet payment is instant — no verification needed ⚡

---

## 🗄 Database Collections (MongoDB)

- **users** — User profiles, blocked status, INR & USDT wallet balances
- **products** — Product name, prices, stock array
- **orders** — Full order history with delivery status
- **pending_payments** — Active payment sessions being verified

---

## 🔐 Security Notes

- Bot token/admin/database values are in `.env`; payment method details are managed in WebAdmin → Payment Settings and stored in MongoDB. Never commit `.env`.
- Admin commands are restricted to the comma-separated IDs in `ADMIN_IDS`
- USDT auto-verification requires the exact unique amount shown to the user
- Manual payment methods require screenshot proof before admin approval
- Blocked users cannot interact with the bot at all


### BEP20 automatic verification

The bot runs a global BEP20 auto-verification watcher in the background. This watcher scans all waiting USDT BEP20 payments every `USDT_VERIFY_INTERVAL_SECONDS` seconds and completes shop orders or wallet top-ups automatically after the required confirmations. The **Check Payment** button is only a manual fallback; users should not need to press it when the bot is running normally.


## List and stock order

- Order, wallet history, user, and product list pages show the newest records first and older records on later pages.
- Stock delivery uses FIFO: stock added first is delivered/sold first. New stock is appended to the back of the queue.
- Paid orders waiting for stock are delivered oldest-first when new stock is added.


### Wallet History Top-up Again

The `/wallethistory` page includes a **➕ Top-up Again** button under pagination and above Back.

---

## Bot token troubleshooting

The Telegram polling bot reads the token from MongoDB WebAdmin Secret Settings, not from `.env`. On startup it logs a masked token preview and fingerprint, for example:

```text
🔐 Bot token source: MongoDB WebAdmin Secret Settings
🔐 Bot token in use: bot_id 1234…90:…abcd / sha256:xxxxxxxxxxxx
🔐 MongoDB database in use: shopbot
```

In WebAdmin → System Health, the Bot token check shows the same masked preview/fingerprint. If the bot log and WebAdmin System Health show different fingerprints, your Bot and WebAdmin services are connected to different `MONGO_URI`/`DB_NAME` values or one service was not redeployed/restarted.
