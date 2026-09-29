"""Belza Digital Telegram bot.

The bot sells OTP numbers through the NusaOTP v2 API and keeps customer
balances and deposit approvals in a local SQLite database.
"""

import html
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
import telebot
from flask import request
from telebot import types

from keep_alive import app, keep_alive


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_KEY_NUSA_OTP = os.environ.get("API_KEY_NUSA_OTP")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "1945888848"))
BASE_URL = "https://nusaotp.site/api"
MARGIN_KEUNTUNGAN = 1000
DATABASE_PATH = "belza_digital.db"
NUSAOTP_WEBHOOK_URL = os.environ.get("NUSAOTP_WEBHOOK_URL", "").strip()

# The attached QRIS image is the default deposit QR. QRIS_IMAGE_PATH can
# override it, and QRIS_IMAGE_URL remains supported for a hosted image.
DEFAULT_QRIS_IMAGE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "attached_assets",
    "IMG-20260928-WA0026_1790592570125.jpg",
)
QRIS_IMAGE_PATH = os.environ.get("QRIS_IMAGE_PATH", DEFAULT_QRIS_IMAGE_PATH)
QRIS_IMAGE_URL = os.environ.get("QRIS_IMAGE_URL", "").strip()

REQUEST_TIMEOUT = 20
MAX_BUTTONS_PER_PAGE = 24
DEPOSIT_AMOUNTS = (5000, 10000, 15000, 20000, 50000)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("belza-digital")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN belum diatur pada environment variables.")
if not API_KEY_NUSA_OTP:
    raise RuntimeError("API_KEY_NUSA_OTP belum diatur pada environment variables.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)
db_lock = threading.RLock()


# ---------------------------------------------------------------------------
# SQLite database
# ---------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def init_database() -> None:
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                external_order_id TEXT NOT NULL UNIQUE,
                country_id TEXT,
                country_name TEXT,
                service_id TEXT,
                service_name TEXT,
                number TEXT,
                price INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                refunded INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                proof_message_id INTEGER,
                amount INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                admin_message_id INTEGER,
                created_at TEXT NOT NULL,
                reviewed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_deposits_user_id ON deposits(user_id);
            """
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_user(user: Any) -> None:
    user_id = int(user.id)
    username = getattr(user, "username", None) or getattr(user, "first_name", "")
    timestamp = now_iso()
    with db_lock, get_connection() as connection:
        connection.execute(
            """
            INSERT INTO users (user_id, username, balance, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                updated_at = excluded.updated_at
            """,
            (user_id, username, timestamp, timestamp),
        )


def get_balance(user_id: int) -> int:
    with db_lock, get_connection() as connection:
        row = connection.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row["balance"]) if row else 0


def add_balance(user_id: int, amount: int) -> int:
    if amount <= 0:
        raise ValueError("Jumlah saldo harus lebih besar dari nol.")
    timestamp = now_iso()
    with db_lock, get_connection() as connection:
        connection.execute(
            """
            INSERT INTO users (user_id, username, balance, created_at, updated_at)
            VALUES (?, '', ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                balance = users.balance + excluded.balance,
                updated_at = excluded.updated_at
            """,
            (user_id, amount, timestamp, timestamp),
        )
        row = connection.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row["balance"])


def deduct_balance(user_id: int, amount: int) -> bool:
    """Atomically deduct amount, returning False when balance is insufficient."""
    if amount <= 0:
        return False
    with db_lock, get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE users
            SET balance = balance - ?, updated_at = ?
            WHERE user_id = ? AND balance >= ?
            """,
            (amount, now_iso(), user_id, amount),
        )
        return cursor.rowcount == 1


def save_order(
    user_id: int,
    external_order_id: str,
    country_id: str,
    country_name: str,
    service_id: str,
    service_name: str,
    number: str,
    price: int,
    status: str,
) -> None:
    timestamp = now_iso()
    with db_lock, get_connection() as connection:
        connection.execute(
            """
            INSERT INTO orders (
                user_id, external_order_id, country_id, country_name,
                service_id, service_name, number, price, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                external_order_id,
                country_id,
                country_name,
                service_id,
                service_name,
                number,
                price,
                status,
                timestamp,
                timestamp,
            ),
        )


def get_order_for_user(user_id: int, external_order_id: str) -> Optional[sqlite3.Row]:
    with db_lock, get_connection() as connection:
        return connection.execute(
            """
            SELECT * FROM orders
            WHERE user_id = ? AND external_order_id = ?
            """,
            (user_id, external_order_id),
        ).fetchone()


def get_order(external_order_id: str) -> Optional[sqlite3.Row]:
    with db_lock, get_connection() as connection:
        return connection.execute(
            "SELECT * FROM orders WHERE external_order_id = ?",
            (external_order_id,),
        ).fetchone()


def update_order(
    external_order_id: str,
    status: Optional[str] = None,
    number: Optional[str] = None,
    refunded: Optional[int] = None,
) -> None:
    changes = ["updated_at = ?"]
    values: List[Any] = [now_iso()]
    if status is not None:
        changes.append("status = ?")
        values.append(status)
    if number is not None:
        changes.append("number = ?")
        values.append(number)
    if refunded is not None:
        changes.append("refunded = ?")
        values.append(refunded)
    values.append(external_order_id)
    with db_lock, get_connection() as connection:
        connection.execute(
            f"UPDATE orders SET {', '.join(changes)} WHERE external_order_id = ?",
            values,
        )


def refund_order_once(external_order_id: str) -> Optional[int]:
    """Refund one canceled order only once; returns the refunded price."""
    with db_lock, get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT user_id, price, refunded FROM orders
            WHERE external_order_id = ?
            """,
            (external_order_id,),
        ).fetchone()
        if not row or row["refunded"]:
            connection.commit()
            return None
        timestamp = now_iso()
        connection.execute(
            "UPDATE users SET balance = balance + ?, updated_at = ? WHERE user_id = ?",
            (row["price"], timestamp, row["user_id"]),
        )
        connection.execute(
            """
            UPDATE orders SET refunded = 1, status = 'canceled',
                updated_at = ? WHERE external_order_id = ?
            """,
            (timestamp, external_order_id),
        )
        connection.commit()
        return int(row["price"])


def create_deposit(user_id: int, proof_message_id: int) -> int:
    with db_lock, get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO deposits (user_id, proof_message_id, created_at)
            VALUES (?, ?, ?)
            """,
            (user_id, proof_message_id, now_iso()),
        )
        return int(cursor.lastrowid)


def set_deposit_admin_message(deposit_id: int, message_id: int) -> None:
    with db_lock, get_connection() as connection:
        connection.execute(
            "UPDATE deposits SET admin_message_id = ? WHERE id = ?",
            (message_id, deposit_id),
        )


def approve_deposit(deposit_id: int, amount: int) -> Optional[int]:
    """Approve exactly once and credit the associated user."""
    with db_lock, get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT user_id, status FROM deposits WHERE id = ?",
            (deposit_id,),
        ).fetchone()
        if not row or row["status"] != "pending":
            connection.commit()
            return None
        timestamp = now_iso()
        connection.execute(
            "UPDATE users SET balance = balance + ?, updated_at = ? WHERE user_id = ?",
            (amount, timestamp, row["user_id"]),
        )
        connection.execute(
            """
            UPDATE deposits SET amount = ?, status = 'approved',
                reviewed_at = ? WHERE id = ?
            """,
            (amount, timestamp, deposit_id),
        )
        connection.commit()
        return int(row["user_id"])


def reject_deposit(deposit_id: int) -> Optional[int]:
    with db_lock, get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE deposits SET status = 'rejected', reviewed_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (now_iso(), deposit_id),
        )
        if cursor.rowcount != 1:
            return None
        row = connection.execute(
            "SELECT user_id FROM deposits WHERE id = ?", (deposit_id,)
        ).fetchone()
    return int(row["user_id"]) if row else None


# ---------------------------------------------------------------------------
# NusaOTP API helpers
# ---------------------------------------------------------------------------

def api_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {API_KEY_NUSA_OTP}",
    }


def api_request(
    endpoint: str,
    method: str = "GET",
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Any:
    try:
        response = requests.request(
            method,
            f"{BASE_URL}/{endpoint.lstrip('/')}",
            params=params or {},
            json=json_body,
            headers=api_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        try:
            body = response.json()
        except ValueError:
            body = response.text
        if not response.ok:
            detail = response_detail(body)
            raise RuntimeError(f"API HTTP {response.status_code}: {detail}")
        if isinstance(body, dict) and body.get("success") is False:
            raise RuntimeError(response_detail(body))
        return body
    except requests.RequestException as error:
        logger.warning("NusaOTP request failed: %s", error)
        raise RuntimeError("Server OTP sedang tidak dapat dihubungi.") from error


def response_detail(body: Any) -> str:
    if not isinstance(body, dict):
        return str(body)
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "Server OTP menolak permintaan.")
    return str(body.get("message") or body.get("detail") or "Server OTP menolak permintaan.")


def api_get(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
    return api_request(endpoint, "GET", params=params)


def api_post(endpoint: str, json_body: Dict[str, Any]) -> Any:
    return api_request(endpoint, "POST", json_body=json_body)


def unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def fetch_countries() -> List[Dict[str, str]]:
    payload = unwrap_data(api_get("v2/country"))
    if not isinstance(payload, list):
        return []
    result = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        country_id = item.get("id")
        name = item.get("name")
        if country_id is not None and name:
            result.append({"id": str(country_id), "name": str(name)})
    return result


def fetch_services(country_id: str) -> List[Dict[str, Any]]:
    payload = unwrap_data(api_get("v2/services", {"country_id": country_id}))
    result: List[Dict[str, Any]] = []
    if not isinstance(payload, list):
        return result
    for item in payload:
        if not isinstance(item, dict):
            continue
        service_id = item.get("id")
        name = item.get("name")
        if service_id is None or name is None:
            continue
        result.append(
            {
                "id": str(service_id),
                "name": str(name),
                "code": str(item.get("code", "")),
                "base_price": 0,
                "stock": 0,
            }
        )
    return result


def fetch_products(country_id: str, service_id: str) -> List[Dict[str, Any]]:
    payload = unwrap_data(
        api_get(
            "v2/products",
            {"country_id": country_id, "service_id": service_id},
        )
    )
    result: List[Dict[str, Any]] = []
    if not isinstance(payload, list):
        return result
    for item in payload:
        if not isinstance(item, dict):
            continue
        product_id = item.get("id")
        name = item.get("name", f"Server {product_id}")
        price = item.get("price")
        if product_id is None or price is None:
            continue
        try:
            base_price = int(float(price))
        except (TypeError, ValueError):
            continue
        result.append(
            {
                "id": str(product_id),
                "name": str(name),
                "base_price": base_price,
                "stock": 0,
            }
        )
    return result


def create_api_order(
    country_id: str,
    service_id: str,
    product_id: str,
) -> Dict[str, Any]:
    payload = api_post(
        "v2/order",
        {
            "country_id": int(country_id),
            "service_id": int(service_id),
            "product_id": int(product_id),
        },
    )
    data = unwrap_data(payload)
    if not isinstance(data, dict):
        raise RuntimeError("Respons order dari API tidak valid.")
    return data


def check_api_order(order_id: str) -> Dict[str, Any]:
    """Cek status order dan kode OTP secara manual ke server NusaOTP."""
    try:
        payload = api_get("v2/order", {"id": order_id})
    except Exception:
        payload = api_get("v2/order", {"order_id": order_id})
    data = unwrap_data(payload)
    if isinstance(data, list) and len(data) > 0:
        data = data[0]
    if not isinstance(data, dict):
        raise RuntimeError("Respons status order dari API tidak valid.")
    return data


def cancel_api_order(order_id: str) -> Dict[str, Any]:
    data = unwrap_data(api_post("v2/cancel", {"order_id": int(order_id)}))
    if not isinstance(data, dict):
        raise RuntimeError("Respons pembatalan dari API tidak valid.")
    return data


def pick_value(data: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return default


@app.post("/webhook/nusaotp")
@app.post("/webhook/belza")
def nusaotp_webhook():
    """Receive realtime order and OTP events from the upstream provider."""
    payload = request.get_json(silent=True)
    if not payload:
        payload = request.form.to_dict() or {}

    event = str(payload.get("event", ""))
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        data = payload

    order_id = str(pick_value(data, "order_id", "id", "id_order", "orderId", default=""))
    order = get_order(order_id) if order_id else None
    if not order:
        logger.warning("Webhook order tidak ditemukan: event=%s order_id=%s", event, order_id)
        return {"success": False, "message": "Order not found"}, 404

    otp_value = pick_value(data, "otp_code", "otp", "code", "sms", default=None)
    otp = str(otp_value or "").strip()
    status = str(pick_value(data, "status", default=order["status"]))
    number = str(pick_value(data, "number", "phone", default=order["number"]))
    update_order(order_id, status=status, number=number)

    normalized_status = status.lower()
    if normalized_status in {"failed", "canceled", "cancelled"}:
        refund_order_once(order_id)

    if otp and otp.lower() not in {"waiting", "pending", "null", "none"}:
        try:
            bot.send_message(
                order["user_id"],
                "<b>📩 Kode OTP Diterima</b>\n\n"
                f"Nomor: <code>{html.escape(number)}</code>\n"
                f"Kode: <code>{html.escape(otp)}</code>\n"
                f"Status: <b>{html.escape(status)}</b>",
                reply_markup=order_keyboard(order_id),
            )
        except Exception:
            logger.exception("Gagal mengirim OTP ke Telegram: order_id=%s", order_id)
    elif normalized_status in {"failed", "canceled", "cancelled"}:
        try:
            bot.send_message(
                order["user_id"],
                f"⚠️ Order <code>{html.escape(order_id)}</code> berstatus "
                f"<b>{html.escape(status)}</b>. Saldo internal diproses untuk refund.",
                reply_markup=back_to_menu(),
            )
        except Exception:
            logger.exception("Gagal mengirim status order ke Telegram: order_id=%s", order_id)

    logger.info("NusaOTP webhook received: event=%s order_id=%s", event, order_id)
    return {"success": True}


def configure_nusa_webhook() -> None:
    if not NUSAOTP_WEBHOOK_URL:
        logger.info("NUSAOTP_WEBHOOK_URL belum diatur; webhook belum didaftarkan.")
        return
    try:
        api_post("v2/set-webhook", {"webhook_url": NUSAOTP_WEBHOOK_URL})
        logger.info("NusaOTP webhook berhasil didaftarkan.")
    except RuntimeError as error:
        logger.warning("NusaOTP webhook gagal didaftarkan: %s", error)


# ---------------------------------------------------------------------------
# Telegram UI helpers
# ---------------------------------------------------------------------------

def rupiah(value: int) -> str:
    return f"Rp {int(value):,}".replace(",", ".")


def main_menu() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("📱 Beli Nomor Nokos", callback_data="menu:buy"),
        types.InlineKeyboardButton("💳 Isi Saldo (Deposit)", callback_data="menu:deposit"),
        types.InlineKeyboardButton("💰 Saldo Saya", callback_data="menu:balance"),
    )
    return keyboard


def back_to_menu() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("⬅️ Menu Utama", callback_data="menu:home"))
    return keyboard


def send_home(chat_id: int, edit_message: Optional[types.Message] = None) -> None:
    text = (
        "<b>Belza Digital</b>\n"
        "Layanan pembelian nomor virtual OTP cepat dan praktis.\n\n"
        "Silakan pilih menu:"
    )
    if edit_message:
        try:
            bot.edit_message_text(
                text,
                edit_message.chat.id,
                edit_message.message_id,
                reply_markup=main_menu(),
            )
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=main_menu())


def service_label(service: Dict[str, Any]) -> str:
    return f"{str(service['name'])[:35]} • pilih server"


def product_label(product: Dict[str, Any]) -> str:
    sell_price = int(product["base_price"]) + MARGIN_KEUNTUNGAN
    return f"{str(product['name'])[:24]} • {rupiah(sell_price)}"


def chunked(items: List[Any], size: int) -> Iterable[List[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def country_keyboard(countries: List[Dict[str, str]], page: int = 0) -> types.InlineKeyboardMarkup:
    pages = list(chunked(countries, MAX_BUTTONS_PER_PAGE))
    page = max(0, min(page, len(pages) - 1))
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    for country in pages[page]:
        keyboard.add(
            types.InlineKeyboardButton(
                country["name"].title()[:30],
                callback_data=f"country:{country['id']}",
            )
        )
    navigation = []
    if page > 0:
        navigation.append(types.InlineKeyboardButton("◀️ Sebelumnya", callback_data=f"countries:{page - 1}"))
    if page < len(pages) - 1:
        navigation.append(types.InlineKeyboardButton("Berikutnya ▶️", callback_data=f"countries:{page + 1}"))
    if navigation:
        keyboard.row(*navigation)
    keyboard.add(types.InlineKeyboardButton("⬅️ Menu Utama", callback_data="menu:home"))
    return keyboard


def services_keyboard(
    country_id: str,
    services: List[Dict[str, Any]],
    page: int = 0,
) -> types.InlineKeyboardMarkup:
    pages = list(chunked(services, MAX_BUTTONS_PER_PAGE))
    page = max(0, min(page, len(pages) - 1))
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for service in pages[page]:
        service_id = str(service["id"])[:20]
        keyboard.add(
            types.InlineKeyboardButton(
                service_label(service)[:64],
                callback_data=f"products:{country_id}:{service_id}",
            )
        )
    navigation = []
    if page > 0:
        navigation.append(
            types.InlineKeyboardButton(
                "◀️ Sebelumnya",
                callback_data=f"services:{country_id}:{page - 1}",
            )
        )
    if page < len(pages) - 1:
        navigation.append(
            types.InlineKeyboardButton(
                "Berikutnya ▶️",
                callback_data=f"services:{country_id}:{page + 1}",
            )
        )
    if navigation:
        keyboard.row(*navigation)
    keyboard.add(
        types.InlineKeyboardButton("⬅️ Pilih Negara", callback_data="menu:buy")
    )
    return keyboard


def products_keyboard(
    country_id: str,
    service_id: str,
    products: List[Dict[str, Any]],
    page: int = 0,
) -> types.InlineKeyboardMarkup:
    pages = list(chunked(products, MAX_BUTTONS_PER_PAGE))
    page = max(0, min(page, len(pages) - 1))
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for product in pages[page]:
        keyboard.add(
            types.InlineKeyboardButton(
                product_label(product)[:64],
                callback_data=f"confirm:{country_id}:{service_id}:{product['id']}",
            )
        )
    navigation = []
    if page > 0:
        navigation.append(
            types.InlineKeyboardButton(
                "◀️ Sebelumnya",
                callback_data=f"productspage:{country_id}:{service_id}:{page - 1}",
            )
        )
    if page < len(pages) - 1:
        navigation.append(
            types.InlineKeyboardButton(
                "Berikutnya ▶️",
                callback_data=f"productspage:{country_id}:{service_id}:{page + 1}",
            )
        )
    if navigation:
        keyboard.row(*navigation)
    keyboard.add(
        types.InlineKeyboardButton(
            "⬅️ Pilih Layanan",
            callback_data=f"services:{country_id}:0",
        )
    )
    return keyboard


def order_keyboard(order_id: str) -> types.InlineKeyboardMarkup:
    safe_id = str(order_id)[:40]
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("🔄 Cek OTP", callback_data=f"otp:{safe_id}"),
        types.InlineKeyboardButton("❌ Batalkan", callback_data=f"cancel:{safe_id}"),
    )
    keyboard.add(types.InlineKeyboardButton("⬅️ Menu Utama", callback_data="menu:home"))
    return keyboard


def deposit_keyboard(deposit_id: int) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=3)
    keyboard.add(
        *[
            types.InlineKeyboardButton(
                f"ACC {rupiah(amount)}",
                callback_data=f"depacc:{deposit_id}:{amount}",
            )
            for amount in DEPOSIT_AMOUNTS
        ]
    )
    keyboard.add(types.InlineKeyboardButton("❌ Tolak Deposit", callback_data=f"deprej:{deposit_id}"))
    return keyboard


def qris_available() -> bool:
    return bool(QRIS_IMAGE_URL or os.path.isfile(QRIS_IMAGE_PATH))


def send_qris_image(
    chat_id: int,
    caption: str,
    reply_markup: Optional[types.InlineKeyboardMarkup] = None,
) -> None:
    if QRIS_IMAGE_URL:
        bot.send_photo(
            chat_id,
            QRIS_IMAGE_URL,
            caption=caption,
            reply_markup=reply_markup,
        )
        return
    with open(QRIS_IMAGE_PATH, "rb") as qris_file:
        bot.send_photo(
            chat_id,
            qris_file,
            caption=caption,
            reply_markup=reply_markup,
        )


def deposit_instructions() -> str:
    qris_note = (
        "Scan QRIS pada gambar di atas."
        if qris_available()
        else "QRIS belum dikonfigurasi sebagai gambar. Admin dapat menambahkan QRIS_IMAGE_PATH atau QRIS_IMAGE_URL."
    )
    return (
        "<b>💳 Deposit Saldo via QRIS</b>\n\n"
        f"1. {qris_note}\n"
        "2. Bayar sesuai nominal yang diinginkan.\n"
        "3. Kirim screenshot/bukti pembayaran di chat ini.\n"
        "4. Admin akan memeriksa dan memilih nominal saldo yang sesuai.\n\n"
        "Nominal yang dapat diproses: "
        + ", ".join(rupiah(amount) for amount in DEPOSIT_AMOUNTS)
        + "\n\n"
        "Jangan kirim bukti yang sama lebih dari satu kali."
    )


# ---------------------------------------------------------------------------
# Commands and callbacks
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start", "menu"])
def handle_start(message: types.Message):
    upsert_user(message.from_user)
    send_home(message.chat.id)


@bot.message_handler(commands=["saldo", "balance"])
def handle_balance_command(message: types.Message):
    upsert_user(message.from_user)
    bot.reply_to(
        message,
        f"💰 Saldo Belza Digital kamu: <b>{rupiah(get_balance(message.from_user.id))}</b>",
        reply_markup=back_to_menu(),
    )


@bot.message_handler(commands=["beli", "buy"])
def handle_buy_command(message: types.Message):
    upsert_user(message.from_user)
    show_countries(message.chat.id)


@bot.message_handler(commands=["deposit", "isi_saldo"])
def handle_deposit_command(message: types.Message):
    upsert_user(message.from_user)
    show_deposit(message.chat.id)


def show_countries(chat_id: int, message: Optional[types.Message] = None, page: int = 0):
    try:
        countries = fetch_countries()
    except RuntimeError as error:
        text = f"⚠️ Gagal mengambil daftar negara: {html.escape(str(error))}"
        if message:
            bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=back_to_menu())
        else:
            bot.send_message(chat_id, text, reply_markup=back_to_menu())
        return
    if not countries:
        text = "⚠️ Daftar negara sedang kosong."
        if message:
            bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=back_to_menu())
        else:
            bot.send_message(chat_id, text, reply_markup=back_to_menu())
        return
    text = "<b>📱 Pilih Negara</b>\nHarga layanan akan ditambah margin Belza Digital."
    if message:
        bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=country_keyboard(countries, page))
    else:
        bot.send_message(chat_id, text, reply_markup=country_keyboard(countries, page))


def show_deposit(chat_id: int, message: Optional[types.Message] = None):
    text = deposit_instructions()
    if message:
        try:
            bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=back_to_menu())
            if qris_available():
                send_qris_image(chat_id, "QRIS Belza Digital")
            return
        except Exception:
            pass
    if qris_available():
        send_qris_image(chat_id, text, reply_markup=back_to_menu())
    else:
        bot.send_message(chat_id, text, reply_markup=back_to_menu())


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call: types.CallbackQuery):
    try:
        upsert_user(call.from_user)
        data = call.data or ""
        if data == "menu:home":
            bot.answer_callback_query(call.id)
            send_home(call.message.chat.id, call.message)
        elif data == "menu:buy":
            bot.answer_callback_query(call.id, "Mengambil daftar negara...")
            show_countries(call.message.chat.id, call.message)
        elif data.startswith("countries:"):
            bot.answer_callback_query(call.id)
            show_countries(call.message.chat.id, call.message, int(data.split(":")[1]))
        elif data.startswith("services:"):
            show_services_page_callback(call)
        elif data.startswith("productspage:"):
            show_products_page_callback(call)
        elif data == "menu:balance":
            bot.answer_callback_query(call.id)
            balance = get_balance(call.from_user.id)
            bot.edit_message_text(
                f"💰 Saldo kamu: <b>{rupiah(balance)}</b>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_to_menu(),
            )
        elif data == "menu:deposit":
            bot.answer_callback_query(call.id)
            show_deposit(call.message.chat.id, call.message)
        elif data.startswith("country:"):
            show_services_callback(call)
        elif data.startswith("products:"):
            show_products_callback(call)
        elif data.startswith("confirm:"):
            confirm_service_callback(call)
        elif data.startswith("buy:"):
            buy_service_callback(call)
        elif data.startswith("otp:"):
            check_otp_callback(call)
        elif data.startswith("cancel:"):
            cancel_order_callback(call)
        elif data.startswith("depacc:") or data.startswith("deprej:"):
            review_deposit_callback(call)
        else:
            bot.answer_callback_query(call.id, "Menu tidak dikenali.", show_alert=True)
    except Exception:
        logger.exception("Callback failed")
        try:
            bot.answer_callback_query(call.id, "Terjadi kesalahan. Silakan coba lagi.", show_alert=True)
        except Exception:
            pass


def show_services_callback(call: types.CallbackQuery):
    country_id = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id, "Mengambil daftar layanan...")
    try:
        countries = fetch_countries()
        country_name = next(
            (country["name"] for country in countries if country["id"] == country_id),
            country_id,
        )
        services = fetch_services(country_id)
        if not services:
            raise RuntimeError("Tidak ada layanan/stok untuk negara ini.")
        bot.edit_message_text(
            f"<b>🌍 {html.escape(country_name.title())}</b>\nPilih layanan yang ingin dibeli:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=services_keyboard(country_id, services, 0),
        )
    except RuntimeError as error:
        bot.edit_message_text(
            f"⚠️ {html.escape(str(error))}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=back_to_menu(),
        )


def confirm_service_callback(call: types.CallbackQuery):
    _, country_id, service_id, product_id = call.data.split(":", 3)
    bot.answer_callback_query(call.id)
    try:
        services = fetch_services(country_id)
        service = next((item for item in services if str(item["id"]) == service_id), None)
        if not service:
            raise RuntimeError("Layanan sudah tidak tersedia. Silakan muat ulang.")
        products = fetch_products(country_id, service_id)
        product = next((item for item in products if str(item["id"]) == product_id), None)
        if not product:
            raise RuntimeError("Server layanan sudah tidak tersedia. Silakan muat ulang.")
        sell_price = int(product["base_price"]) + MARGIN_KEUNTUNGAN
        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(
            types.InlineKeyboardButton(
                "✅ Konfirmasi Beli",
                callback_data=f"buy:{country_id}:{service_id}:{product_id}",
            ),
            types.InlineKeyboardButton(
                "⬅️ Kembali",
                callback_data=f"products:{country_id}:{service_id}",
            ),
        )
        bot.edit_message_text(
            "<b>Konfirmasi Pembelian</b>\n\n"
            f"Layanan: <b>{html.escape(str(service['name']))}</b>\n"
            f"Server: <b>{html.escape(str(product['name']))}</b>\n"
            f"Harga dasar: {rupiah(int(product['base_price']))}\n"
            f"Margin: {rupiah(MARGIN_KEUNTUNGAN)}\n"
            f"Total: <b>{rupiah(sell_price)}</b>\n\n"
            "Saldo akan dipotong setelah pembelian diproses.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard,
        )
    except RuntimeError as error:
        bot.edit_message_text(
            f"⚠️ {html.escape(str(error))}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=back_to_menu(),
        )


def show_products_callback(call: types.CallbackQuery):
    _, country_id, service_id = call.data.split(":", 2)
    show_products(call, country_id, service_id, 0)


def show_products_page_callback(call: types.CallbackQuery):
    _, country_id, service_id, page_text = call.data.split(":", 3)
    try:
        page = int(page_text)
    except ValueError:
        page = 0
    show_products(call, country_id, service_id, page)


def show_products(
    call: types.CallbackQuery,
    country_id: str,
    service_id: str,
    page: int,
) -> None:
    bot.answer_callback_query(call.id, "Mengambil daftar server...")
    try:
        services = fetch_services(country_id)
        service = next((item for item in services if str(item["id"]) == service_id), None)
        if not service:
            raise RuntimeError("Layanan sudah tidak tersedia.")
        products = fetch_products(country_id, service_id)
        if not products:
            raise RuntimeError("Tidak ada server/stok untuk layanan ini.")
        bot.edit_message_text(
            f"<b>🛠 {html.escape(str(service['name']))}</b>\n"
            "Pilih server yang ingin digunakan:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=products_keyboard(country_id, service_id, products, page),
        )
    except RuntimeError as error:
        bot.edit_message_text(
            f"⚠️ {html.escape(str(error))}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=back_to_menu(),
        )


def show_services_page_callback(call: types.CallbackQuery):
    _, country_id, page_text = call.data.split(":", 2)
    try:
        page = int(page_text)
    except ValueError:
        page = 0
    bot.answer_callback_query(call.id)
    try:
        countries = fetch_countries()
        country_name = next(
            (country["name"] for country in countries if country["id"] == country_id),
            country_id,
        )
        services = fetch_services(country_id)
        if not services:
            raise RuntimeError("Tidak ada layanan/stok untuk negara ini.")
        bot.edit_message_text(
            f"<b>🌍 {html.escape(country_name.title())}</b>\nPilih layanan yang ingin dibeli:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=services_keyboard(country_id, services, page),
        )
    except RuntimeError as error:
        bot.edit_message_text(
            f"⚠️ {html.escape(str(error))}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=back_to_menu(),
        )


def buy_service_callback(call: types.CallbackQuery):
    _, country_id, service_id, product_id = call.data.split(":", 3)
    user_id = call.from_user.id
    bot.answer_callback_query(call.id, "Memproses pembelian...")
    try:
        services = fetch_services(country_id)
        service = next((item for item in services if str(item["id"]) == service_id), None)
        if not service:
            raise RuntimeError("Layanan sudah tidak tersedia.")
        products = fetch_products(country_id, service_id)
        product = next((item for item in products if str(item["id"]) == product_id), None)
        if not product:
            raise RuntimeError("Server layanan sudah tidak tersedia.")
        price = int(product["base_price"]) + MARGIN_KEUNTUNGAN
        if not deduct_balance(user_id, price):
            raise RuntimeError(
                f"Saldo tidak cukup. Saldo kamu {rupiah(get_balance(user_id))}, "
                f"sedangkan harga {rupiah(price)}."
            )
        try:
            api_order = create_api_order(country_id, service_id, product_id)
        except Exception:
            add_balance(user_id, price)
            raise

        external_id = str(pick_value(api_order, "id", "order_id", "order", default=""))
        if not external_id:
            add_balance(user_id, price)
            raise RuntimeError("API tidak mengembalikan ID order.")
        number = str(pick_value(api_order, "number", "phone", default="Menunggu"))
        status = str(pick_value(api_order, "status", default="pending"))
        save_order(
            user_id,
            external_id,
            country_id,
            country_id,
            service_id,
            f"{service['name']} - {product['name']}",
            number,
            price,
            status,
        )
        bot.edit_message_text(
            "<b>✅ Order Berhasil</b>\n\n"
            f"Order ID: <code>{html.escape(external_id)}</code>\n"
            f"Layanan: {html.escape(str(service['name']))}\n"
            f"Server: {html.escape(str(product['name']))}\n"
            f"Nomor: <code>{html.escape(number)}</code>\n"
            f"Terpotong: <b>{rupiah(price)}</b>\n\n"
            "Gunakan tombol Cek OTP setelah SMS masuk. Kamu juga bisa membatalkan "
            "selama order masih dapat dibatalkan.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=order_keyboard(external_id),
        )
    except RuntimeError as error:
        bot.edit_message_text(
            f"⚠️ Pembelian gagal: {html.escape(str(error))}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=back_to_menu(),
        )


def check_otp_callback(call: types.CallbackQuery):
    order_id = call.data.split(":", 1)[1]
    order = get_order_for_user(call.from_user.id, order_id)
    if not order:
        bot.answer_callback_query(call.id, "Order tidak ditemukan.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Mengecek status OTP...")
    try:
        data = check_api_order(order_id)
        otp_value = pick_value(data, "otp_code", "otp", "code", "sms", default=None)
        otp = str(otp_value or "").strip()
        status = str(pick_value(data, "status", default=order["status"]))
        number = str(pick_value(data, "number", "phone", default=order["number"]))
        update_order(order_id, status=status, number=number)

        if otp and otp.lower() not in {"waiting", "pending", "null", "none", ""}:
            bot.edit_message_text(
                "<b>📩 Kode OTP Diterima</b>\n\n"
                f"Nomor: <code>{html.escape(number)}</code>\n"
                f"Kode: <code>{html.escape(otp)}</code>\n"
                f"Status: <b>{html.escape(status)}</b>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=order_keyboard(order_id),
            )
        else:
            bot.edit_message_text(
                "<b>⏳ Menunggu OTP</b>\n\n"
                f"Order: <code>{html.escape(order_id)}</code>\n"
                "Kode OTP akan dikirim otomatis saat SMS masuk.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=order_keyboard(order_id),
            )
    except Exception as error:
        bot.edit_message_text(
            "<b>⏳ Menunggu OTP</b>\n\n"
            f"Order: <code>{html.escape(order_id)}</code>\n"
            "Kode OTP akan dikirim otomatis saat SMS masuk.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=order_keyboard(order_id),
        )


def cancel_order_callback(call: types.CallbackQuery):
    order_id = call.data.split(":", 1)[1]
    order = get_order_for_user(call.from_user.id, order_id)
    if not order:
        bot.answer_callback_query(call.id, "Order tidak ditemukan.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Membatalkan order...")
    try:
        data = cancel_api_order(order_id)
        status = str(pick_value(data, "status", default="canceled")).lower()
        if status not in {"cancelled", "canceled", "cancel"}:
            raise RuntimeError(f"API mengembalikan status {status}.")
        refunded = refund_order_once(order_id)
        update_order(order_id, status="canceled")
        if refunded is None:
            text = "Order sudah pernah diproses atau saldonya sudah dikembalikan."
        else:
            text = f"✅ Order dibatalkan. Saldo dikembalikan: <b>{rupiah(refunded)}</b>."
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=back_to_menu(),
        )
    except RuntimeError as error:
        bot.send_message(call.message.chat.id, f"⚠️ Gagal membatalkan order: {html.escape(str(error))}")


def review_deposit_callback(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Khusus admin.", show_alert=True)
        return
    parts = call.data.split(":")
    deposit_id = int(parts[1])
    if parts[0] == "depacc":
        amount = int(parts[2])
        user_id = approve_deposit(deposit_id, amount)
        if not user_id:
            bot.answer_callback_query(call.id, "Deposit sudah diproses.", show_alert=True)
            return
        new_balance = get_balance(user_id)
        bot.answer_callback_query(call.id, "Deposit disetujui.")
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None,
        )
        bot.send_message(
            user_id,
            f"✅ Deposit {rupiah(amount)} disetujui.\n"
            f"Saldo kamu sekarang: <b>{rupiah(new_balance)}</b>",
        )
        bot.send_message(
            call.message.chat.id,
            f"Deposit #{deposit_id} disetujui untuk user <code>{user_id}</code>: {rupiah(amount)}.",
        )
    else:
        user_id = reject_deposit(deposit_id)
        if not user_id:
            bot.answer_callback_query(call.id, "Deposit sudah diproses.", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Deposit ditolak.")
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None,
        )
        bot.send_message(user_id, "❌ Bukti deposit kamu ditolak admin. Silakan kirim bukti yang valid.")


@bot.message_handler(content_types=["photo"])
def handle_payment_proof(message: types.Message):
    upsert_user(message.from_user)
    deposit_id = create_deposit(message.from_user.id, message.id)
    caption = (
        "🧾 <b>Bukti Deposit Baru</b>\n"
        f"Deposit ID: <code>{deposit_id}</code>\n"
        f"User ID: <code>{message.from_user.id}</code>\n"
        f"Username: @{html.escape(message.from_user.username or '-')} \n\n"
        "Pilih nominal untuk menyetujui saldo:"
    )
    try:
        forwarded = bot.forward_message(ADMIN_ID, message.chat.id, message.id)
        admin_message = bot.send_message(
            ADMIN_ID,
            caption,
            reply_to_message_id=forwarded.message_id,
            reply_markup=deposit_keyboard(deposit_id),
        )
        set_deposit_admin_message(deposit_id, admin_message.message_id)
        bot.reply_to(
            message,
            "✅ Bukti pembayaran sudah diteruskan ke admin. Tunggu verifikasi saldo.",
        )
    except Exception:
        logger.exception("Could not forward deposit proof")
        bot.reply_to(message, "⚠️ Bukti tersimpan, tetapi gagal diteruskan ke admin. Hubungi admin.")


@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_unknown_text(message: types.Message):
    upsert_user(message.from_user)
    bot.reply_to(
        message,
        "Silakan gunakan menu Belza Digital di bawah.",
        reply_markup=main_menu(),
    )


def main() -> None:
    init_database()
    keep_alive()
    configure_nusa_webhook()
    logger.info("Belza Digital bot started.")
    bot.infinity_polling(skip_pending=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
