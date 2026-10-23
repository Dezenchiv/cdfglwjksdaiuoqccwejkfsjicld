# -*- coding: utf-8 -*-
"""Бот Tosamoe — полный код с изменениями:
- Реклама после 10 просмотров (не при /start)
- Логирование активации промокодов
- db_check с try/except и callback.answer()
- errors_handler логирует все BadRequest
- render_earn_menu в 2 столбца + кнопка Кликер
- Рассылка с паузой 0.2 + TelegramRetryAfter
- safe_send с TelegramRetryAfter
- on_channel_post с проверкой дубликатов
- Жалоба на видео: меню из 3 причин
"""

import os
os.environ.setdefault("TZ", "Europe/Moscow")
try:
    import time as _time
    _time.tzset()
except Exception:
    pass

import asyncio
import json
import logging
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

import aiohttp
from aiogram import Bot, Dispatcher, BaseMiddleware, Router, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramRetryAfter, TelegramUnauthorizedError, TelegramConflictError,
    TelegramForbiddenError, TelegramBadRequest
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, FSInputFile, BufferedInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    from aiosend import CryptoPay
    from aiosend.types import Invoice
    HAS_CRYPTO = True
except ImportError:
    CryptoPay = None
    Invoice = None
    HAS_CRYPTO = False


def env_int(key, default):
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
LOG_CHANNEL = env_int("LOG_CHANNEL", 0)
DB_PATH = os.getenv("DB_PATH", "bot_database.db")

DEFAULT_HOURLY = 10
DEFAULT_DAILY = 10
DEFAULT_PAID_PRICE = 5
DEFAULT_REF_BONUS = 20
START_BALANCE = 10
FLOOD_LIMIT = 15
FLOOD_WINDOW = 10

AUTO_DELETE_SECONDS = 1800
BOT_MSG_DELETE_SECONDS = 86400
SUGGEST_MAX = 50
PT_RATE_RUB = 10
PT_RATE_STAR = 10
PT_MIN = 300

STARS_RECEIVER = os.getenv("STARS_RECEIVER", "mleur")
STARS_WAIT_HOURS = 12

STREAK_REWARDS = [10, 15, 20, 25, 30, 35, 40]
STREAK_AFTER_7 = 20
STREAK_PLUS_DAY = 7

CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "")
cp = None
if HAS_CRYPTO and CRYPTO_PAY_TOKEN:
    try:
        cp = CryptoPay(CRYPTO_PAY_TOKEN)
    except Exception as e:
        print(f"CryptoPay init: {e}")
        cp = None

DARKBOOST_API_KEY = os.getenv("DARKBOOST_API_KEY", "")
DARKBOOST_BASE = "https://darkboosts.com"
MAX_OFFERS = 4
REMIND_INTERVAL = 6 * 3600
HAS_DARKBOOST = bool(DARKBOOST_API_KEY)

VIDEO_HOWTO = "https://youtube.com/shorts/z0Mr05YlaPs?si=tGZPJS7JtKLc9it2"

_db_sessions = {}
_db_sponsors = {}
_http_session = None

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан (env)")
if not ADMIN_ID_RAW:
    raise SystemExit("ADMIN_ID не задан (env)")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    raise SystemExit("ADMIN_ID должен быть числом")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=3,
                            encoding="utf-8")
    ]
)
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

flood_buffer = {}
_media_order_cache = {}
_report_cooldown = {}


async def _auto_del_task(chat_id, message_id, bot_instance, delay=None):
    try:
        if not is_auto_delete_enabled():
            return
        if delay is None:
            delay = get_auto_delete_delay()
        await asyncio.sleep(delay)
        await bot_instance.delete_message(chat_id, message_id)
    except Exception:
        pass


def _schedule_auto_del(sent, bot_instance, delay=None):
    if not sent or not getattr(sent, "message_id", None):
        return
    try:
        chat = getattr(sent, "chat", None)
        if not chat or getattr(chat, "type", "") != "private":
            return
        asyncio.create_task(_auto_del_task(chat.id, sent.message_id,
                                            bot_instance, delay))
    except Exception:
        pass


_original_message_answer = types.Message.answer
_original_message_reply = types.Message.reply


async def _patched_message_answer(self, text=None, **kwargs):
    sent = await _original_message_answer(self, text, **kwargs)
    b = getattr(self, "bot", None) or bot
    _schedule_auto_del(sent, b, BOT_MSG_DELETE_SECONDS)
    return sent


async def _patched_message_reply(self, text=None, **kwargs):
    sent = await _original_message_reply(self, text, **kwargs)
    b = getattr(self, "bot", None) or bot
    _schedule_auto_del(sent, b, BOT_MSG_DELETE_SECONDS)
    return sent


types.Message.answer = _patched_message_answer
types.Message.reply = _patched_message_reply


def schedule_auto_delete_message(bot_instance, chat_id, message_id, delay=None):
    if not is_auto_delete_enabled():
        return
    if delay is None:
        delay = get_auto_delete_delay()

    async def _del():
        try:
            await asyncio.sleep(delay)
            await bot_instance.delete_message(chat_id, message_id)
        except Exception:
            pass

    asyncio.create_task(_del())


AGREEMENT_TEXT = """⚠️ <b>ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ</b> ⚠️

<b>I. Поддержка</b>
1. Пиши в поддержку адекватно и без обмана.
1.1 Проблемы сообщай в поддержку.
1.2 Если не отвечают более дня — обращение незначительно.
1.3 Поддержка вправе забанить за нарушение.
1.4 Писать можно сколько хочешь, но только по теме.

<b>II. Контент</b>
2. При просмотре можно сохранять в избранное.
2.1 После покупки разрешено не удалять материалы.
2.2 Контент обновляется когда есть что публиковать.
2.3 Если купил и не понравилось — ✈️ не возвращаются.
2.4 Пробных видео нет.
2.5 Видео не открывается — напиши в поддержку.

<b>III. Магазин</b>
3. В магазине только эксклюзив.
3.1 Купленное остаётся навсегда.
3.2 Цены могут отличаться.

<b>IV. Функционал</b>
4. Бот развлекательный, контент сгенерирован ИИ.
4.1 Валюта ✈️ — получай каждый день/час.
4.2 Топ-1 недели получает 100 ✈️.
4.3 Бот может лагать.
4.4 Есть предложка — платим ✈️.

<b>V. Правила</b>
5. Запрещён абуз.
5.1 Запрещён флуд в поддержку.
5.2 Запрещена продажа ✈️."""


def check_flood(user_id):
    now = time.time()
    t = flood_buffer.get(user_id, [])
    t = [x for x in t if now - x < FLOOD_WINDOW]
    t.append(now)
    flood_buffer[user_id] = t
    return len(t) > FLOOD_LIMIT


# =========================================================
# DARKBOOST — API
# =========================================================
async def db_http():
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15))
    return _http_session


async def db_api_sponsors(uid: int, username: str) -> dict:
    if not HAS_DARKBOOST:
        return {"status": "disabled"}
    s = await db_http()
    try:
        async with s.post(
            f"{DARKBOOST_BASE}/api/v1/sponsors",
            headers={"Auth": DARKBOOST_API_KEY},
            json={"user_id": uid, "chat_id": uid,
                  "username": username or "", "max_sponsors": MAX_OFFERS},
        ) as r:
            d = await r.json()
            log.info(f"db sponsors: status={d.get('status')}, "
                     f"count={len(d.get('sponsors', []))}")
            return d
    except Exception as e:
        log.error(f"db sponsors err: {e}")
        return {"status": "failed"}


async def db_api_check(uid: int, sid: int) -> dict:
    if not HAS_DARKBOOST:
        return {"status": "disabled"}
    s = await db_http()
    try:
        async with s.post(
            f"{DARKBOOST_BASE}/api/v1/check",
            headers={"Auth": DARKBOOST_API_KEY},
            json={"user_id": uid, "chat_id": uid, "session_id": sid},
        ) as r:
            d = await r.json()
            log.info(f"db check: status={d.get('status')}")
            return d
    except Exception as e:
        log.error(f"db check err: {e}")
        return {"status": "failed"}


def db_needs_check(uid: int) -> bool:
    if not HAS_DARKBOOST:
        return False
    val = get_setting(f"db_last_{uid}", "0")
    try:
        last = float(val)
    except Exception:
        last = 0
    if last <= 0:
        return True
    return (time.time() - last) >= REMIND_INTERVAL


def db_mark_checked(uid: int):
    ts = str(time.time())
    _settings_cache[f"db_last_{uid}"] = ts
    conn = db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
              (f"db_last_{uid}", ts))
    conn.commit()
    conn.close()
    log.info(f"🛡 db_mark_checked: uid={uid}, ts={ts}")


# =========================================================
# MIDDLEWARE
# =========================================================
class DarkBoostMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = None
        if isinstance(event, types.Message):
            user = event.from_user
        elif isinstance(event, types.CallbackQuery):
            user = event.from_user
        if not user:
            return await handler(event, data)
        if user.id == ADMIN_ID:
            return await handler(event, data)
        if not HAS_DARKBOOST:
            return await handler(event, data)

        if isinstance(event, types.Message):
            txt = event.text or ""
            if txt.startswith("/start"):
                return await handler(event, data)

        if isinstance(event, types.CallbackQuery):
            d = event.data or ""
            if d.startswith("db:") or d == "agree:accept":
                return await handler(event, data)

        if not db_needs_check(user.id):
            return await handler(event, data)

        try:
            if isinstance(event, types.Message):
                await show_db_sponsors(event, user.id,
                                        user.username or "", "")
            else:
                await event.answer(
                    "⚠️ Подпишись на спонсоров! Нажми /start.",
                    show_alert=True)
        except Exception as e:
            log.error(f"DarkBoostMiddleware: {e}")
        return


class AntiFloodMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = None
        if isinstance(event, types.Message):
            user = event.from_user
        elif isinstance(event, types.CallbackQuery):
            user = event.from_user
        if user and user.id != ADMIN_ID:
            if check_flood(user.id):
                try:
                    if isinstance(event, types.Message):
                        await event.answer("⚠️ Слишком много сообщений.")
                    else:
                        await event.answer("⚠️ Слишком много запросов.",
                                           show_alert=False)
                except Exception:
                    pass
                return
        return await handler(event, data)


class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = None
        if isinstance(event, types.Message):
            user = event.from_user
        elif isinstance(event, types.CallbackQuery):
            user = event.from_user
        if user and user.id == ADMIN_ID:
            return await handler(event, data)
        if is_bot_stopped():
            try:
                if isinstance(event, types.Message):
                    await event.answer(
                        "🔧 <b>Бот на техническом перерыве</b>\n\n"
                        "Пожалуйста, подождите — скоро вернёмся!",
                        parse_mode="HTML")
                elif isinstance(event, types.CallbackQuery):
                    await event.answer("🔧 Бот на тех. перерыве. Подождите.",
                                       show_alert=True)
            except Exception:
                pass
            return
        return await handler(event, data)


class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = None
        if isinstance(event, types.Message):
            user = event.from_user
        elif isinstance(event, types.CallbackQuery):
            user = event.from_user
        if not user:
            return await handler(event, data)
        if user.id == ADMIN_ID:
            return await handler(event, data)
        if is_banned(user.id):
            try:
                if isinstance(event, types.Message):
                    await event.answer("🚫 Вы забанены.")
                elif isinstance(event, types.CallbackQuery):
                    await event.answer("🚫 Вы забанены.", show_alert=True)
            except Exception:
                pass
            return
        return await handler(event, data)


def _register_middleware(d: Dispatcher):
    d.message.middleware(DarkBoostMiddleware())
    d.message.middleware(MaintenanceMiddleware())
    d.message.middleware(BanMiddleware())
    d.message.middleware(AntiFloodMiddleware())
    d.callback_query.middleware(DarkBoostMiddleware())
    d.callback_query.middleware(MaintenanceMiddleware())
    d.callback_query.middleware(BanMiddleware())
    d.callback_query.middleware(AntiFloodMiddleware())


_register_middleware(dp)


def kb_db_sponsors(sponsors: list):
    b = InlineKeyboardBuilder()
    for s in sponsors[:MAX_OFFERS]:
        b.button(text=s["title"], url=s["link"])
    b.button(text="✅ Проверить", callback_data="db:check")
    b.button(text="🔙 Заново", callback_data="db:restart")
    b.adjust(1)
    return b.as_markup()


def kb_db_retry():
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Проверить ещё раз", callback_data="db:check")
    b.button(text="🔙 Заново", callback_data="db:restart")
    b.adjust(1)
    return b.as_markup()
    # =========================================================
# БД
# =========================================================
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-20000")
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        joined_at TEXT, last_seen TEXT, banned INTEGER DEFAULT 0,
        downloads INTEGER DEFAULT 0, balance INTEGER DEFAULT 0,
        last_accrual TEXT, last_daily TEXT, activated INTEGER DEFAULT 0,
        agreement_accepted INTEGER DEFAULT 0,
        last_balance_change TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
        emoji TEXT DEFAULT '📁', paid_price INTEGER DEFAULT 5,
        channel_id INTEGER DEFAULT 0, cover TEXT DEFAULT '',
        created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS media (
        id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER,
        channel_id INTEGER DEFAULT 0, channel_msg_id INTEGER DEFAULT 0,
        file_id TEXT, file_type TEXT, caption TEXT, added_at TEXT,
        views INTEGER DEFAULT 0,
        FOREIGN KEY (category_id) REFERENCES categories (id) ON DELETE CASCADE)""")
    c.execute("""CREATE TABLE IF NOT EXISTS media_purchases (
        user_id INTEGER, media_id INTEGER, bought_at TEXT,
        price_paid INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, media_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS cat_purchases (
        user_id INTEGER, category_id INTEGER, bought_at TEXT,
        price_paid INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, category_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS purchase_cooldown (
        user_id INTEGER, target TEXT, last_attempt TEXT,
        PRIMARY KEY (user_id, target))""")
    c.execute("""CREATE TABLE IF NOT EXISTS referrals (
        user_id INTEGER PRIMARY KEY, inviter_id INTEGER, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS downloads_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        media_id INTEGER, ts TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS support_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        subject TEXT, message TEXT, ts TEXT, answered INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS shop_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT,
        price INTEGER, content_type TEXT, content TEXT,
        requires_photo INTEGER DEFAULT 0,
        created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS shop_purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item_id INTEGER,
        price INTEGER, bought_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT,
        emoji TEXT, reward INTEGER, media_type TEXT, media_count INTEGER,
        is_repeatable INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
        created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS task_progress (
        user_id INTEGER, task_id INTEGER, file_ids TEXT, media_type TEXT,
        updated_at TEXT, PRIMARY KEY (user_id, task_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS task_completions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, task_id INTEGER,
        status TEXT, admin_msg_id INTEGER, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS promocodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE,
        reward INTEGER, max_uses INTEGER, uses INTEGER DEFAULT 0,
        expires_at TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS promo_activations (
        user_id INTEGER, code TEXT, activated_at TEXT,
        PRIMARY KEY (user_id, code))""")
    c.execute("""CREATE TABLE IF NOT EXISTS promo_privs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE,
        priv_type TEXT, days INTEGER, max_uses INTEGER, uses INTEGER DEFAULT 0,
        expires_at TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS promo_priv_activations (
        user_id INTEGER, code TEXT, activated_at TEXT,
        PRIMARY KEY (user_id, code))""")
    c.execute("""CREATE TABLE IF NOT EXISTS activity_bonus (
        user_id INTEGER PRIMARY KEY, last_bonus_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS blocked_users (
        user_id INTEGER PRIMARY KEY, blocked_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS plus_subscriptions (
        user_id INTEGER PRIMARY KEY, expires_at TEXT,
        bought_at TEXT, price_paid INTEGER DEFAULT 0,
        custom_cat_used INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS allow_subscriptions (
        user_id INTEGER PRIMARY KEY, expires_at TEXT,
        bought_at TEXT, price_paid INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS balance_reasons (
        id INTEGER PRIMARY KEY AUTOINCREMENT, reason TEXT UNIQUE,
        created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS crypto_topups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        invoice_id TEXT, amount_pt INTEGER, amount_usdt REAL,
        status TEXT DEFAULT 'pending', created_at TEXT, paid_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS balance_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        reason TEXT,
        admin_id INTEGER DEFAULT 0,
        ts TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_balance_log_user ON balance_log(user_id, ts)")
    c.execute("""CREATE TABLE IF NOT EXISTS user_streaks (
        user_id INTEGER PRIMARY KEY, streak INTEGER DEFAULT 0,
        last_claim TEXT, total_claims INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS quests_weekly (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE,
        name TEXT, description TEXT, target INTEGER, reward INTEGER, emoji TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS quest_progress (
        user_id INTEGER, quest_id INTEGER, progress INTEGER DEFAULT 0,
        claimed INTEGER DEFAULT 0, week_start TEXT,
        PRIMARY KEY (user_id, quest_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS low_balance_notified (
        user_id INTEGER PRIMARY KEY, notified_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS low_earn_push (
        user_id INTEGER PRIMARY KEY, last_push TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_activated ON users(activated)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_banned ON users(banned)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_media_category ON media(category_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_media_channel ON media(channel_id, channel_msg_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_downloads_user_ts ON downloads_log(user_id, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_media_purchases_user ON media_purchases(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cat_purchases_user ON cat_purchases(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_promo_uses ON promocodes(uses)")
    conn.commit()
    conn.close()


init_db()


def migrate_db():
    conn = db()
    c = conn.cursor()
    for table, cols in [
        ("users", [("balance", "INTEGER DEFAULT 0"), ("last_accrual", "TEXT"),
                   ("last_daily", "TEXT"), ("activated", "INTEGER DEFAULT 0"),
                   ("agreement_accepted", "INTEGER DEFAULT 0"),
                   ("last_balance_change", "TEXT")]),
        ("categories", [("paid_price", "INTEGER DEFAULT 5"),
                        ("channel_id", "INTEGER DEFAULT 0"),
                        ("cover", "TEXT DEFAULT ''")]),
        ("media", [("channel_id", "INTEGER DEFAULT 0"),
                   ("channel_msg_id", "INTEGER DEFAULT 0"),
                   ("file_id", "TEXT")]),
        ("media_purchases", [("price_paid", "INTEGER DEFAULT 0")]),
        ("cat_purchases", [("price_paid", "INTEGER DEFAULT 0")]),
        ("shop_items", [("requires_photo", "INTEGER DEFAULT 0")]),
    ]:
        c.execute(f"PRAGMA table_info({table})")
        existing = {r[1] for r in c.fetchall()}
        for col, typ in cols:
            if col not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()
    conn.close()


migrate_db()


def seed_quests():
    conn = db()
    c = conn.cursor()
    quests = [
        ("streak_7", "Стрик 7 дней", "Заходи 7 дней подряд", 7, 50, "🔥"),
        ("views_30", "30 видео", "Посмотри 30 видео за неделю", 30, 50, "🎬"),
        ("refs_5", "5 друзей", "Пригласи 5 друзей за неделю", 5, 100, "👥"),
    ]
    for code, name, desc, target, reward, emoji in quests:
        c.execute("""INSERT OR IGNORE INTO quests_weekly
                     (code, name, description, target, reward, emoji)
                     VALUES (?,?,?,?,?,?)""",
                  (code, name, desc, target, reward, emoji))
    conn.commit()
    conn.close()


seed_quests()


# =========================================================
# SETTINGS
# =========================================================
_settings_cache = {}


def get_setting(key, default=""):
    if key in _settings_cache:
        return _settings_cache[key]
    conn = db()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    r = c.fetchone()
    conn.close()
    val = r[0] if r else default
    _settings_cache[key] = val
    return val


def set_setting(key, value):
    _settings_cache[key] = str(value)
    conn = db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
              (key, str(value)))
    conn.commit()
    conn.close()


def is_bot_stopped():
    return get_setting("bot_stopped", "0") == "1"


def set_bot_stopped(value):
    set_setting("bot_stopped", "1" if value else "0")


def get_hourly_reward():
    return int(get_setting("hourly_reward", str(DEFAULT_HOURLY)))


def get_daily_bonus():
    return int(get_setting("daily_bonus", str(DEFAULT_DAILY)))


def get_daily_bonus_with_multiplier(user_id=None):
    base = get_daily_bonus()
    is_weekend = datetime.now().weekday() >= 5
    if is_weekend:
        base = int(base * 1.5)
    return base, is_weekend


def get_top_reward(place):
    defaults = {1: "100", 2: "50", 3: "25"}
    return int(get_setting(f"top_reward_{place}", defaults.get(place, "0")))


def set_top_reward(place, value):
    set_setting(f"top_reward_{place}", str(value))


def get_referral_bonus():
    return int(get_setting("referral_bonus", str(DEFAULT_REF_BONUS)))


def get_plus_price():
    return int(get_setting("plus_price", "350"))


def get_plus_hourly_bonus():
    return int(get_setting("plus_hourly_bonus", "20"))


def get_plus_daily_bonus():
    return int(get_setting("plus_daily_bonus", "20"))


def get_plus_discount_mult():
    return int(get_setting("plus_discount_mult", "2"))


def get_plus_days():
    return int(get_setting("plus_days", "7"))


def get_allow_price():
    return int(get_setting("allow_price", "1000"))


def get_allow_days():
    return int(get_setting("allow_days", "7"))


def is_auto_delete_enabled():
    return get_setting("auto_delete_enabled", "1") == "1"


def set_auto_delete_enabled(value):
    set_setting("auto_delete_enabled", "1" if value else "0")


def get_auto_delete_delay():
    return int(get_setting("auto_delete_delay", str(AUTO_DELETE_SECONDS)))


def get_broadcast_signature_enabled():
    return get_setting("broadcast_signature", "1") == "1"


def set_broadcast_signature_enabled(value):
    set_setting("broadcast_signature", "1" if value else "0")


def get_broadcast_signature_text():
    return get_setting(
        "broadcast_signature_text",
        "\n\n━━━━━━━━━━━━\n"
        "📢 <b>Канал:</b> https://t.me/+CUxA5RAR1604Nzk8")


# =========================================================
# HELPERS
# =========================================================
async def send_log(text, bot_instance=None):
    if not LOG_CHANNEL:
        return
    try:
        b = bot_instance or bot
        await b.send_message(LOG_CHANNEL, text, parse_mode="HTML")
    except Exception as e:
        log.warning(f"Log: {e}")


# ИЗМЕНЕНО: safe_send с TelegramRetryAfter
async def safe_send(bot_instance, chat_id, text, **kwargs):
    if is_user_blocked(chat_id):
        return False
    try:
        await bot_instance.send_message(chat_id, text, **kwargs)
        return True
    except TelegramForbiddenError:
        mark_user_blocked(chat_id)
        log.info(f"🚫 Юзер {chat_id} заблокировал бота")
        return False
    except TelegramRetryAfter as e:
        wait = e.retry_after + 1
        log.warning(f"⏳ Flood {chat_id}: ждём {wait} сек")
        await asyncio.sleep(wait)
        try:
            await bot_instance.send_message(chat_id, text, **kwargs)
            return True
        except Exception as e2:
            log.warning(f"safe_send retry {chat_id}: {e2}")
            return False
    except Exception as e:
        log.warning(f"safe_send {chat_id}: {e}")
        return False


def greeting():
    h = datetime.now().hour
    if 5 <= h < 12:
        return "🌅 Доброе утро"
    if 12 <= h < 18:
        return "☀️ Добрый день"
    if 18 <= h < 23:
        return "🌆 Добрый вечер"
    return "🌙 Доброй ночи"


# =========================================================
# БАЛАНС + ИСТОРИЯ
# =========================================================
def get_balance(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    return r[0] if r else 0


def log_balance_change(user_id, amount, reason, admin_id=0):
    if not reason:
        reason = "—"
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT INTO balance_log (user_id, amount, reason, admin_id, ts)
                 VALUES (?,?,?,?,?)""",
              (user_id, amount, reason, admin_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_balance_history(user_id, limit=10):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT amount, reason, admin_id, ts FROM balance_log
                 WHERE user_id=? ORDER BY id DESC LIMIT ?""",
              (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows


def add_balance(user_id, amount, reason="", admin_id=0):
    conn = db()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("""UPDATE users SET balance = balance + ?, last_balance_change=?
                 WHERE user_id=?""", (amount, now, user_id))
    conn.commit()
    c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    if amount != 0:
        log_balance_change(user_id, amount, reason, admin_id)
    return r[0] if r else 0


def spend_balance(user_id, amount, reason="", admin_id=0):
    conn = db()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("""UPDATE users SET balance = balance - ?, last_balance_change=?
                 WHERE user_id=? AND balance >= ?""",
              (amount, now, user_id, amount))
    conn.commit()
    affected = c.rowcount
    conn.close()
    if affected > 0 and amount != 0:
        log_balance_change(user_id, -amount, reason, admin_id)
    return affected > 0


def set_balance(user_id, value):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET balance=? WHERE user_id=?", (value, user_id))
    conn.commit()
    conn.close()


def can_take_hourly(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT last_accrual FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    if not r or not r[0]:
        return True
    try:
        return (datetime.now() - datetime.fromisoformat(r[0])).total_seconds() >= 3600
    except Exception:
        return True


def can_take_daily(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT last_daily FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    if not r or not r[0]:
        return True
    try:
        return (datetime.now() - datetime.fromisoformat(r[0])).total_seconds() >= 86400
    except Exception:
        return True


def get_time_until_hourly(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT last_accrual FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    if not r or not r[0]:
        return 0
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(r[0])).total_seconds()
        return max(0, int(3600 - elapsed))
    except Exception:
        return 0


def get_time_until_daily(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT last_daily FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    if not r or not r[0]:
        return 0
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(r[0])).total_seconds()
        return max(0, int(86400 - elapsed))
    except Exception:
        return 0


def format_time(seconds):
    if seconds <= 0:
        return "готово"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    parts = []
    if d > 0:
        parts.append(f"{d}д")
    if h > 0:
        parts.append(f"{h}ч")
    if m > 0:
        parts.append(f"{m}мин")
    if s > 0 and d == 0 and h == 0:
        parts.append(f"{s}сек")
    return " ".join(parts) if parts else "0сек"


def check_purchase_cooldown(user_id, target, seconds=2):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT last_attempt FROM purchase_cooldown WHERE user_id=? AND target=?",
              (user_id, target))
    r = c.fetchone()
    now = datetime.now()
    if r and r[0]:
        try:
            if (now - datetime.fromisoformat(r[0])).total_seconds() < seconds:
                conn.close()
                return False
        except Exception:
            pass
    c.execute("""INSERT OR REPLACE INTO purchase_cooldown
                 (user_id, target, last_attempt) VALUES (?,?,?)""",
              (user_id, target, now.isoformat()))
    conn.commit()
    conn.close()
    return True


# =========================================================
# СТРИК
# =========================================================
def get_streak(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT streak, last_claim, total_claims FROM user_streaks WHERE user_id=?",
              (user_id,))
    r = c.fetchone()
    conn.close()
    if not r:
        return 0, None, 0
    return r[0] or 0, r[1], r[2] or 0


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def can_claim_streak(user_id):
    streak, last_claim, _ = get_streak(user_id)
    last = _parse_dt(last_claim)
    if not last:
        return True
    return last.date() < datetime.now().date()


def get_streak_next_day(user_id):
    streak, last_claim, _ = get_streak(user_id)
    last = _parse_dt(last_claim)
    if not last:
        return 1
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    if last.date() == yesterday:
        return streak + 1
    if last.date() == today:
        return streak
    return 1


def get_streak_reward(day_number):
    if 1 <= day_number <= 7:
        return STREAK_REWARDS[day_number - 1]
    return STREAK_AFTER_7


def claim_streak(user_id):
    streak, last_claim, total = get_streak(user_id)
    last = _parse_dt(last_claim)
    today = datetime.now().date()
    if last and last.date() == today:
        return False, streak, 0, False, "already_claimed"
    if last and last.date() == today - timedelta(days=1):
        new_day = streak + 1
    else:
        new_day = 1
    reward = get_streak_reward(new_day)
    plus_granted = False
    if new_day == STREAK_PLUS_DAY:
        admin_grant_plus(user_id, 1)
        plus_granted = True
    add_balance(user_id, reward, f"🔥 Стрик день {new_day}")
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT INTO user_streaks (user_id, streak, last_claim, total_claims)
                 VALUES (?,?,?,?)
                 ON CONFLICT(user_id) DO UPDATE SET
                   streak=excluded.streak,
                   last_claim=excluded.last_claim,
                   total_claims=excluded.total_claims""",
              (user_id, new_day, datetime.now().isoformat(), total + 1))
    conn.commit()
    conn.close()
    return True, new_day, reward, plus_granted, "ok"


# =========================================================
# АКТИВНОСТЬ
# =========================================================
def was_inactive_3days(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT last_seen FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    if not r or not r[0]:
        conn.close()
        return False
    try:
        last = datetime.fromisoformat(r[0])
    except Exception:
        conn.close()
        return False
    if (datetime.now() - last).total_seconds() < 3 * 86400:
        conn.close()
        return False
    c.execute("SELECT last_bonus_at FROM activity_bonus WHERE user_id=?", (user_id,))
    b = c.fetchone()
    conn.close()
    if b and b[0]:
        try:
            if (datetime.now() - datetime.fromisoformat(b[0])).total_seconds() < 7 * 86400:
                return False
        except Exception:
            pass
    return True


def give_activity_bonus(user_id, amount=25):
    add_balance(user_id, amount, "🎁 Бонус за возвращение")
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO activity_bonus (user_id, last_bonus_at)
                 VALUES (?, ?)""",
              (user_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return get_balance(user_id)


# =========================================================
# БЛОКИРОВКА
# =========================================================
def mark_user_blocked(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO blocked_users (user_id, blocked_at)
                 VALUES (?, ?)""",
              (user_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def is_user_blocked(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    return r is not None


def unmark_user_blocked(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM blocked_users WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    # =========================================================
# PLUS
# =========================================================
def has_plus(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT expires_at FROM plus_subscriptions WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    if not r or not r[0]:
        return False
    try:
        return datetime.fromisoformat(r[0]) > datetime.now()
    except Exception:
        return False


def get_plus_time_left(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT expires_at FROM plus_subscriptions WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    if not r or not r[0]:
        return 0
    try:
        return max(0, int((datetime.fromisoformat(r[0]) - datetime.now()).total_seconds()))
    except Exception:
        return 0


def buy_plus(user_id, price_paid=None):
    if price_paid is None:
        price_paid = get_plus_price()
    days = get_plus_days()
    now = datetime.now()
    conn = db()
    c = conn.cursor()
    c.execute("SELECT expires_at, custom_cat_used FROM plus_subscriptions WHERE user_id=?",
              (user_id,))
    r = c.fetchone()
    if r and r[0]:
        try:
            current_exp = datetime.fromisoformat(r[0])
            base = current_exp if current_exp > now else now
        except Exception:
            base = now
        custom_used = r[1] or 0
    else:
        base = now
        custom_used = 0
    new_exp = base + timedelta(days=days)
    c.execute("""INSERT OR REPLACE INTO plus_subscriptions
                 (user_id, expires_at, bought_at, price_paid, custom_cat_used)
                 VALUES (?,?,?,?,?)""",
              (user_id, new_exp.isoformat(), now.isoformat(), price_paid, custom_used))
    conn.commit()
    conn.close()
    return new_exp


def get_plus_sale_price(base_price, user_id):
    if not has_plus(user_id):
        return base_price
    mult = get_plus_discount_mult()
    if mult <= 1:
        return base_price
    return max(1, base_price // mult)


def get_all_plus_users():
    now_iso = datetime.now().isoformat()
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT user_id, expires_at FROM plus_subscriptions
                 WHERE expires_at > ? ORDER BY expires_at DESC""", (now_iso,))
    rows = c.fetchall()
    conn.close()
    return rows


def admin_grant_plus(user_id, days):
    now = datetime.now()
    conn = db()
    c = conn.cursor()
    c.execute("SELECT expires_at, custom_cat_used FROM plus_subscriptions WHERE user_id=?",
              (user_id,))
    r = c.fetchone()
    if r and r[0]:
        try:
            current_exp = datetime.fromisoformat(r[0])
            base = current_exp if current_exp > now else now
        except Exception:
            base = now
        custom_used = r[1] or 0
    else:
        base = now
        custom_used = 0
    new_exp = base + timedelta(days=days)
    c.execute("""INSERT OR REPLACE INTO plus_subscriptions
                 (user_id, expires_at, bought_at, price_paid, custom_cat_used)
                 VALUES (?,?,?,?,?)""",
              (user_id, new_exp.isoformat(), now.isoformat(), 0, custom_used))
    conn.commit()
    conn.close()
    return new_exp


def admin_revoke_plus(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM plus_subscriptions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


# =========================================================
# ALLOW
# =========================================================
def has_allow(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT expires_at FROM allow_subscriptions WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    if not r or not r[0]:
        return False
    try:
        return datetime.fromisoformat(r[0]) > datetime.now()
    except Exception:
        return False


def get_allow_time_left(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT expires_at FROM allow_subscriptions WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    if not r or not r[0]:
        return 0
    try:
        return max(0, int((datetime.fromisoformat(r[0]) - datetime.now()).total_seconds()))
    except Exception:
        return 0


def buy_allow(user_id, price_paid=None):
    if price_paid is None:
        price_paid = get_allow_price()
    days = get_allow_days()
    now = datetime.now()
    conn = db()
    c = conn.cursor()
    c.execute("SELECT expires_at FROM allow_subscriptions WHERE user_id=?", (user_id,))
    r = c.fetchone()
    if r and r[0]:
        try:
            current_exp = datetime.fromisoformat(r[0])
            base = current_exp if current_exp > now else now
        except Exception:
            base = now
    else:
        base = now
    new_exp = base + timedelta(days=days)
    c.execute("""INSERT OR REPLACE INTO allow_subscriptions
                 (user_id, expires_at, bought_at, price_paid)
                 VALUES (?,?,?,?)""",
              (user_id, new_exp.isoformat(), now.isoformat(), price_paid))
    conn.commit()
    conn.close()
    return new_exp


def get_all_allow_users():
    now_iso = datetime.now().isoformat()
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT user_id, expires_at FROM allow_subscriptions
                 WHERE expires_at > ? ORDER BY expires_at DESC""", (now_iso,))
    rows = c.fetchall()
    conn.close()
    return rows


def admin_grant_allow(user_id, days):
    now = datetime.now()
    conn = db()
    c = conn.cursor()
    c.execute("SELECT expires_at FROM allow_subscriptions WHERE user_id=?", (user_id,))
    r = c.fetchone()
    if r and r[0]:
        try:
            current_exp = datetime.fromisoformat(r[0])
            base = current_exp if current_exp > now else now
        except Exception:
            base = now
    else:
        base = now
    new_exp = base + timedelta(days=days)
    c.execute("""INSERT OR REPLACE INTO allow_subscriptions
                 (user_id, expires_at, bought_at, price_paid)
                 VALUES (?,?,?,?)""",
              (user_id, new_exp.isoformat(), now.isoformat(), 0))
    conn.commit()
    conn.close()
    return new_exp


def admin_revoke_allow(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM allow_subscriptions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_user_privileges(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT expires_at FROM plus_subscriptions WHERE user_id=?", (user_id,))
    r1 = c.fetchone()
    c.execute("SELECT expires_at FROM allow_subscriptions WHERE user_id=?", (user_id,))
    r2 = c.fetchone()
    conn.close()
    plus_active, plus_exp = False, None
    if r1 and r1[0]:
        try:
            exp = datetime.fromisoformat(r1[0])
            if exp > datetime.now():
                plus_active, plus_exp = True, exp
        except Exception:
            pass
    allow_active, allow_exp = False, None
    if r2 and r2[0]:
        try:
            exp = datetime.fromisoformat(r2[0])
            if exp > datetime.now():
                allow_active, allow_exp = True, exp
        except Exception:
            pass
    return plus_active, plus_exp, allow_active, allow_exp


# =========================================================
# ПРОМОКОДЫ (баланс) — возвращают uses/max_uses
# =========================================================
def create_promo(code, reward, max_uses, hours):
    code = code.strip()
    expires = ""
    if hours > 0:
        expires = (datetime.now() + timedelta(hours=hours)).isoformat()
    conn = db()
    c = conn.cursor()
    try:
        c.execute("""INSERT INTO promocodes (code, reward, max_uses, uses, expires_at, created_at)
                     VALUES (?,?,?,0,?,?)""",
                  (code, reward, max_uses, expires, datetime.now().isoformat()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def get_all_promos():
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, code, reward, max_uses, uses, expires_at
                 FROM promocodes ORDER BY id DESC""")
    rows = c.fetchall()
    conn.close()
    return rows


def get_active_promos():
    now_iso = datetime.now().isoformat()
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, code, reward, max_uses, uses, expires_at
                 FROM promocodes
                 WHERE (max_uses = 0 OR uses < max_uses)
                   AND (expires_at = '' OR expires_at > ?)
                 ORDER BY id DESC""", (now_iso,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_archived_promos():
    now_iso = datetime.now().isoformat()
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, code, reward, max_uses, uses, expires_at
                 FROM promocodes
                 WHERE (max_uses > 0 AND uses >= max_uses)
                    OR (expires_at != '' AND expires_at <= ?)
                 ORDER BY id DESC""", (now_iso,))
    rows = c.fetchall()
    conn.close()
    return rows


def clear_archived_promos():
    now_iso = datetime.now().isoformat()
    conn = db()
    c = conn.cursor()
    c.execute("""DELETE FROM promocodes
                 WHERE (max_uses > 0 AND uses >= max_uses)
                    OR (expires_at != '' AND expires_at <= ?)""", (now_iso,))
    count = c.rowcount
    conn.commit()
    conn.close()
    return count


def delete_promo(promo_id):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM promocodes WHERE id=?", (promo_id,))
    conn.commit()
    conn.close()


def activate_promo(user_id, code):
    """Возвращает (ok, msg, reward, uses, max_uses)."""
    conn = db()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("""SELECT reward, max_uses, uses, expires_at
                     FROM promocodes WHERE code=?""", (code,))
        row = c.fetchone()
        if not row:
            conn.rollback()
            return False, "❌ Промокод не найден", 0, 0, 0
        reward, max_uses, uses, expires_at = row
        if expires_at:
            try:
                if datetime.now() > datetime.fromisoformat(expires_at):
                    conn.rollback()
                    return False, "⏰ Промокод истёк", 0, uses, max_uses
            except Exception:
                pass
        c.execute("""SELECT 1 FROM promo_activations
                     WHERE user_id=? AND code=?""", (user_id, code))
        if c.fetchone():
            conn.rollback()
            return False, "⚠️ Ты уже активировал этот промокод", 0, uses, max_uses
        c.execute("""UPDATE promocodes SET uses = uses + 1
                     WHERE code=? AND (max_uses = 0 OR uses < max_uses)""",
                  (code,))
        if c.rowcount == 0:
            conn.rollback()
            return False, "❌ Промокод исчерпан", 0, uses, max_uses
        c.execute("""INSERT INTO promo_activations
                     (user_id, code, activated_at) VALUES (?,?,?)""",
                  (user_id, code, datetime.now().isoformat()))
        conn.commit()
        add_balance(user_id, reward, f"🎟 Промокод {code}")
        return True, f"✅ Промокод активирован! +{reward}✈️", reward, uses + 1, max_uses
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.error(f"activate_promo: {e}")
        return False, "⚠️ Ошибка активации", 0, 0, 0
    finally:
        conn.close()


# =========================================================
# ПРОМОКОДЫ НА ПОДПИСКИ (PLUS/ALLOW)
# =========================================================
def create_promo_priv(code, priv_type, days, max_uses, hours):
    code = code.strip()
    expires = ""
    if hours > 0:
        expires = (datetime.now() + timedelta(hours=hours)).isoformat()
    conn = db()
    c = conn.cursor()
    try:
        c.execute("""INSERT INTO promo_privs
                     (code, priv_type, days, max_uses, uses, expires_at, created_at)
                     VALUES (?,?,?,?,0,?,?)""",
                  (code, priv_type, days, max_uses, expires,
                   datetime.now().isoformat()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def get_all_promo_privs():
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, code, priv_type, days, max_uses, uses, expires_at
                 FROM promo_privs ORDER BY id DESC""")
    rows = c.fetchall()
    conn.close()
    return rows


def get_active_promo_privs():
    now_iso = datetime.now().isoformat()
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, code, priv_type, days, max_uses, uses, expires_at
                 FROM promo_privs
                 WHERE (max_uses = 0 OR uses < max_uses)
                   AND (expires_at = '' OR expires_at > ?)
                 ORDER BY id DESC""", (now_iso,))
    rows = c.fetchall()
    conn.close()
    return rows


def delete_promo_priv(pid):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM promo_privs WHERE id=?", (pid,))
    conn.commit()
    conn.close()


def activate_promo_priv(user_id, code):
    """Возвращает (ok, msg, priv, uses, max_uses)."""
    conn = db()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("""SELECT priv_type, days, max_uses, uses, expires_at
                     FROM promo_privs WHERE code=?""", (code,))
        row = c.fetchone()
        if not row:
            conn.rollback()
            return False, "❌ Промокод не найден", None, 0, 0
        priv_type, days, max_uses, uses, expires_at = row
        if expires_at:
            try:
                if datetime.now() > datetime.fromisoformat(expires_at):
                    conn.rollback()
                    return False, "⏰ Промокод истёк", None, uses, max_uses
            except Exception:
                pass
        c.execute("""SELECT 1 FROM promo_priv_activations
                     WHERE user_id=? AND code=?""", (user_id, code))
        if c.fetchone():
            conn.rollback()
            return False, "⚠️ Ты уже активировал этот промокод", None, uses, max_uses
        c.execute("""UPDATE promo_privs SET uses = uses + 1
                     WHERE code=? AND (max_uses = 0 OR uses < max_uses)""",
                  (code,))
        if c.rowcount == 0:
            conn.rollback()
            return False, "❌ Промокод исчерпан", None, uses, max_uses
        c.execute("""INSERT INTO promo_priv_activations
                     (user_id, code, activated_at) VALUES (?,?,?)""",
                  (user_id, code, datetime.now().isoformat()))
        conn.commit()
        return True, f"✅ Промокод активирован! {priv_type.upper()} +{days}д", (priv_type, days), uses + 1, max_uses
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.error(f"activate_promo_priv: {e}")
        return False, "⚠️ Ошибка активации", None, 0, 0
    finally:
        conn.close()


# =========================================================
# ПРИЧИНЫ БАЛАНСА
# =========================================================
def get_balance_reasons():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id, reason FROM balance_reasons ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows


def get_balance_reason(rid):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id, reason FROM balance_reasons WHERE id=?", (rid,))
    r = c.fetchone()
    conn.close()
    return r


def add_balance_reason(reason):
    reason = reason.strip()
    if not reason:
        return False
    conn = db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO balance_reasons (reason, created_at) VALUES (?,?)",
                  (reason, datetime.now().isoformat()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def delete_balance_reason(rid):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM balance_reasons WHERE id=?", (rid,))
    conn.commit()
    conn.close()


# =========================================================
# CRYPTO PAY (БД)
# =========================================================
def create_crypto_topup(user_id, invoice_id, amount_pt, amount_usdt):
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT INTO crypto_topups
                 (user_id, invoice_id, amount_pt, amount_usdt, created_at)
                 VALUES (?,?,?,?,?)""",
              (user_id, str(invoice_id), amount_pt, amount_usdt,
               datetime.now().isoformat()))
    tid = c.lastrowid
    conn.commit()
    conn.close()
    return tid


def get_crypto_topup(invoice_id):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, user_id, amount_pt, status
                 FROM crypto_topups WHERE invoice_id=?""", (str(invoice_id),))
    r = c.fetchone()
    conn.close()
    return r


def mark_crypto_topup_paid(invoice_id):
    conn = db()
    c = conn.cursor()
    c.execute("""UPDATE crypto_topups SET status='paid', paid_at=?
                 WHERE invoice_id=? AND status='pending'""",
              (datetime.now().isoformat(), str(invoice_id)))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


# =========================================================
# FSM
# =========================================================
class AdminStates(StatesGroup):
    cat_name = State()
    cat_emoji = State()
    cat_paid = State()
    cat_channel = State()
    cat_cover = State()
    edit_cat_value = State()
    edit_cat_cover = State()
    cat_import_start = State()
    cat_import_limit = State()
    ban_id = State()
    unban_id = State()
    del_user_id = State()
    stat_user_id = State()
    priv_user_id = State()
    priv_plus_days = State()
    priv_allow_days = State()
    dm_user_id = State()
    dm_message = State()
    bal_simple_input = State()
    bal_reason_add = State()
    shop_name = State()
    shop_desc = State()
    shop_price = State()
    shop_type = State()
    shop_content = State()
    shop_edit_value = State()
    task_name = State()
    task_desc = State()
    task_emoji = State()
    task_reward = State()
    task_media_type = State()
    task_media_count = State()
    task_repeatable = State()
    bonus_hourly = State()
    bonus_daily = State()
    top_reward_amount = State()
    ref_bonus = State()
    promo_code = State()
    promo_reward = State()
    promo_uses = State()
    promo_hours = State()
    promo_priv_code = State()
    promo_priv_type = State()
    promo_priv_days = State()
    promo_priv_uses = State()
    promo_priv_hours = State()
    plus_price = State()
    allow_price = State()
    bcast_sig_text = State()
    waiting_broadcast = State()
    autodel_delay = State()


class SupportStates(StatesGroup):
    waiting_message = State()


# ИЗМЕНЕНО: добавлен report_custom
class UserStates(StatesGroup):
    suggest_video = State()
    waiting_promo = State()
    buy_pt_amount = State()
    manual_topup_amount = State()
    shop_photo = State()
    report_custom = State()


class TaskStates(StatesGroup):
    sending_files = State()


# =========================================================
# CRYPTO PAY — API
# =========================================================
async def cp_create_invoice(user_id, amount_pt, amount_usdt):
    if not cp:
        return None
    try:
        invoice = await cp.create_invoice(
            amount=amount_usdt,
            asset="USDT",
            description=f"Пополнение {amount_pt}✈️",
            payload=f"topup_{user_id}_{amount_pt}",
            expires_in=3600)
        return invoice
    except Exception as e:
        log.error(f"CryptoPay create: {e}")
        return None


async def cp_get_invoice(invoice_id):
    if not cp:
        return None
    try:
        invoices = await cp.get_invoices(invoice_ids=str(invoice_id))
        if invoices:
            return invoices[0]
        return None
    except Exception as e:
        log.error(f"CryptoPay get: {e}")
        return None
        # =========================================================
# РЕФЕРАЛКА
# =========================================================
def save_referral(user_id, inviter_id):
    if user_id == inviter_id:
        return False
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM referrals WHERE user_id=?", (user_id,))
    if c.fetchone():
        conn.close()
        return False
    try:
        c.execute("INSERT INTO referrals (user_id, inviter_id, created_at) VALUES (?,?,?)",
                  (user_id, inviter_id, datetime.now().isoformat()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def get_invited_count(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id=?", (user_id,))
    n = c.fetchone()[0]
    conn.close()
    return n


# =========================================================
# ЮЗЕРЫ
# =========================================================
def register_user(user):
    conn = db()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,))
    if c.fetchone():
        c.execute("UPDATE users SET username=?, first_name=?, last_seen=? WHERE user_id=?",
                  (user.username, user.first_name, now, user.id))
    else:
        c.execute("""INSERT INTO users (user_id, username, first_name,
                     joined_at, last_seen, balance, activated)
                     VALUES (?,?,?,?,?,?,1)""",
                  (user.id, user.username, user.first_name, now, now, START_BALANCE))
    conn.commit()
    conn.close()


def activate_user(user):
    conn = db()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,))
    if c.fetchone():
        c.execute("UPDATE users SET activated=1, last_seen=? WHERE user_id=?",
                  (now, user.id))
    else:
        c.execute("""INSERT INTO users (user_id, username, first_name,
                     joined_at, last_seen, balance, activated)
                     VALUES (?,?,?,?,?,?,1)""",
                  (user.id, user.username, user.first_name, now, now, START_BALANCE))
    conn.commit()
    conn.close()


def is_activated(user_id):
    if user_id == ADMIN_ID:
        return True
    conn = db()
    c = conn.cursor()
    c.execute("SELECT activated FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    return bool(r and r[0])


def accept_agreement(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET agreement_accepted=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def is_agreement_accepted(user_id):
    if user_id == ADMIN_ID:
        return True
    conn = db()
    c = conn.cursor()
    c.execute("SELECT agreement_accepted FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    return bool(r and r[0])


def is_banned(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT banned FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    return bool(r and r[0])


def set_banned(user_id, banned):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if banned else 0, user_id))
    conn.commit()
    conn.close()


def get_all_user_ids():
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT u.user_id FROM users u
                 WHERE u.banned=0 AND u.activated=1
                   AND u.user_id NOT IN (SELECT user_id FROM blocked_users)""")
    ids = [r[0] for r in c.fetchall()]
    conn.close()
    return ids


def get_users_count():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE activated=1")
    n = c.fetchone()[0]
    conn.close()
    return n


def get_active_users(days=1):
    conn = db()
    c = conn.cursor()
    since = (datetime.now() - timedelta(days=days)).isoformat()
    c.execute("SELECT COUNT(*) FROM users WHERE last_seen >= ? AND activated=1", (since,))
    n = c.fetchone()[0]
    conn.close()
    return n


def get_user_stats(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT joined_at, downloads, banned, balance FROM users WHERE user_id=?",
              (user_id,))
    r = c.fetchone()
    conn.close()
    return r


def get_users_page(page=0, per_page=10):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT user_id, username, first_name, balance, banned
                 FROM users WHERE activated=1 ORDER BY last_seen DESC LIMIT ? OFFSET ?""",
              (per_page, page * per_page))
    rows = c.fetchall()
    c.execute("SELECT COUNT(*) FROM users WHERE activated=1")
    total = c.fetchone()[0]
    conn.close()
    return rows, total


def get_top_users_by_balance(limit=10):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT user_id, username, first_name, balance
                 FROM users WHERE activated=1 AND banned=0
                 ORDER BY balance DESC LIMIT ?""", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_user_full_stats(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT user_id, username, first_name, joined_at, last_seen,
                 banned, activated, balance, downloads, agreement_accepted
                 FROM users WHERE user_id=?""", (user_id,))
    u = c.fetchone()
    if not u:
        conn.close()
        return None
    c.execute("SELECT expires_at FROM plus_subscriptions WHERE user_id=?", (user_id,))
    r_plus = c.fetchone()
    c.execute("SELECT expires_at FROM allow_subscriptions WHERE user_id=?", (user_id,))
    r_allow = c.fetchone()
    c.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id=?", (user_id,))
    ref_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM referrals WHERE user_id=?", (user_id,))
    was_invited = c.fetchone()[0]
    c.execute("SELECT COUNT(*), COALESCE(SUM(price_paid),0) FROM media_purchases WHERE user_id=?",
              (user_id,))
    media_bought, spent_media = c.fetchone()
    c.execute("SELECT COUNT(*), COALESCE(SUM(price_paid),0) FROM cat_purchases WHERE user_id=?",
              (user_id,))
    cat_bought, spent_cat = c.fetchone()
    c.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM shop_purchases WHERE user_id=?",
              (user_id,))
    shop_bought, spent_shop = c.fetchone()
    c.execute("SELECT COUNT(*) FROM downloads_log WHERE user_id=?", (user_id,))
    total_views = c.fetchone()[0]
    c.execute("""SELECT COUNT(*) FROM downloads_log
                 WHERE user_id=? AND ts >= ?""",
              (user_id, get_week_start().isoformat()))
    week_views = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM promo_activations WHERE user_id=?", (user_id,))
    promos_used = c.fetchone()[0]
    c.execute("""SELECT COUNT(*) FROM task_completions
                 WHERE user_id=? AND status='approved'""", (user_id,))
    tasks_done = c.fetchone()[0]
    conn.close()
    return {
        "user_id": u[0], "username": u[1], "first_name": u[2],
        "joined_at": u[3], "last_seen": u[4], "banned": u[5],
        "activated": u[6], "balance": u[7], "downloads": u[8],
        "agreement": u[9],
        "plus_expires": r_plus[0] if r_plus else None,
        "allow_expires": r_allow[0] if r_allow else None,
        "ref_count": ref_count, "was_invited": was_invited,
        "media_bought": media_bought, "spent_media": spent_media,
        "cat_bought": cat_bought, "spent_cat": spent_cat,
        "shop_bought": shop_bought, "spent_shop": spent_shop,
        "total_views": total_views, "week_views": week_views,
        "promos_used": promos_used, "tasks_done": tasks_done,
    }


# ИЗМЕНЕНО: добавлена функция get_user_total_views
def get_user_total_views(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM downloads_log WHERE user_id=?", (user_id,))
    n = c.fetchone()[0]
    conn.close()
    return n


# =========================================================
# ТОП НЕДЕЛИ
# =========================================================
def get_week_start():
    val = get_setting("week_start", "")
    if val:
        try:
            return datetime.fromisoformat(val)
        except Exception:
            pass
    default = datetime.now() - timedelta(days=7)
    set_setting("week_start", default.isoformat())
    return default


def reset_week_start():
    set_setting("week_start", datetime.now().isoformat())


def get_top_week(limit=3):
    since = get_week_start().isoformat()
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT u.user_id, u.username, u.first_name, u.banned,
                 COUNT(d.id) AS cnt
                 FROM downloads_log d JOIN users u ON u.user_id = d.user_id
                 WHERE d.ts >= ? AND u.user_id != ?
                 GROUP BY u.user_id ORDER BY cnt DESC LIMIT ?""",
              (since, ADMIN_ID, limit))
    rows = c.fetchall()
    conn.close()
    return rows


def get_user_week_views(user_id):
    if user_id == ADMIN_ID:
        return 0
    since = get_week_start().isoformat()
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM downloads_log WHERE user_id=? AND ts >= ?",
              (user_id, since))
    n = c.fetchone()[0]
    conn.close()
    return n


def get_user_week_position(user_id):
    if user_id == ADMIN_ID:
        return None
    since = get_week_start().isoformat()
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT user_id, COUNT(d.id) FROM downloads_log d
                 WHERE d.ts >= ? AND user_id != ?
                 GROUP BY user_id ORDER BY COUNT(d.id) DESC""",
              (since, ADMIN_ID))
    rows = c.fetchall()
    conn.close()
    for i, (uid, _) in enumerate(rows, 1):
        if uid == user_id:
            return i
    return None


async def finish_week():
    top = get_top_week(3)
    if not top:
        reset_week_start()
        return True, "📭 Топ пуст — наград нет.\n\n🔄 Неделя сброшена."
    rewards = {1: get_top_reward(1), 2: get_top_reward(2), 3: get_top_reward(3)}
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    total_paid = 0
    winners = []
    for i, (uid, uname, fname, banned, cnt) in enumerate(top, 1):
        if i > 3:
            break
        reward = rewards.get(i, 0)
        if reward <= 0:
            continue
        new_bal = add_balance(uid, reward, f"🏆 Топ недели #{i}")
        total_paid += reward
        winners.append((uid, fname, reward, new_bal, cnt))
        lines.append(
            f"{medals[i-1]} <b>{fname or 'Без имени'}</b> — "
            f"{cnt} просмотров → <b>+{reward}✈️</b>")
    for uid, fname, reward, new_bal, cnt in winners:
        try:
            await bot.send_message(
                uid,
                f"🏆 <b>Топ недели завершён!</b>\n\n"
                f"Ты занял призовое место с <b>{cnt}</b> просмотрами!\n"
                f"💰 Награда: <b>+{reward}✈️</b>\n"
                f"💳 Баланс: <b>{new_bal}✈️</b>\n\n"
                f"<i>Новая неделя уже началась — смотри видео и зарабатывай!</i>",
                parse_mode="HTML")
        except Exception:
            pass
    reset_week_start()
    text = "🏁 <b>Неделя завершена!</b>\n\n"
    if lines:
        text += "\n".join(lines)
        text += f"\n\n💰 Выдано: <b>{total_paid}✈️</b>"
    else:
        text += "Наград не выдано (нет просмотров или награды = 0)."
    text += "\n\n🔄 <b>Новый топ начат с 0</b>"
    return True, text


# =========================================================
# СТАТИСТИКА (с фиксом сброса)
# =========================================================
def get_stats_period(period="all"):
    if period == "day":
        since = (datetime.now() - timedelta(days=1)).isoformat()
    elif period == "week":
        since = (datetime.now() - timedelta(days=7)).isoformat()
    else:
        since = "1970-01-01T00:00:00"

    reset_at = get_setting(f"stats_reset_{period}", "")
    if reset_at and reset_at > since:
        since = reset_at

    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE joined_at >= ? AND activated=1", (since,))
    users_new = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE activated=1")
    users_total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE last_seen >= ? AND activated=1", (since,))
    users_active = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM media_purchases WHERE bought_at >= ?", (since,))
    media_bought = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM cat_purchases WHERE bought_at >= ?", (since,))
    cat_bought = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(price_paid), 0) FROM media_purchases WHERE bought_at >= ?",
              (since,))
    spent_media = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(price_paid), 0) FROM cat_purchases WHERE bought_at >= ?",
              (since,))
    spent_cat = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM downloads_log WHERE ts >= ?", (since,))
    views = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM shop_purchases WHERE bought_at >= ?", (since,))
    shop_bought = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(price), 0) FROM shop_purchases WHERE bought_at >= ?",
              (since,))
    spent_shop = c.fetchone()[0]
    conn.close()
    return {
        "users_new": users_new, "users_total": users_total,
        "users_active": users_active, "media_bought": media_bought,
        "cat_bought": cat_bought, "shop_bought": shop_bought,
        "total_bought": media_bought + cat_bought,
        "spent_media": spent_media, "spent_cat": spent_cat,
        "spent_shop": spent_shop, "total_spent": spent_media + spent_cat + spent_shop,
        "views": views,
    }


def get_categories_ranking(period="all", limit=3):
    if period == "day":
        since = (datetime.now() - timedelta(days=1)).isoformat()
    elif period == "week":
        since = (datetime.now() - timedelta(days=7)).isoformat()
    else:
        since = "1970-01-01T00:00:00"
    reset_at = get_setting(f"stats_reset_{period}", "")
    if reset_at and reset_at > since:
        since = reset_at
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT cat.id, cat.name, cat.emoji, COUNT(d.id) AS cnt
                 FROM categories cat
                 LEFT JOIN media m ON m.category_id = cat.id
                 LEFT JOIN downloads_log d ON d.media_id = m.id AND d.ts >= ?
                 GROUP BY cat.id
                 ORDER BY cnt DESC""", (since,))
    rows = c.fetchall()
    conn.close()
    top = rows[:limit]
    top_ids = {r[0] for r in top}
    rest = [r for r in rows if r[0] not in top_ids]
    bottom = sorted(rest, key=lambda r: r[3])[:limit] if rest else []
    return top, bottom


def reset_stats_period(period):
    if period == "all":
        return False, "♾ За всё время нельзя сбросить"
    if period == "day":
        since = (datetime.now() - timedelta(days=1)).isoformat()
        label = "за сутки"
    elif period == "week":
        since = (datetime.now() - timedelta(days=7)).isoformat()
        label = "за неделю"
        reset_week_start()
    else:
        return False, "❌ Неизвестный период"

    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM downloads_log WHERE ts >= ?", (since,))
    views_deleted = c.rowcount
    c.execute("DELETE FROM media_purchases WHERE bought_at >= ?", (since,))
    media_deleted = c.rowcount
    c.execute("DELETE FROM cat_purchases WHERE bought_at >= ?", (since,))
    cat_deleted = c.rowcount
    c.execute("DELETE FROM shop_purchases WHERE bought_at >= ?", (since,))
    shop_deleted = c.rowcount
    c.execute("DELETE FROM referrals WHERE created_at >= ?", (since,))
    refs_deleted = c.rowcount
    c.execute("DELETE FROM promo_activations WHERE activated_at >= ?", (since,))
    promos_deleted = c.rowcount
    c.execute("DELETE FROM task_completions WHERE created_at >= ?", (since,))
    tasks_deleted = c.rowcount
    conn.commit()
    conn.close()

    reset_ts = datetime.now().isoformat()
    set_setting(f"stats_reset_{period}", reset_ts)
    log.info(f"🧹 reset_stats_period: period={period}, reset_at={reset_ts}")

    return True, (
        f"✅ <b>Статистика {label} сброшена В 0</b>\n\n"
        f"📥 Просмотры: -{views_deleted}\n"
        f"🎬 Покупки видео: -{media_deleted}\n"
        f"📂 Покупки категорий: -{cat_deleted}\n"
        f"🏪 Покупки магазина: -{shop_deleted}\n"
        f"👥 Рефералы: -{refs_deleted}\n"
        f"🎟 Промокоды: -{promos_deleted}\n"
        f"📝 Задания: -{tasks_deleted}\n\n"
        f"<i>Сброс с: {reset_ts[:16].replace('T', ' ')}</i>"
    )
    # =========================================================
# КАТЕГОРИИ / МЕДИА
# =========================================================
def get_categories():
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT c.id, c.name, c.emoji, c.paid_price, COUNT(m.id),
                 c.channel_id, c.cover
                 FROM categories c LEFT JOIN media m ON m.category_id = c.id
                 GROUP BY c.id ORDER BY c.name""")
    rows = c.fetchall()
    conn.close()
    return rows


def get_category(cat_id):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, name, emoji, paid_price, channel_id, cover
                 FROM categories WHERE id=?""", (cat_id,))
    r = c.fetchone()
    conn.close()
    return r


def get_category_by_channel(channel_id):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, name, emoji, paid_price, channel_id, cover
                 FROM categories WHERE channel_id=?""", (channel_id,))
    r = c.fetchone()
    conn.close()
    return r


def create_category(name, emoji="📁", paid_price=DEFAULT_PAID_PRICE,
                    channel_id=0, cover=""):
    conn = db()
    c = conn.cursor()
    try:
        c.execute("""INSERT INTO categories
                     (name, emoji, paid_price, channel_id, cover, created_at)
                     VALUES (?,?,?,?,?,?)""",
                  (name, emoji, paid_price, channel_id, cover,
                   datetime.now().isoformat()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def update_category(cat_id, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=?" for k in kwargs.keys())
    values = list(kwargs.values()) + [cat_id]
    conn = db()
    c = conn.cursor()
    c.execute(f"UPDATE categories SET {fields} WHERE id=?", values)
    conn.commit()
    conn.close()


def delete_category(cat_id):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    conn.commit()
    conn.close()
    invalidate_media_cache(cat_id)


def is_category_fully_bought(user_id, cat_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM cat_purchases WHERE user_id=? AND category_id=?",
              (user_id, cat_id))
    r = c.fetchone()
    conn.close()
    return r is not None


def is_media_bought(user_id, media_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM media_purchases WHERE user_id=? AND media_id=?",
              (user_id, media_id))
    r = c.fetchone()
    conn.close()
    return r is not None


def is_media_unlocked(user_id, cat_id, media_id, position):
    if has_allow(user_id):
        return True
    return is_category_fully_bought(user_id, cat_id) or is_media_bought(user_id, media_id)


def buy_media(user_id, media_id, price_paid=0):
    conn = db()
    c = conn.cursor()
    try:
        c.execute("""INSERT INTO media_purchases (user_id, media_id, bought_at, price_paid)
                     VALUES (?,?,?,?)""",
                  (user_id, media_id, datetime.now().isoformat(), price_paid))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def buy_full_category(user_id, cat_id, price_paid=0):
    conn = db()
    c = conn.cursor()
    try:
        c.execute("""INSERT INTO cat_purchases (user_id, category_id, bought_at, price_paid)
                     VALUES (?,?,?,?)""",
                  (user_id, cat_id, datetime.now().isoformat(), price_paid))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def invalidate_media_cache(cat_id):
    global _media_order_cache
    _media_order_cache.pop(cat_id, None)


def get_media_in_category(cat_id):
    global _media_order_cache
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, channel_id, channel_msg_id, file_id, file_type,
                 caption, views
                 FROM media WHERE category_id=? ORDER BY id""", (cat_id,))
    all_rows = c.fetchall()
    conn.close()
    if not all_rows:
        return []
    current_ids = [r[0] for r in all_rows]
    cached_ids = _media_order_cache.get(cat_id)
    if cached_ids and set(cached_ids) == set(current_ids):
        by_id = {r[0]: r for r in all_rows}
        return [by_id[i] for i in cached_ids]
    random.shuffle(all_rows)
    _media_order_cache[cat_id] = [r[0] for r in all_rows]
    return all_rows


def get_media_in_category_ordered(cat_id):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, channel_id, channel_msg_id, file_id, file_type,
                 caption, views
                 FROM media WHERE category_id=? ORDER BY id""", (cat_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_media_by_id(mid):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, category_id, channel_id, channel_msg_id, file_id,
                 file_type, caption FROM media WHERE id=?""", (mid,))
    r = c.fetchone()
    conn.close()
    return r


def media_exists_in_category(cat_id, channel_id, channel_msg_id):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT 1 FROM media
                 WHERE category_id=? AND channel_id=? AND channel_msg_id=?""",
              (cat_id, channel_id, channel_msg_id))
    r = c.fetchone()
    conn.close()
    return r is not None


def add_media(cat_id, channel_id, channel_msg_id, file_type, caption="", file_id=None):
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT INTO media (category_id, channel_id, channel_msg_id,
                 file_id, file_type, caption, added_at)
                 VALUES (?,?,?,?,?,?,?)""",
              (cat_id, channel_id, channel_msg_id, file_id, file_type, caption,
               datetime.now().isoformat()))
    mid = c.lastrowid
    conn.commit()
    conn.close()
    invalidate_media_cache(cat_id)
    return mid


def delete_media(mid):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT category_id FROM media WHERE id=?", (mid,))
    r = c.fetchone()
    c.execute("DELETE FROM media WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    if r:
        invalidate_media_cache(r[0])


def increment_views(mid):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE media SET views = views + 1 WHERE id=?", (mid,))
    conn.commit()
    conn.close()


def log_download(user_id, media_id):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO downloads_log (user_id, media_id, ts) VALUES (?,?,?)",
              (user_id, media_id, datetime.now().isoformat()))
    c.execute("UPDATE users SET downloads = downloads + 1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


# =========================================================
# ИМПОРТ ИЗ КАНАЛА
# =========================================================
async def import_last_channel_messages(bot_instance, cat_id, channel_id,
                                        limit=100, offset_id=0):
    added, skipped, errors = 0, 0, 0
    msg_id = max(1, offset_id)
    checked = 0
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 30
    while checked < limit and consecutive_errors < MAX_CONSECUTIVE_ERRORS:
        try:
            sent = await bot_instance.forward_message(
                chat_id=ADMIN_ID, from_chat_id=channel_id, message_id=msg_id)
            try:
                await bot_instance.delete_message(ADMIN_ID, sent.message_id)
            except Exception:
                pass
            has_video = sent.video is not None
            has_photo = sent.photo is not None
            if not (has_video or has_photo):
                consecutive_errors = 0
                msg_id += 1
                continue
            if media_exists_in_category(cat_id, channel_id, msg_id):
                skipped += 1
                consecutive_errors = 0
                msg_id += 1
                continue
            file_type = "video" if has_video else "photo"
            file_id = sent.video.file_id if has_video else sent.photo[-1].file_id
            caption = sent.caption or ""
            add_media(cat_id, channel_id, msg_id, file_type,
                      caption=caption, file_id=file_id)
            added += 1
            checked += 1
            consecutive_errors = 0
            msg_id += 1
        except TelegramBadRequest:
            consecutive_errors += 1
            msg_id += 1
        except TelegramForbiddenError:
            errors += 1
            break
        except Exception as e:
            log.warning(f"import channel msg {msg_id}: {e}")
            errors += 1
            consecutive_errors += 1
            msg_id += 1
    return added, skipped, errors, msg_id


async def save_forwarded_from_channel(bot_instance, message: types.Message):
    origin = message.forward_origin or message.forward_from_chat
    channel_id = None
    if origin:
        if hasattr(origin, "chat") and origin.chat:
            channel_id = origin.chat.id
        elif hasattr(origin, "sender_chat") and origin.sender_chat:
            channel_id = origin.sender_chat.id
        elif hasattr(origin, "id"):
            channel_id = origin.id
    if not channel_id:
        return False, None, "no_channel"
    cat = get_category_by_channel(channel_id)
    if not cat:
        return False, None, "no_category"
    cat_id = cat[0]
    channel_msg_id = None
    if message.forward_origin and hasattr(message.forward_origin, "message_id"):
        channel_msg_id = message.forward_origin.message_id
    if not channel_msg_id:
        return False, cat_id, "no_msg_id"
    if media_exists_in_category(cat_id, channel_id, channel_msg_id):
        return False, cat_id, "exists"
    if message.video:
        file_type = "video"
        file_id = message.video.file_id
    elif message.photo:
        file_type = "photo"
        file_id = message.photo[-1].file_id
    else:
        return False, cat_id, "not_media"
    caption = message.caption or ""
    add_media(cat_id, channel_id, channel_msg_id, file_type,
              caption=caption, file_id=file_id)
    return True, cat_id, "added"


# =========================================================
# МАГАЗИН (БД)
# =========================================================
def get_shop_items():
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, name, description, price, content_type, content, requires_photo
                 FROM shop_items ORDER BY id""")
    rows = c.fetchall()
    conn.close()
    return rows


def get_shop_item(iid):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, name, description, price, content_type, content, requires_photo
                 FROM shop_items WHERE id=?""", (iid,))
    r = c.fetchone()
    conn.close()
    return r


def add_shop_item(name, description, price, content_type, content, requires_photo=0):
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT INTO shop_items
                 (name, description, price, content_type, content, requires_photo, created_at)
                 VALUES (?,?,?,?,?,?,?)""",
              (name, description, price, content_type, content, requires_photo,
               datetime.now().isoformat()))
    iid = c.lastrowid
    conn.commit()
    conn.close()
    return iid


def update_shop_item(iid, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=?" for k in kwargs.keys())
    values = list(kwargs.values()) + [iid]
    conn = db()
    c = conn.cursor()
    c.execute(f"UPDATE shop_items SET {fields} WHERE id=?", values)
    conn.commit()
    conn.close()


def delete_shop_item(iid):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM shop_items WHERE id=?", (iid,))
    conn.commit()
    conn.close()


def log_shop_purchase(user_id, item_id, price):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO shop_purchases (user_id, item_id, price, bought_at) VALUES (?,?,?,?)",
              (user_id, item_id, price, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def user_bought_item(user_id, item_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM shop_purchases WHERE user_id=? AND item_id=?",
              (user_id, item_id))
    r = c.fetchone()
    conn.close()
    return r is not None


# =========================================================
# ЗАДАНИЯ (БД)
# =========================================================
def get_tasks(active_only=True):
    conn = db()
    c = conn.cursor()
    if active_only:
        c.execute("SELECT id, name, description, emoji, reward, media_type, media_count, is_repeatable FROM tasks WHERE active=1 ORDER BY id")
    else:
        c.execute("SELECT id, name, description, emoji, reward, media_type, media_count, is_repeatable, active FROM tasks ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows


def get_task(task_id):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT id, name, description, emoji, reward, media_type,
                 media_count, is_repeatable, active FROM tasks WHERE id=?""", (task_id,))
    r = c.fetchone()
    conn.close()
    return r


def create_task(name, description, emoji, reward, media_type, media_count, is_repeatable=0):
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT INTO tasks (name, description, emoji, reward, media_type,
                 media_count, is_repeatable, active, created_at)
                 VALUES (?,?,?,?,?,?,?,1,?)""",
              (name, description, emoji, reward, media_type, media_count,
               is_repeatable, datetime.now().isoformat()))
    tid = c.lastrowid
    conn.commit()
    conn.close()
    return tid


def delete_task(task_id):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    c.execute("DELETE FROM task_progress WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()


def get_task_progress(user_id, task_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT file_ids, media_type FROM task_progress WHERE user_id=? AND task_id=?",
              (user_id, task_id))
    r = c.fetchone()
    conn.close()
    if r:
        return json.loads(r[0]), r[1]
    return [], None


def add_task_file(user_id, task_id, file_id, media_type):
    files, _ = get_task_progress(user_id, task_id)
    files.append(file_id)
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO task_progress
                 (user_id, task_id, file_ids, media_type, updated_at)
                 VALUES (?,?,?,?,?)""",
              (user_id, task_id, json.dumps(files), media_type,
               datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return len(files)


def clear_task_progress(user_id, task_id):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM task_progress WHERE user_id=? AND task_id=?",
              (user_id, task_id))
    conn.commit()
    conn.close()


def user_completed_task(user_id, task_id):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT status FROM task_completions
                 WHERE user_id=? AND task_id=?
                 ORDER BY id DESC LIMIT 1""", (user_id, task_id))
    r = c.fetchone()
    conn.close()
    return r[0] if r else None


def create_task_completion(user_id, task_id, admin_msg_id):
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT INTO task_completions (user_id, task_id, status, admin_msg_id, created_at)
                 VALUES (?,?,'pending',?,?)""",
              (user_id, task_id, admin_msg_id, datetime.now().isoformat()))
    cid = c.lastrowid
    conn.commit()
    conn.close()
    return cid


def update_task_completion_status(user_id, task_id, status):
    conn = db()
    c = conn.cursor()
    c.execute("""UPDATE task_completions SET status=?
                 WHERE user_id=? AND task_id=? AND status='pending'""",
              (status, user_id, task_id))
    conn.commit()
    conn.close()


def create_ticket(user_id, subject, message):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO support_tickets (user_id, subject, message, ts) VALUES (?,?,?,?)",
              (user_id, subject, message, datetime.now().isoformat()))
    tid = c.lastrowid
    conn.commit()
    conn.close()
    return tid


# =========================================================
# КВЕСТЫ
# =========================================================
def get_week_start_iso():
    now = datetime.now()
    monday = now - timedelta(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.isoformat()


def get_all_quests():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id, code, name, description, target, reward, emoji FROM quests_weekly ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows


def get_quest_progress(user_id, quest_id):
    week = get_week_start_iso()
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT progress, claimed FROM quest_progress
                 WHERE user_id=? AND quest_id=? AND week_start=?""",
              (user_id, quest_id, week))
    r = c.fetchone()
    conn.close()
    if r:
        return r[0], r[1]
    return 0, 0


def update_quest_progress(user_id, quest_id, value):
    week = get_week_start_iso()
    cur, claimed = get_quest_progress(user_id, quest_id)
    if value <= cur:
        return cur
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT INTO quest_progress (user_id, quest_id, progress, claimed, week_start)
                 VALUES (?,?,?,?,?)
                 ON CONFLICT(user_id, quest_id) DO UPDATE SET
                    progress=excluded.progress,
                    week_start=excluded.week_start""",
              (user_id, quest_id, value, claimed, week))
    conn.commit()
    conn.close()
    return value


def mark_quest_claimed(user_id, quest_id):
    week = get_week_start_iso()
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT INTO quest_progress (user_id, quest_id, progress, claimed, week_start)
                 VALUES (?,?,?,1,?)
                 ON CONFLICT(user_id, quest_id) DO UPDATE SET claimed=1""",
              (user_id, quest_id, 0, week))
    conn.commit()
    conn.close()


def get_quest_stats(user_id):
    quests = get_all_quests()
    result = []
    streak, _, _ = get_streak(user_id)
    week = get_week_start_iso()
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM downloads_log WHERE user_id=? AND ts >= ?",
              (user_id, week))
    views_week = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id=? AND created_at >= ?",
              (user_id, week))
    refs_week = c.fetchone()[0]
    conn.close()
    values = {"streak_7": streak, "views_30": views_week, "refs_5": refs_week}
    for qid, code, name, desc, target, reward, emoji in quests:
        val = values.get(code, 0)
        if val > target:
            val = target
        update_quest_progress(user_id, qid, val)
        progress, claimed = get_quest_progress(user_id, qid)
        result.append({
            "id": qid, "code": code, "name": name, "desc": desc,
            "target": target, "reward": reward, "emoji": emoji,
            "progress": progress, "claimed": claimed,
        })
    return result


# =========================================================
# «ЗАБРАТЬ ВСЁ»
# =========================================================
def claim_all_earn(user_id):
    result = {"hourly": 0, "daily": 0, "streak": 0, "streak_day": 0,
              "streak_plus": False, "quests": [], "total": 0}
    if can_take_hourly(user_id):
        base = get_hourly_reward()
        plus_b = get_plus_hourly_bonus() if has_plus(user_id) else 0
        reward = base + plus_b
        conn = db()
        c = conn.cursor()
        c.execute("UPDATE users SET last_accrual=? WHERE user_id=?",
                  (datetime.now().isoformat(), user_id))
        conn.commit()
        conn.close()
        add_balance(user_id, reward, "🕐 Часовой бонус")
        result["hourly"] = reward
    if can_take_daily(user_id):
        base, is_weekend = get_daily_bonus_with_multiplier()
        plus_b = get_plus_daily_bonus() if has_plus(user_id) else 0
        reward = base + plus_b
        conn = db()
        c = conn.cursor()
        c.execute("UPDATE users SET last_daily=? WHERE user_id=?",
                  (datetime.now().isoformat(), user_id))
        conn.commit()
        conn.close()
        add_balance(user_id, reward, "📅 Дневной бонус")
        result["daily"] = reward
    if can_claim_streak(user_id):
        ok, day, reward, plus_granted, _ = claim_streak(user_id)
        if ok:
            result["streak"] = reward
            result["streak_day"] = day
            result["streak_plus"] = plus_granted
    for q in get_quest_stats(user_id):
        if q["progress"] >= q["target"] and not q["claimed"]:
            mark_quest_claimed(user_id, q["id"])
            add_balance(user_id, q["reward"], f"🎯 Квест: {q['name']}")
            result["quests"].append((q["name"], q["reward"]))
            result["total"] += q["reward"]
    result["total"] += result["hourly"] + result["daily"] + result["streak"]
    return result


def user_can_claim_all(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT joined_at FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    if not r or not r[0]:
        return False
    try:
        joined = datetime.fromisoformat(r[0])
    except Exception:
        return False
    return (datetime.now() - joined).total_seconds() >= 7 * 86400


# =========================================================
# ПУШ «МАЛО ВАЛЮТЫ»
# =========================================================
def has_available_earn(user_id):
    if can_take_hourly(user_id):
        return True
    if can_take_daily(user_id):
        return True
    if can_claim_streak(user_id):
        return True
    for q in get_quest_stats(user_id):
        if q["progress"] >= q["target"] and not q["claimed"]:
            return True
    return False


def was_low_balance_notified(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM low_balance_notified WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    return r is not None


def mark_low_balance_notified(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO low_balance_notified (user_id, notified_at)
                 VALUES (?, ?)""",
              (user_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def user_ever_claimed_bonus(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT last_accrual, last_daily FROM users WHERE user_id=?""",
              (user_id,))
    r = c.fetchone()
    c.execute("SELECT total_claims FROM user_streaks WHERE user_id=?", (user_id,))
    st = c.fetchone()
    conn.close()
    if r and (r[0] or r[1]):
        return True
    if st and (st[0] or 0) > 0:
        return True
    return False


def user_is_new_less_than_day(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT joined_at FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    if not r or not r[0]:
        return False
    try:
        joined = datetime.fromisoformat(r[0])
    except Exception:
        return False
    return (datetime.now() - joined).total_seconds() < 86400


# =========================================================
# ПУШ «МАЛО ✈️» ЧЕРЕЗ 1 ЧАС
# =========================================================
def should_push_low_earn(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("""SELECT balance, last_balance_change FROM users WHERE user_id=?""",
              (user_id,))
    r = c.fetchone()
    if not r:
        conn.close()
        return False
    balance, last_change = r
    if balance >= 3:
        conn.close()
        return False
    if not last_change:
        conn.close()
        return False
    try:
        last = datetime.fromisoformat(last_change)
    except Exception:
        conn.close()
        return False
    if (datetime.now() - last).total_seconds() < 3600:
        conn.close()
        return False
    c.execute("SELECT last_push FROM low_earn_push WHERE user_id=?", (user_id,))
    p = c.fetchone()
    conn.close()
    if p and p[0]:
        try:
            if (datetime.now() - datetime.fromisoformat(p[0])).total_seconds() < 6 * 3600:
                return False
        except Exception:
            pass
    return has_available_earn(user_id)


def mark_low_earn_push(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO low_earn_push (user_id, last_push)
                 VALUES (?, ?)""",
              (user_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()


# =========================================================
# ЖАЛОБА НА ВИДЕО (ИЗМЕНЕНО: send_report_to_admin)
# =========================================================
async def send_report_to_admin(bot_instance, user, cat_id, cat_name, pos, total, media_id, reason_label, custom_text=None):
    try:
        username = f"@{user.username}" if user.username else "нет"
        full_name = user.full_name or "—"
        text = (
            f"⚠️ <b>Жалоба на видео</b>\n\n"
            f"👤 {full_name} ({username})\n"
            f"🆔 <code>{user.id}</code>\n\n"
            f"📂 Категория: <b>{cat_name}</b> (<code>{cat_id}</code>)\n"
            f"🎬 Видео: <b>#{pos}/{total}</b>\n"
            f"🆔 media_id: <code>{media_id}</code>\n"
            f"📋 Причина: <b>{reason_label}</b>\n"
        )
        if custom_text:
            text += f"💬 <i>{custom_text}</i>\n"
        text += f"🕒 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        await bot_instance.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception as e:
        log.error(f"send_report_to_admin: {e}")


# Старая функция оставлена для совместимости, но не используется
async def report_media_error(bot_instance, user, cat_id, cat_name, pos, total, media_id):
    await send_report_to_admin(bot_instance, user, cat_id, cat_name, pos, total, media_id,
                                "⚠️ Сообщить об ошибке")
                                # =========================================================
# КЛАВИАТУРЫ
# =========================================================
def main_kb(user_id):
    rows = [
        [KeyboardButton(text="📁 Каталог"), KeyboardButton(text="🏪 Магазин")],
        [KeyboardButton(text="💰 Заработать"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="🆘 Поддержка")],
    ]
    if user_id == ADMIN_ID:
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="a:stats:all"),
         InlineKeyboardButton(text="👥 Юзеры", callback_data="adm:users")],
        [InlineKeyboardButton(text="💰 Экономика", callback_data="adm:economy"),
         InlineKeyboardButton(text="📂 Контент", callback_data="adm:content")],
        [InlineKeyboardButton(text="💎 Подписки", callback_data="adm:subs"),
         InlineKeyboardButton(text="🎁 Акции", callback_data="adm:promo")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="a:broadcast_menu"),
         InlineKeyboardButton(text="🛠 Система", callback_data="adm:system")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="a:close")],
    ])


def task_sending_kb(task_id, progress, needed):
    rows = []
    if progress >= needed:
        rows.append([InlineKeyboardButton(
            text="✅ Отправить на проверку",
            callback_data=f"task:submit:{task_id}"
        )])
    rows.append([InlineKeyboardButton(
        text="❌ Отменить задание",
        callback_data=f"task:cancel:{task_id}"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ИЗМЕНЕНО: report -> report_menu
def watch_kb(cat_id, media_list, user_id, current_pos):
    cat = get_category(cat_id)
    if has_allow(user_id):
        paid_price = 0
    else:
        paid_price = get_plus_sale_price(cat[3], user_id)
    total = len(media_list)
    next_pos = current_pos + 1
    cur_mid = media_list[current_pos - 1][0] if 0 < current_pos <= total else 0

    common = [
        [InlineKeyboardButton(text="⚠️ Сообщить об ошибке",
                              callback_data=f"report_menu:{cat_id}:{current_pos}:{cur_mid}")],
        [InlineKeyboardButton(text="📂 К категории", callback_data=f"c:{cat_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="del_this")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
    ]

    if next_pos > total:
        return InlineKeyboardMarkup(inline_keyboard=common)

    next_mid = media_list[next_pos - 1][0]
    if is_media_unlocked(user_id, cat_id, next_mid, next_pos):
        rows = [[InlineKeyboardButton(text=f"⏭ Следующее ({next_pos}/{total})",
                                      callback_data=f"watch:{cat_id}:{next_pos}")]] + common
        return InlineKeyboardMarkup(inline_keyboard=rows)

    rows = [[InlineKeyboardButton(text=f"💳 Оплатить {paid_price}✈️ за #{next_pos}",
                                  callback_data=f"buymedia:{next_mid}:{cat_id}:{next_pos}")]] + common
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =========================================================
# ОТПРАВКА МЕДИА
# =========================================================
async def send_media_to_user(bot_instance, user_id, mid_row, header, kb):
    mid_id, ch_id, ch_msg_id, f_id, ftype, caption, views = mid_row
    if f_id:
        try:
            if ftype == "photo":
                return await bot_instance.send_photo(
                    chat_id=user_id, photo=f_id, caption=header,
                    protect_content=True, reply_markup=kb)
            else:
                return await bot_instance.send_video(
                    chat_id=user_id, video=f_id, caption=header,
                    protect_content=True, reply_markup=kb)
        except Exception as e:
            log.warning(f"file_id send fail (mid={mid_id}): {e}")
    if ch_id and ch_msg_id:
        try:
            return await bot_instance.copy_message(
                chat_id=user_id,
                from_chat_id=ch_id,
                message_id=ch_msg_id,
                caption=header,
                protect_content=True,
                reply_markup=kb)
        except Exception as e:
            log.error(f"copy_message fail (mid={mid_id}): {e}")
    return None


# =========================================================
# DARKBOOST — показ спонсоров
# =========================================================
async def show_db_sponsors(target, uid: int, username: str, prefix: str = ""):
    try:
        d = await db_api_sponsors(uid, username)
    except Exception as e:
        log.exception(f"db sponsors: {e}")
        await target.answer("😔 Ошибка. Попробуй позже /start.")
        return

    st = d.get("status")
    if st == "no_offers":
        db_mark_checked(uid)
        await target.answer("✅ Проверка не требуется. Заходи!")
        return
    if st == "busy":
        await target.answer(f"⏳ Подожди {d.get('retry_after_ms', 2000) / 1000:.1f} сек.")
        return
    if st == "cooldown":
        await target.answer(f"⏳ Через {d.get('reset_seconds', 60)} сек.")
        return
    if st == "disabled":
        return
    if st != "ok":
        await target.answer("⚠️ Сервис временно недоступен. Попробуй позже.")
        return

    sid = d["session_id"]
    sponsors = d.get("sponsors", [])[:MAX_OFFERS]
    if not sponsors:
        log.warning(f"show_db_sponsors: status=ok, но sponsors пусто. raw={d}")
        db_mark_checked(uid)
        await target.answer(
            "✅ Проверка не требуется. Заходи!\n\n"
            "<i>(Спонсоры пока не настроены)</i>",
            parse_mode="HTML")
        return

    _db_sessions[uid] = sid
    _db_sponsors[uid] = sponsors

    hours = REMIND_INTERVAL // 3600
    text = (
        f"{prefix}📢 <b>Подпишись на спонсоров</b>\n\n"
        f"После подписки нажми <b>«✅ Проверить»</b>.\n\n"
        f"📋 Спонсоров: <b>{len(sponsors)}</b>\n\n"
        f"🎥 <a href=\"{VIDEO_HOWTO}\">Как подписываться (видео)</a>\n\n"
        f"<i>Это займёт 10 секунд. После проверки — {hours} часов без рекламы.</i>"
    )
    kb = kb_db_sponsors(sponsors)

    if hasattr(target, "edit_text"):
        try:
            await target.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except TelegramBadRequest as e:
            err = str(e).lower()
            if "message is not modified" in err:
                pass
            elif "message can't be edited" in err or "message to edit not found" in err:
                try:
                    await target.answer(text, parse_mode="HTML", reply_markup=kb)
                except Exception as e2:
                    log.error(f"show_db_sponsors send: {e2}")
            else:
                log.error(f"show_db_sponsors edit: {e}")
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "db:restart")
async def db_restart(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if uid == ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()
    await show_db_sponsors(callback.message, uid,
                            callback.from_user.username or "",
                            "🔄 <b>Новый список.</b>\n\n")


# ИЗМЕНЕНО: db_check с try/except и callback.answer() в начале
@dp.callback_query(F.data == "db:check")
async def db_check(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if uid == ADMIN_ID:
        await callback.answer()
        return

    # 1) сразу снимаем спиннер
    try:
        await callback.answer("⏳ Проверяю...", show_alert=False)
    except Exception:
        pass

    sid = _db_sessions.get(uid)
    if not sid:
        try:
            await callback.message.edit_text(
                "🔄 Сессия истекла. Нажми «Заново».",
                reply_markup=kb_db_retry())
        except Exception as e:
            log.warning(f"db_check no_sid edit: {e}")
        return

    try:
        d = await db_api_check(uid, sid)
    except Exception as e:
        log.exception(f"db check api: {e}")
        try:
            await callback.message.edit_text(
                "😔 Ошибка проверки. Попробуй ещё раз.",
                reply_markup=kb_db_retry())
        except Exception as e2:
            log.warning(f"db_check api_err edit: {e2}")
        return

    st = d.get("status")
    hours = REMIND_INTERVAL // 3600
    log.info(f"db_check: uid={uid}, status={st}")

    if st == "ok":
        _db_sessions.pop(uid, None)
        _db_sponsors.pop(uid, None)
        db_mark_checked(uid)
        try:
            await callback.message.edit_text(
                f"✅ <b>Все подписки подтверждены!</b>\n\n"
                f"Добро пожаловать! 🎉\n"
                f"<i>{hours} часов без рекламы.</i>",
                parse_mode="HTML")
            log.info(f"db_check: edit ok for uid={uid}")
        except Exception as e:
            log.error(f"db_check ok edit FAIL: {e}")
            try:
                await callback.message.answer(
                    "✅ Подписки подтверждены! Жми /start.",
                    reply_markup=main_kb(uid))
            except Exception:
                pass
        return

    if st == "not_ok":
        missing = d.get("missing", [])
        blocked = d.get("blocked_offers", [])
        ids = set()
        for m in missing:
            if isinstance(m, dict):
                ids.add(m.get("offer_id") or m.get("id"))
            else:
                ids.add(str(m))
        blocked_ids = set()
        for m in blocked:
            if isinstance(m, dict):
                blocked_ids.add(m.get("id"))
        original = _db_sponsors.get(uid, [])
        lines = []
        for i, s in enumerate(original, 1):
            sid_ = s.get("id")
            if sid_ in ids:
                lines.append(f"• ❌ Спонсор #{i} — не подписан")
            elif sid_ in blocked_ids:
                lines.append(f"• 🚫 Спонсор #{i} — заблокирован")
        tail = ("\n\n" + "\n".join(lines)) if lines else ""
        try:
            await callback.message.edit_text(
                "⚠️ <b>Не все подписки выполнены.</b>\n\n"
                "Подпишись и нажми «Проверить»." + tail,
                parse_mode="HTML",
                reply_markup=kb_db_retry())
            log.info(f"db_check: not_ok edit ok for uid={uid}")
        except Exception as e:
            log.error(f"db_check not_ok edit FAIL: {e}")
            try:
                await callback.message.answer(
                    "⚠️ Не все подписки. Нажми /start и проверь заново.",
                    reply_markup=main_kb(uid))
            except Exception:
                pass
        return

    if st == "busy":
        try:
            await callback.message.edit_text(
                f"⏳ Подожди {d.get('retry_after_ms', 2000) / 1000:.1f} сек.",
                reply_markup=kb_db_retry())
        except Exception:
            pass
    elif st == "cooldown":
        try:
            await callback.message.edit_text(
                f"⏳ Через {d.get('reset_seconds', 60)} сек.",
                reply_markup=kb_db_retry())
        except Exception:
            pass
    elif st == "expired" or (st == "failed" and d.get("new_session_required")):
        _db_sessions.pop(uid, None)
        try:
            await callback.message.edit_text(
                "🔄 Сессия истекла. Нажми «Заново».",
                reply_markup=kb_db_retry())
        except Exception:
            pass
    elif st == "failed":
        try:
            await callback.message.edit_text(
                "⚠️ Сервис временно недоступен. Попробуй через пару минут.",
                reply_markup=kb_db_retry())
        except Exception:
            pass
    elif st == "disabled":
        db_mark_checked(uid)
        try:
            await callback.message.edit_text("✅ Проверка не требуется.")
        except Exception:
            pass
    else:
        try:
            await callback.message.edit_text(
                f"⚠️ Ошибка DarkBoost: {st}",
                reply_markup=kb_db_retry())
        except Exception:
            pass


# =========================================================
# /start (ИЗМЕНЕНО: убран показ спонсоров при /start)
# =========================================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext, bot: Bot):
    await state.clear()
    if is_banned(message.from_user.id):
        await message.answer("🚫 Вы забанены.")
        return

    is_new = False
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (message.from_user.id,))
    if not c.fetchone():
        is_new = True
    conn.close()

    args = message.text.split()
    ref_applied = False
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            inviter_id = int(args[1].replace("ref_", ""))
            bonus = get_referral_bonus()
            if save_referral(message.from_user.id, inviter_id):
                ref_applied = True
                add_balance(inviter_id, bonus, "👥 Реферал")
                try:
                    await bot.send_message(
                        inviter_id,
                        f"🎉 <b>По твоей ссылке пришёл новый юзер!</b>\n\n"
                        f"💰 Начислено: <b>+{bonus}✈️</b>\n"
                        f"💳 Баланс: <b>{get_balance(inviter_id)}✈️</b>",
                        parse_mode="HTML")
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"Referral error: {e}")

    register_user(message.from_user)
    activate_user(message.from_user)
    unmark_user_blocked(message.from_user.id)

    if is_new:
        username = f"@{message.from_user.username}" if message.from_user.username else "нет"
        full_name = message.from_user.full_name or "—"
        ref_str = "да" if ref_applied else "нет"
        try:
            me = await bot.get_me()
            bot_tag = f"@{me.username}"
        except Exception:
            bot_tag = "?"
        await send_log(
            f"🆕 <b>Новый пользователь!</b> ({bot_tag})\n\n"
            f"👤 {full_name}\n"
            f"🔗 {username}\n"
            f"🆔 <code>{message.from_user.id}</code>\n"
            f"👥 Пришёл по рефке: <b>{ref_str}</b>\n"
            f"📊 Всего: <b>{get_users_count()}</b>", bot)

    if not is_agreement_accepted(message.from_user.id):
        await show_agreement(message, first_time=True)
        return

    # ИЗМЕНЕНО: убран показ спонсоров при /start
    # if HAS_DARKBOOST and db_needs_check(message.from_user.id):
    #     await show_db_sponsors(message, message.from_user.id,
    #                             message.from_user.username or "", "👋 Привет! ")
    #     return

    activity_bonus_given = False
    if was_inactive_3days(message.from_user.id):
        give_activity_bonus(message.from_user.id, 25)
        activity_bonus_given = True

    if activity_bonus_given:
        await message.answer(
            "🎁 <b>С возвращением!</b>\n\n"
            f"Тебя давно не было — вот бонус <b>+25✈️</b>!\n"
            f"💳 Баланс: <b>{get_balance(message.from_user.id)}✈️</b>",
            parse_mode="HTML")

    if not was_low_balance_notified(message.from_user.id):
        bal = get_balance(message.from_user.id)
        if bal < 3 and has_available_earn(message.from_user.id):
            conn = db()
            c = conn.cursor()
            c.execute("SELECT joined_at FROM users WHERE user_id=?",
                      (message.from_user.id,))
            r = c.fetchone()
            conn.close()
            age = 0
            if r and r[0]:
                try:
                    age = (datetime.now() - datetime.fromisoformat(r[0])).total_seconds()
                except Exception:
                    age = 0
            is_new_u = age < 86400
            is_old_u = age >= 7 * 86400
            if (is_new_u or is_old_u) and not user_ever_claimed_bonus(message.from_user.id):
                mark_low_balance_notified(message.from_user.id)
                if is_old_u:
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎁 Забрать всё",
                                              callback_data="earn:claim_all")],
                        [InlineKeyboardButton(text="💰 Заработать",
                                              callback_data="goto_earn")],
                    ])
                    push_text = (
                        "💸 <b>У тебя почти нет валюты!</b>\n\n"
                        f"💰 Баланс: <b>{bal}✈️</b>\n\n"
                        "🎁 В «Заработать» тебя ждут бонусы — забери всё одним нажатием!"
                    )
                else:
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💰 Заработать",
                                              callback_data="goto_earn")],
                    ])
                    push_text = (
                        "💸 <b>У тебя почти нет валюты!</b>\n\n"
                        f"💰 Баланс: <b>{bal}✈️</b>\n\n"
                        "🎁 В разделе «💰 Заработать» тебя ждут бонусы — забери их!"
                    )
                await message.answer(push_text, parse_mode="HTML", reply_markup=kb)

    marks = []
    if has_plus(message.from_user.id):
        marks.append("💎")
    if has_allow(message.from_user.id):
        marks.append("🔓")
    mark_str = " " + " ".join(marks) if marks else ""
    text = f"{greeting()}, {message.from_user.first_name}!{mark_str} 👋\n\n🎬 Добро пожаловать!"
    await message.answer(text, reply_markup=main_kb(message.from_user.id))


async def show_agreement(message: types.Message, first_time=False):
    if first_time:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Погнали", callback_data="agree:accept")]
        ])
        await message.answer(AGREEMENT_TEXT, parse_mode="HTML", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")]
        ])
        await message.answer(AGREEMENT_TEXT, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "agree:accept")
async def agree_accept(callback: types.CallbackQuery):
    accept_agreement(callback.from_user.id)
    try:
        await callback.message.delete()
    except Exception:
        pass
    # ИЗМЕНЕНО: убран показ спонсоров после соглашения
    # if HAS_DARKBOOST and db_needs_check(callback.from_user.id):
    #     await show_db_sponsors(callback.message, callback.from_user.id,
    #                             callback.from_user.username or "", "👋 Привет! ")
    #     await callback.answer()
    #     return
    await callback.message.answer(
        "✅ Соглашение принято!\n\nДобро пожаловать в бот 🎬",
        reply_markup=main_kb(callback.from_user.id))
    await callback.answer("Добро пожаловать!")


@dp.message(Command("cancel"))
async def cancel_any(message: types.Message, state: FSMContext):
    await state.clear()
    if is_activated(message.from_user.id):
        await message.answer("❌ Отменено.", reply_markup=main_kb(message.from_user.id))
    else:
        await message.answer("❌ Отменено.")


def try_activate_any_promo(user_id, code):
    ok, msg, reward, uses, max_uses = activate_promo(user_id, code)
    if ok:
        return True, msg, "balance", reward, None, uses, max_uses
    ok2, msg2, priv, uses2, max_uses2 = activate_promo_priv(user_id, code)
    if ok2:
        return True, msg2, "priv", 0, priv, uses2, max_uses2
    return False, msg, None, 0, None, uses, max_uses


# ИЗМЕНЕНО: добавлен send_log после активации
@dp.message(Command("promo"))
async def cmd_promo(message: types.Message, state: FSMContext, bot: Bot):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "🎟 <b>Ввод промокода</b>\n\n"
            "Использование: <code>/promo КОД</code>",
            parse_mode="HTML", reply_markup=main_kb(message.from_user.id))
        return
    code = args[1].strip()
    if not code:
        await message.answer("⚠️ Введи код после команды.")
        return
    if not is_activated(message.from_user.id):
        await message.answer("🔒 Сначала напиши /start",
                             reply_markup=main_kb(message.from_user.id))
        return
    if is_banned(message.from_user.id):
        await message.answer("🚫 Вы забанены.")
        return

    ok, msg, kind, reward, priv, uses, max_uses = try_activate_any_promo(message.from_user.id, code)
    await state.clear()
    mu_str = f"{uses}/{max_uses}" if max_uses > 0 else f"{uses}/∞"

    if ok:
        if kind == "balance":
            new_bal = get_balance(message.from_user.id)
            await message.answer(
                f"{msg}\n👥 Активаций: <b>{mu_str}</b>\n"
                f"💳 Баланс: <b>{new_bal}✈️</b>",
                parse_mode="HTML",
                reply_markup=main_kb(message.from_user.id))
            await send_log(
                f"🎟 <b>Промокод (баланс)</b>\n"
                f"👤 @{message.from_user.username or 'нет'} "
                f"({message.from_user.full_name or '—'})\n"
                f"🆔 <code>{message.from_user.id}</code>\n"
                f"🎫 <code>{code}</code>\n"
                f"💰 +{reward}✈️\n"
                f"👥 Активаций: <b>{mu_str}</b>", bot)
        else:
            priv_type, days = priv
            if priv_type == "plus":
                admin_grant_plus(message.from_user.id, days)
                exp = get_plus_time_left(message.from_user.id)
            else:
                admin_grant_allow(message.from_user.id, days)
                exp = get_allow_time_left(message.from_user.id)
            await message.answer(
                f"{msg}\n👥 Активаций: <b>{mu_str}</b>\n"
                f"⏳ Осталось: <b>{format_time(exp)}</b>",
                parse_mode="HTML",
                reply_markup=main_kb(message.from_user.id))
            await send_log(
                f"🎟 <b>Промокод ({priv_type.upper()})</b>\n"
                f"👤 @{message.from_user.username or 'нет'} "
                f"({message.from_user.full_name or '—'})\n"
                f"🆔 <code>{message.from_user.id}</code>\n"
                f"🎫 <code>{code}</code>\n"
                f"⏳ +{days} дней\n"
                f"👥 Активаций: <b>{mu_str}</b>", bot)
    else:
        tail = f"\n👥 Активаций: <b>{mu_str}</b>" if (max_uses > 0 or uses > 0) else ""
        await message.answer(msg + tail, parse_mode="HTML",
                             reply_markup=main_kb(message.from_user.id))
                             # =========================================================
# КАТАЛОГ
# =========================================================
@dp.message(F.text == "📁 Каталог")
async def catalog(message: types.Message):
    cats = get_categories()
    if not cats:
        await message.answer("📂 Каталог пуст.")
        return
    buttons = []
    row = []
    for c in cats:
        cid = c[0]
        name = c[1]
        emoji = c[2]
        cnt = c[4]
        short_name = name[:15] + "…" if len(name) > 15 else name
        row.append(InlineKeyboardButton(
            text=f"{emoji} {short_name} · {cnt}",
            callback_data=f"c:{cid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    marks = ""
    if has_allow(message.from_user.id):
        marks = " 🔓"
    elif has_plus(message.from_user.id):
        marks = " 💎"
    await message.answer(
        f"📂 <b>Каталог</b>{marks}\n💰 Баланс: {get_balance(message.from_user.id)}✈️",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("c:"))
async def open_category(callback: types.CallbackQuery):
    cat_id = int(callback.data.split(":")[1])
    cat = get_category(cat_id)
    if not cat:
        await callback.answer("Не найдено", show_alert=True)
        return
    _, name, emoji, paid_price, channel_id, cover = cat
    media = get_media_in_category(cat_id)
    if not media:
        await callback.answer("Пусто", show_alert=True)
        return
    allow = has_allow(callback.from_user.id)
    if allow:
        sale_price = 0
    else:
        sale_price = get_plus_sale_price(paid_price, callback.from_user.id)
    full_price = sale_price * len(media)
    balance = get_balance(callback.from_user.id)
    plus_str = ""
    if allow:
        plus_str = "\n🔓 <b>ALLOW: всё бесплатно</b>"
    elif has_plus(callback.from_user.id):
        plus_str = f"\n💎 PLUS-скидка: {paid_price} → <b>{sale_price}✈️</b>"
    text = (f"{emoji} <b>{name}</b>\n"
            f"📦 Видео: <b>{len(media)}</b>\n"
            f"💰 Цена за 1: <b>{sale_price}✈️</b>{plus_str}\n"
            f"🔓 Всё: <b>{full_price}✈️</b>\n"
            f"\n💳 Баланс: <b>{balance}✈️</b>")
    rows = [[InlineKeyboardButton(text="▶️ Смотреть", callback_data=f"watch:{cat_id}:1")]]
    if not allow and not is_category_fully_bought(callback.from_user.id, cat_id):
        rows.append([InlineKeyboardButton(
            text=f"🔓 Купить всё за {full_price}✈️",
            callback_data=f"buyfull:{cat_id}")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if cover:
        try:
            await callback.message.delete()
        except Exception:
            pass
        try:
            await callback.message.answer_photo(
                cover, caption=text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            log.warning(f"Cover fail: {e}")
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    else:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ИЗМЕНЕНО: реклама после 10 просмотров
@dp.callback_query(F.data.startswith("watch:"))
async def watch_video(callback: types.CallbackQuery, bot: Bot):
    _, cat_id, pos = callback.data.split(":")
    cat_id, pos = int(cat_id), int(pos)
    cat = get_category(cat_id)
    if not cat:
        await callback.answer("Не найдено", show_alert=True)
        return
    media = get_media_in_category(cat_id)
    if pos < 1 or pos > len(media):
        await callback.answer("Конец", show_alert=True)
        return
    row = media[pos - 1]
    mid = row[0]
    if not is_media_unlocked(callback.from_user.id, cat_id, mid, pos):
        pp = get_plus_sale_price(cat[3], callback.from_user.id)
        balance = get_balance(callback.from_user.id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {pp}✈️",
                                  callback_data=f"buymedia:{mid}:{cat_id}:{pos}")],
            [InlineKeyboardButton(text="📂 К категории", callback_data=f"c:{cat_id}")],
        ])
        await callback.message.answer(
            f"🔒 Видео #{pos} платное.\n💰 Цена: <b>{pp}✈️</b>\n💳 Баланс: <b>{balance}✈️</b>",
            parse_mode="HTML", reply_markup=kb)
        await callback.answer()
        return
    increment_views(mid)
    if not is_media_bought(callback.from_user.id, mid) and not is_category_fully_bought(callback.from_user.id, cat_id):
        log_download(callback.from_user.id, mid)

    # ИЗМЕНЕНО: реклама после 10 просмотров
    total_views = get_user_total_views(callback.from_user.id)
    if HAS_DARKBOOST and total_views >= 10 and db_needs_check(callback.from_user.id):
        await show_db_sponsors(callback.message, callback.from_user.id,
                                callback.from_user.username or "", "")
        return

    caption = row[5]
    mins = get_auto_delete_delay() // 60
    header = f"📹 Видео {pos}/{len(media)}\n⏳ Удалится через {mins} мин\n🔒 Пересылка запрещена"
    if caption:
        header += f"\n\n{caption}"
    kb = watch_kb(cat_id, media, callback.from_user.id, pos)
    sent_msg = await send_media_to_user(bot, callback.from_user.id, row, header, kb)
    if not sent_msg:
        await callback.message.answer("❌ Не удалось отправить видео")
    else:
        schedule_auto_delete_message(
            bot, callback.from_user.id, sent_msg.message_id,
            delay=get_auto_delete_delay())
    await callback.answer()


@dp.callback_query(F.data == "del_this")
async def del_this(callback: types.CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Удалено")


# ИЗМЕНЕНО: report_menu с 3 причинами
@dp.callback_query(F.data.startswith("report_menu:"))
async def report_menu(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    now = time.time()
    if now - _report_cooldown.get(uid, 0) < 60:
        await callback.answer("⏳ Уже отправлено, подожди минуту", show_alert=False)
        return
    try:
        parts = callback.data.split(":")
        cat_id = int(parts[1])
        pos = int(parts[2])
        media_id = int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("⚠️ Ошибка", show_alert=False)
        return

    await state.update_data(report_cat_id=cat_id, report_pos=pos, report_media_id=media_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Видео не загружается",
                              callback_data="report_reason:not_loading")],
        [InlineKeyboardButton(text="📂 Видео не соответствует категории",
                              callback_data="report_reason:wrong_category")],
        [InlineKeyboardButton(text="✏️ Написать самому",
                              callback_data="report_reason:custom")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="report_reason:cancel")],
    ])
    await callback.message.answer(
        "⚠️ <b>Что не так с видео?</b>\n\nВыбери причину:",
        parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("report_reason:"))
async def report_reason(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    reason = callback.data.split(":")[1]
    data = await state.get_data()
    cat_id = data.get("report_cat_id")
    pos = data.get("report_pos")
    media_id = data.get("report_media_id")
    if not cat_id:
        await callback.answer("⚠️ Сессия истекла. Нажми «Сообщить об ошибке» ещё раз.", show_alert=True)
        return

    if reason == "cancel":
        await state.clear()
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer("Отменено")
        return

    if reason == "custom":
        await state.set_state(UserStates.report_custom)
        await callback.message.edit_text(
            "✏️ <b>Напиши, что не так с видео</b>\n\n"
            "Отправь текст одним сообщением.",
            parse_mode="HTML")
        await callback.answer()
        return

    reason_label = "🎬 Видео не загружается" if reason == "not_loading" else "📂 Видео не соответствует категории"
    cat = get_category(cat_id)
    cat_name = cat[1] if cat else "—"
    total = len(get_media_in_category_ordered(cat_id)) or 0

    _report_cooldown[callback.from_user.id] = time.time()
    await send_report_to_admin(
        bot, callback.from_user, cat_id, cat_name, pos, total, media_id, reason_label
    )
    await state.clear()
    try:
        await callback.message.edit_text(
            f"✅ <b>Жалоба отправлена</b>\n\n📋 Причина: {reason_label}",
            parse_mode="HTML")
    except Exception:
        pass
    await callback.answer("✅ Отправлено")


@dp.message(UserStates.report_custom)
async def report_custom_text(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Напиши текст.")
        return
    if len(text) > 500:
        text = text[:500] + "…"
    data = await state.get_data()
    cat_id = data.get("report_cat_id")
    pos = data.get("report_pos")
    media_id = data.get("report_media_id")
    if not cat_id:
        await state.clear()
        await message.answer("⚠️ Сессия истекла. Нажми «Сообщить об ошибке» ещё раз.")
        return
    cat = get_category(cat_id)
    cat_name = cat[1] if cat else "—"
    total = len(get_media_in_category_ordered(cat_id)) or 0
    _report_cooldown[message.from_user.id] = time.time()
    await send_report_to_admin(
        bot, message.from_user, cat_id, cat_name, pos, total, media_id,
        "✏️ Своя причина", custom_text=text
    )
    await state.clear()
    await message.answer(
        "✅ <b>Жалоба отправлена админу</b>",
        parse_mode="HTML",
        reply_markup=main_kb(message.from_user.id))


@dp.callback_query(F.data.startswith("buymedia:"))
async def buy_media_cb(callback: types.CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) == 4:
        _, mid, cat_id, pos = parts
        mid, cat_id, pos = int(mid), int(cat_id), int(pos)
    else:
        mid = int(parts[1])
        m = get_media_by_id(mid)
        cat_id = m[1]
        media = get_media_in_category(cat_id)
        pos = next((i for i, x in enumerate(media, 1) if x[0] == mid), 1)
    m = get_media_by_id(mid)
    if not m:
        await callback.answer("Не найдено", show_alert=True)
        return
    cat = get_category(cat_id)
    base_price = cat[3]
    if has_allow(callback.from_user.id):
        price = 0
    else:
        price = get_plus_sale_price(base_price, callback.from_user.id)
    if not check_purchase_cooldown(callback.from_user.id, f"media_{mid}", 2):
        await callback.answer("⏳ Подожди 2 секунды", show_alert=True)
        return
    if is_media_bought(callback.from_user.id, mid) or is_category_fully_bought(callback.from_user.id, cat_id):
        await callback.answer("Уже куплено", show_alert=True)
        return
    if price > 0 and not spend_balance(callback.from_user.id, price, f"🎬 Видео #{mid} ({cat[1]})"):
        await callback.answer(f"❌ Не хватает ✈️", show_alert=True)
        return
    buy_media(callback.from_user.id, mid, price_paid=price)
    new_bal = get_balance(callback.from_user.id)
    increment_views(mid)
    log_download(callback.from_user.id, mid)
    if price > 0:
        await callback.answer(f"✅ -{price}✈️")
    else:
        await callback.answer(f"🔓 ALLOW — бесплатно")
    user = callback.from_user
    await send_log(
        f"💰 <b>Покупка видео</b>\n"
        f"👤 @{user.username or 'нет'} (<code>{user.id}</code>)\n"
        f"📂 {cat[2]} {cat[1]}\n"
        f"💵 Цена: <b>{price}✈️</b>\n"
        f"💳 Остаток: <b>{new_bal}✈️</b>", bot)
    media = get_media_in_category(cat_id)
    row = media[pos - 1]
    caption = row[5]
    mins = get_auto_delete_delay() // 60
    header = f"✅ Оплачено! 📹 Видео {pos}/{len(media)}\n💰 Баланс: {new_bal}✈️\n⏳ Удалится через {mins} мин\n🔒 Пересылка запрещена"
    if caption:
        header += f"\n\n{caption}"
    kb = watch_kb(cat_id, media, callback.from_user.id, pos)
    sent_msg = await send_media_to_user(bot, callback.from_user.id, row, header, kb)
    if not sent_msg:
        await callback.message.answer("❌ Не удалось отправить видео")
    else:
        schedule_auto_delete_message(
            bot, callback.from_user.id, sent_msg.message_id,
            delay=get_auto_delete_delay())


@dp.callback_query(F.data.startswith("buyfull:"))
async def buy_full(callback: types.CallbackQuery, bot: Bot):
    cat_id = int(callback.data.split(":")[1])
    cat = get_category(cat_id)
    if not cat:
        await callback.answer("Не найдено", show_alert=True)
        return
    media = get_media_in_category(cat_id)
    if has_allow(callback.from_user.id):
        sale_price = 0
    else:
        sale_price = get_plus_sale_price(cat[3], callback.from_user.id)
    fp = sale_price * len(media)
    if fp <= 0:
        await callback.answer("Пусто", show_alert=True)
        return
    if is_category_fully_bought(callback.from_user.id, cat_id):
        await callback.answer("Уже куплено", show_alert=True)
        return
    if not check_purchase_cooldown(callback.from_user.id, f"cat_{cat_id}", 2):
        await callback.answer("⏳ Подожди 2 секунды", show_alert=True)
        return
    if not spend_balance(callback.from_user.id, fp, f"📂 Категория {cat[1]}"):
        await callback.answer(f"❌ Не хватает ✈️ (нужно {fp})", show_alert=True)
        return
    buy_full_category(callback.from_user.id, cat_id, price_paid=fp)
    new_bal = get_balance(callback.from_user.id)
    if media:
        first_mid = media[0][0]
        increment_views(first_mid)
        log_download(callback.from_user.id, first_mid)
    await callback.answer(f"✅ -{fp}✈️")
    user = callback.from_user
    await send_log(
        f"🔓 <b>Покупка всей категории</b>\n"
        f"👤 @{user.username or 'нет'} (<code>{user.id}</code>)\n"
        f"📂 {cat[2]} {cat[1]}\n"
        f"📦 Видео: {len(media)}\n"
        f"💵 Цена: <b>{fp}✈️</b>\n"
        f"💳 Остаток: <b>{new_bal}✈️</b>", bot)
    try:
        await callback.message.edit_text(
            f"✅ Вся категория открыта!\n💰 Баланс: <b>{new_bal}✈️</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Смотреть", callback_data=f"watch:{cat_id}:1")],
            ]))
    except Exception:
        pass


@dp.callback_query(F.data == "noop")
async def noop(callback: types.CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "m:home")
async def back_home(callback: types.CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("🏠 Меню", reply_markup=main_kb(callback.from_user.id))
    await callback.answer()


# =========================================================
# ЗАРАБОТОК (ИЗМЕНЕНО: 2 столбца + кнопка Кликер)
# =========================================================
async def render_earn_menu(target_message, state, uid):
    await state.clear()
    hourly_ready = can_take_hourly(uid)
    daily_ready = can_take_daily(uid)
    hourly_reward = get_hourly_reward()
    daily_reward, is_weekend = get_daily_bonus_with_multiplier()
    plus_bonus_h = get_plus_hourly_bonus() if has_plus(uid) else 0
    plus_bonus_d = get_plus_daily_bonus() if has_plus(uid) else 0
    total_h = hourly_reward + plus_bonus_h
    total_d = daily_reward + plus_bonus_d
    plus_line_h = f" 💎+{plus_bonus_h}" if plus_bonus_h else ""
    plus_line_d = f" 💎+{plus_bonus_d}" if plus_bonus_d else ""
    weekend_mark = " 🔥×1.5" if is_weekend else ""
    hourly_str = (
        f"🕐 <b>Ежечасный бонус</b> — <b>+{total_h}✈️</b>{plus_line_h}\n"
        f"   {'✅ Доступен' if hourly_ready else '⏳ Через ' + format_time(get_time_until_hourly(uid))}")
    daily_str = (
        f"📅 <b>Ежедневный бонус</b> — <b>+{total_d}✈️</b>{plus_line_d}{weekend_mark}\n"
        f"   {'✅ Доступен' if daily_ready else '⏳ Через ' + format_time(get_time_until_daily(uid))}")

    streak, last_claim, total_claims = get_streak(uid)
    streak_ready = can_claim_streak(uid)
    next_day = get_streak_next_day(uid)
    next_reward = get_streak_reward(next_day)
    plus_bonus_line = " + 💎 PLUS 1д" if next_day == STREAK_PLUS_DAY else ""
    streak_line = (
        f"🔥 <b>Стрик</b> — день <b>{streak}</b> · всего {total_claims}\n"
        f"   {'✅ Доступен' if streak_ready else '⏳ Уже получен сегодня'}"
        f" · награда <b>+{next_reward}✈️</b>{plus_bonus_line}"
    )

    quests_avail = [q for q in get_quest_stats(uid)
                    if q["progress"] >= q["target"] and not q["claimed"]]

    text = (
        f"💰 <b>Заработать ✈️</b>\n\n"
        f"{hourly_str}\n\n"
        f"{daily_str}\n\n"
        f"{streak_line}\n\n"
        f"📝 <b>Задания</b> — выполняй и получай ✈️!\n"
        f"🎯 <b>Квесты недели</b> — выполняй и забирай бонусы!\n"
        f"📹 <b>Предложить видео</b> — за одобренное админ начислит ✈️\n"
        f"🖱 <b>Кликер</b> — отдельный бот для заработка"
    )

    # === КНОПКИ В 2 КОЛОНКИ ===
    rows = []
    top_buttons = []
    if hourly_ready:
        top_buttons.append(InlineKeyboardButton(text=f"🕐 Забрать {total_h}✈️",
                                                 callback_data="earn:hourly"))
    if daily_ready:
        top_buttons.append(InlineKeyboardButton(text=f"📅 Забрать {total_d}✈️",
                                                 callback_data="earn:daily"))
    if streak_ready:
        label = f"🔥 Стрик +{next_reward}✈️"
        if next_day == STREAK_PLUS_DAY:
            label += " 💎"
        top_buttons.append(InlineKeyboardButton(text=label,
                                                 callback_data="earn:streak"))

    # разбиваем на пары
    for i in range(0, len(top_buttons), 2):
        rows.append(top_buttons[i:i + 2])

    # «Забрать всё» — отдельно, на всю ширину
    if user_can_claim_all(uid):
        total_available = 0
        if hourly_ready:
            total_available += total_h
        if daily_ready:
            total_available += total_d
        if streak_ready:
            total_available += next_reward
        for q in quests_avail:
            total_available += q["reward"]
        if total_available > 0:
            rows.append([InlineKeyboardButton(
                text=f"🎁 Забрать всё: +{total_available}✈️",
                callback_data="earn:claim_all")])

    # === НИЖНИЕ КНОПКИ В 2 КОЛОНКИ ===
    bottom = [
        InlineKeyboardButton(text="🎯 Квесты", callback_data="goto_quests"),
        InlineKeyboardButton(text="📝 Задания", callback_data="goto_tasks"),
        InlineKeyboardButton(text="📹 Предложить", callback_data="earn:suggest"),
        InlineKeyboardButton(text="🖱 Кликер", url="https://t.me/ZOVVOZZOVVOXbot"),
    ]
    for i in range(0, len(bottom), 2):
        rows.append(bottom[i:i + 2])

    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await target_message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await target_message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(F.text == "💰 Заработать")
async def earn_menu(message: types.Message, state: FSMContext):
    await render_earn_menu(message, state, message.from_user.id)


@dp.callback_query(F.data == "earn:back")
async def earn_back(callback: types.CallbackQuery, state: FSMContext):
    await render_earn_menu(callback.message, state, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "goto_earn")
async def goto_earn(callback: types.CallbackQuery, state: FSMContext):
    await render_earn_menu(callback.message, state, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "earn:hourly")
async def earn_hourly(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if not can_take_hourly(uid):
        remaining = format_time(get_time_until_hourly(uid))
        await callback.answer(f"⏳ Подожди ещё {remaining}", show_alert=True)
        return
    base = get_hourly_reward()
    plus_b = get_plus_hourly_bonus() if has_plus(uid) else 0
    reward = base + plus_b
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET last_accrual=? WHERE user_id=?",
              (datetime.now().isoformat(), uid))
    conn.commit()
    conn.close()
    add_balance(uid, reward, "🕐 Часовой бонус")
    new_bal = get_balance(uid)
    plus_line = f"\n💎 PLUS-бонус: <b>+{plus_b}✈️</b>" if plus_b else ""
    await callback.answer(f"✅ +{reward}✈️")
    try:
        await callback.message.edit_text(
            f"🕐 <b>Ежечасный бонус получен!</b>\n\n"
            f"💰 Базовый: <b>+{base}✈️</b>{plus_line}\n"
            f"💵 Итого: <b>+{reward}✈️</b>\n"
            f"💳 Баланс: <b>{new_bal}✈️</b>\n\n⏳ Следующий бонус через 1 час",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К заработку", callback_data="earn:back")],
                [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
            ]))
    except Exception:
        pass


@dp.callback_query(F.data == "earn:daily")
async def earn_daily(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if not can_take_daily(uid):
        remaining = format_time(get_time_until_daily(uid))
        await callback.answer(f"⏳ Подожди ещё {remaining}", show_alert=True)
        return
    base, is_weekend = get_daily_bonus_with_multiplier()
    plus_b = get_plus_daily_bonus() if has_plus(uid) else 0
    reward = base + plus_b
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET last_daily=? WHERE user_id=?",
              (datetime.now().isoformat(), uid))
    conn.commit()
    conn.close()
    add_balance(uid, reward, "📅 Дневной бонус")
    new_bal = get_balance(uid)
    plus_line = f"\n💎 PLUS-бонус: <b>+{plus_b}✈️</b>" if plus_b else ""
    wm = " 🔥×1.5" if is_weekend else ""
    await callback.answer(f"✅ +{reward}✈️")
    try:
        await callback.message.edit_text(
            f"📅 <b>Ежедневный бонус получен!</b>\n\n"
            f"💰 Базовый: <b>+{base}✈️</b>{wm}{plus_line}\n"
            f"💵 Итого: <b>+{reward}✈️</b>\n"
            f"💳 Баланс: <b>{new_bal}✈️</b>\n\n⏳ Следующий бонус через 24 часа",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К заработку", callback_data="earn:back")],
                [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
            ]))
    except Exception:
        pass


@dp.callback_query(F.data == "earn:streak")
async def earn_streak(callback: types.CallbackQuery):
    uid = callback.from_user.id
    ok, day, reward, plus_granted, reason = claim_streak(uid)
    if not ok:
        await callback.answer("⏳ Ты уже забрал стрик сегодня", show_alert=True)
        return
    new_bal = get_balance(uid)
    plus_line = "\n💎 <b>+ PLUS на 1 день!</b>" if plus_granted else ""
    next_day = day + 1
    next_reward = get_streak_reward(next_day)
    next_plus = " + 💎 PLUS 1д" if next_day == STREAK_PLUS_DAY else ""
    await callback.answer(f"✅ +{reward}✈️")
    try:
        await callback.message.edit_text(
            f"🔥 <b>Стрик: день {day}</b>\n\n"
            f"💰 Награда: <b>+{reward}✈️</b>{plus_line}\n"
            f"💳 Баланс: <b>{new_bal}✈️</b>\n\n"
            f"⏳ Завтра: <b>+{next_reward}✈️</b>{next_plus}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К заработку", callback_data="earn:back")],
                [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
            ]))
    except Exception:
        pass


@dp.callback_query(F.data == "earn:claim_all")
async def earn_claim_all(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if not user_can_claim_all(uid):
        await callback.answer("⚠️ Доступно после 7 дней в боте", show_alert=True)
        return
    res = claim_all_earn(uid)
    if res["total"] <= 0:
        await callback.answer("Нечего забирать", show_alert=True)
        return
    lines = []
    if res["hourly"]:
        lines.append(f"🕐 Часовой: <b>+{res['hourly']}✈️</b>")
    if res["daily"]:
        lines.append(f"📅 Дневной: <b>+{res['daily']}✈️</b>")
    if res["streak"]:
        plus = " + 💎 PLUS 1д" if res["streak_plus"] else ""
        lines.append(f"🔥 Стрик (день {res['streak_day']}): <b>+{res['streak']}✈️</b>{plus}")
    for name, reward in res["quests"]:
        lines.append(f"🎯 {name}: <b>+{reward}✈️</b>")
    new_bal = get_balance(uid)
    text = (
        f"🎁 <b>Забрано всё!</b>\n\n"
        + "\n".join(lines) +
        f"\n\n💰 Итого: <b>+{res['total']}✈️</b>\n"
        f"💳 Баланс: <b>{new_bal}✈️</b>"
    )
    await callback.answer(f"✅ +{res['total']}✈️")
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К заработку", callback_data="earn:back")],
                [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
            ]))
    except Exception:
        pass


# =========================================================
# КВЕСТЫ (UI)
# =========================================================
async def render_quests_menu(target_message, state, uid):
    await state.clear()
    quests = get_quest_stats(uid)
    week = get_week_start_iso()[:10]
    try:
        wd = datetime.fromisoformat(week).strftime("%d.%m")
    except Exception:
        wd = week
    text = f"🎯 <b>Квесты недели</b>\n<i>с {wd}</i>\n\n"
    rows = []
    for q in quests:
        prog = q["progress"]
        tgt = q["target"]
        done = prog >= tgt
        if q["claimed"]:
            status = "✅ Получено"
        elif done:
            status = "🎁 Забрать!"
        else:
            status = f"{prog}/{tgt}"
        bar_filled = int(10 * prog / tgt) if tgt else 0
        bar = "▓" * bar_filled + "░" * (10 - bar_filled)
        text += (f"{q['emoji']} <b>{q['name']}</b> — +{q['reward']}✈️\n"
                 f"   <code>{bar}</code> {status}\n"
                 f"   <i>{q['desc']}</i>\n\n")
        if done and not q["claimed"]:
            rows.append([InlineKeyboardButton(
                text=f"🎁 Забрать {q['reward']}✈️ ({q['name']})",
                callback_data=f"quest:claim:{q['id']}")])
    rows.append([InlineKeyboardButton(text="💰 К заработку", callback_data="earn:back")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await target_message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await target_message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "goto_quests")
async def goto_quests(callback: types.CallbackQuery, state: FSMContext):
    await render_quests_menu(callback.message, state, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data.startswith("quest:claim:"))
async def quest_claim(callback: types.CallbackQuery):
    qid = int(callback.data.split(":")[2])
    uid = callback.from_user.id
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id, code, name, target, reward FROM quests_weekly WHERE id=?", (qid,))
    q = c.fetchone()
    conn.close()
    if not q:
        await callback.answer("Квест не найден", show_alert=True)
        return
    progress, claimed = get_quest_progress(uid, qid)
    if claimed:
        await callback.answer("Уже получено", show_alert=True)
        return
    if progress < q[3]:
        await callback.answer(f"Не выполнено ({progress}/{q[3]})", show_alert=True)
        return
    mark_quest_claimed(uid, qid)
    new_bal = add_balance(uid, q[4], f"🎯 Квест: {q[2]}")
    await callback.answer(f"✅ +{q[4]}✈️")
    try:
        await callback.message.edit_text(
            f"✅ <b>Квест выполнен!</b>\n\n"
            f"{q[2]} → <b>+{q[4]}✈️</b>\n"
            f"💳 Баланс: <b>{new_bal}✈️</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎯 К квестам", callback_data="goto_quests")],
                [InlineKeyboardButton(text="💰 К заработку", callback_data="earn:back")],
                [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
            ]))
    except Exception:
        pass
        # =========================================================
# ПРОФИЛЬ
# =========================================================
@dp.message(F.text == "👤 Профиль")
async def profile(message: types.Message):
    stats = get_user_stats(message.from_user.id)
    if not stats:
        await message.answer("Введите /start")
        return
    joined, downloads, banned, balance = stats
    try:
        joined_str = datetime.fromisoformat(joined).strftime("%d.%m.%Y")
    except Exception:
        joined_str = joined
    status = "🚫 Забанен" if banned else "✅ Активен"
    week_views = get_user_week_views(message.from_user.id)
    invited = get_invited_count(message.from_user.id)
    streak, _, _ = get_streak(message.from_user.id)
    sub_lines = []
    if has_plus(message.from_user.id):
        left = get_plus_time_left(message.from_user.id)
        sub_lines.append(f"💎 <b>PLUS</b> · осталось: <b>{format_time(left)}</b>")
    if has_allow(message.from_user.id):
        left = get_allow_time_left(message.from_user.id)
        sub_lines.append(f"🔓 <b>ALLOW</b> · осталось: <b>{format_time(left)}</b>")
    sub_str = "\n" + "\n".join(sub_lines) if sub_lines else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="promo:enter")],
        [InlineKeyboardButton(text="🔗 Пригласить друга", callback_data="ref:invite")],
        [InlineKeyboardButton(text="🏆 Топ недели", callback_data="top:view:0")],
        [InlineKeyboardButton(text="📃 Соглашение", callback_data="show_agreement")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
    ])
    await message.answer(
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 <code>{message.from_user.id}</code>\n"
        f"📛 {message.from_user.full_name}\n"
        f"📅 С нами с: {joined_str}\n"
        f"💰 Баланс: <b>{balance}✈️</b>\n"
        f"🔥 Стрик: <b>{streak}</b> дн.\n"
        f"📥 Скачано: <b>{downloads}</b>\n"
        f"📊 За неделю: <b>{week_views}</b>\n"
        f"👥 Приглашено: <b>{invited}</b>\n"
        f"🔓 {status}{sub_str}",
        parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "ref:invite")
async def referral_menu(callback: types.CallbackQuery, bot: Bot):
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=ref_{callback.from_user.id}"
    invited = get_invited_count(callback.from_user.id)
    bonus = get_referral_bonus()
    text = (f"🔗 <b>Пригласить друга</b>\n\n"
            f"Твоя ссылка:\n<code>{link}</code>\n\n"
            f"За каждого друга — <b>+{bonus}✈️</b>\n\n"
            f"👥 Уже приглашено: <b>{invited}</b>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться",
                              url=f"https://t.me/share/url?url={link}&text=Заходи!")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "show_agreement")
async def show_agreement_cb(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")]
    ])
    await callback.message.answer(AGREEMENT_TEXT, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# =========================================================
# PLUS / ALLOW
# =========================================================
@dp.callback_query(F.data == "plus:menu")
async def plus_menu(callback: types.CallbackQuery):
    uid = callback.from_user.id
    price = get_plus_price()
    h_bonus = get_plus_hourly_bonus()
    d_bonus = get_plus_daily_bonus()
    mult = get_plus_discount_mult()
    days = get_plus_days()
    active = has_plus(uid)
    left = get_plus_time_left(uid) if active else 0
    text = (f"💎 <b>PLUS подписка</b>\n\n"
            f"💰 Цена: <b>{price}✈️</b>\n⏳ Срок: <b>{days} дней</b>\n\n"
            f"<b>Что даёт:</b>\n"
            f"🕐 +{h_bonus}✈️ к ежечасному бонусу\n"
            f"📅 +{d_bonus}✈️ к ежедневному бонусу\n"
            f"🛒 Скидка ÷{mult} на всё в каталоге\n")
    if active:
        text += f"\n✅ <b>У тебя активен PLUS</b>\n⏳ Осталось: <b>{format_time(left)}</b>"
    balance = get_balance(uid)
    text += f"\n\n💳 Баланс: <b>{balance}✈️</b>"
    rows = []
    if active:
        rows.append([InlineKeyboardButton(text=f"🔄 Продлить за {price}✈️",
                                          callback_data="plus:buy")])
    else:
        rows.append([InlineKeyboardButton(text=f"💎 Купить за {price}✈️",
                                          callback_data="plus:buy")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")])
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@dp.callback_query(F.data == "plus:buy")
async def plus_buy(callback: types.CallbackQuery, bot: Bot):
    uid = callback.from_user.id
    price = get_plus_price()
    if not spend_balance(uid, price, "💎 Покупка PLUS"):
        await callback.answer(f"❌ Не хватает ✈️ (нужно {price})", show_alert=True)
        return
    new_exp = buy_plus(uid, price_paid=price)
    await callback.answer("✅ PLUS активирован!")
    await send_log(
        f"💎 <b>Покупка PLUS</b>\n"
        f"👤 @{callback.from_user.username or 'нет'} (<code>{uid}</code>)\n"
        f"💰 -{price}✈️\n📅 До: {new_exp.strftime('%d.%m.%Y %H:%M')}", bot)
    try:
        await callback.message.edit_text(
            f"💎 <b>PLUS активирован!</b>\n\n"
            f"⏳ До: <b>{new_exp.strftime('%d.%m.%Y %H:%M')}</b>\n"
            f"💳 Баланс: <b>{get_balance(uid)}✈️</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
            ]))
    except Exception:
        pass


@dp.callback_query(F.data == "allow:menu")
async def allow_menu(callback: types.CallbackQuery):
    uid = callback.from_user.id
    price = get_allow_price()
    days = get_allow_days()
    active = has_allow(uid)
    left = get_allow_time_left(uid) if active else 0
    text = (f"🔓 <b>ALLOW подписка</b>\n\n"
            f"💰 Цена: <b>{price}✈️</b>\n⏳ Срок: <b>{days} дней</b>\n\n"
            f"<b>Что даёт:</b>\n"
            f"🔓 Доступ ко <b>ВСЕМ</b> категориям\n"
            f"🎬 Все видео бесплатно\n")
    if active:
        text += f"\n✅ <b>ALLOW активен</b>\n⏳ Осталось: <b>{format_time(left)}</b>"
    balance = get_balance(uid)
    text += f"\n\n💳 Баланс: <b>{balance}✈️</b>"
    rows = []
    if active:
        rows.append([InlineKeyboardButton(text=f"🔄 Продлить за {price}✈️",
                                          callback_data="allow:buy")])
    else:
        rows.append([InlineKeyboardButton(text=f"🔓 Купить за {price}✈️",
                                          callback_data="allow:buy")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")])
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@dp.callback_query(F.data == "allow:buy")
async def allow_buy(callback: types.CallbackQuery, bot: Bot):
    uid = callback.from_user.id
    price = get_allow_price()
    if not spend_balance(uid, price, "🔓 Покупка ALLOW"):
        await callback.answer(f"❌ Не хватает ✈️ (нужно {price})", show_alert=True)
        return
    new_exp = buy_allow(uid, price_paid=price)
    await callback.answer("✅ ALLOW активирован!")
    await send_log(
        f"🔓 <b>Покупка ALLOW</b>\n"
        f"👤 @{callback.from_user.username or 'нет'} (<code>{uid}</code>)\n"
        f"💰 -{price}✈️\n📅 До: {new_exp.strftime('%d.%m.%Y %H:%M')}", bot)
    try:
        await callback.message.edit_text(
            f"🔓 <b>ALLOW активирован!</b>\n\n"
            f"⏳ До: <b>{new_exp.strftime('%d.%m.%Y %H:%M')}</b>\n"
            f"💳 Баланс: <b>{get_balance(uid)}✈️</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
            ]))
    except Exception:
        pass


# =========================================================
# ПРОМОКОД (кнопка) — с активациями + ИЗМЕНЕНО: send_log
# =========================================================
@dp.callback_query(F.data == "promo:enter")
async def promo_enter(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🎟 <b>Введите промокод</b>\n\nОтправь код сообщением.",
        parse_mode="HTML")
    await state.set_state(UserStates.waiting_promo)
    await callback.answer()


@dp.message(UserStates.waiting_promo)
async def promo_process(message: types.Message, state: FSMContext, bot: Bot):
    code = (message.text or "").strip()
    if not code:
        await message.answer("⚠️ Введи код.")
        return
    ok, msg, kind, reward, priv, uses, max_uses = try_activate_any_promo(message.from_user.id, code)
    await state.clear()
    mu_str = f"{uses}/{max_uses}" if max_uses > 0 else f"{uses}/∞"

    if ok:
        if kind == "balance":
            new_bal = get_balance(message.from_user.id)
            await message.answer(
                f"{msg}\n👥 Активаций: <b>{mu_str}</b>\n"
                f"💳 Баланс: <b>{new_bal}✈️</b>",
                parse_mode="HTML",
                reply_markup=main_kb(message.from_user.id))
            await send_log(
                f"🎟 <b>Промокод (баланс)</b>\n"
                f"👤 @{message.from_user.username or 'нет'} "
                f"({message.from_user.full_name or '—'})\n"
                f"🆔 <code>{message.from_user.id}</code>\n"
                f"🎫 <code>{code}</code>\n"
                f"💰 +{reward}✈️\n"
                f"👥 Активаций: <b>{mu_str}</b>", bot)
        else:
            priv_type, days = priv
            if priv_type == "plus":
                admin_grant_plus(message.from_user.id, days)
                exp = get_plus_time_left(message.from_user.id)
            else:
                admin_grant_allow(message.from_user.id, days)
                exp = get_allow_time_left(message.from_user.id)
            await message.answer(
                f"{msg}\n👥 Активаций: <b>{mu_str}</b>\n"
                f"⏳ Осталось: <b>{format_time(exp)}</b>",
                parse_mode="HTML",
                reply_markup=main_kb(message.from_user.id))
            await send_log(
                f"🎟 <b>Промокод ({priv_type.upper()})</b>\n"
                f"👤 @{message.from_user.username or 'нет'} "
                f"({message.from_user.full_name or '—'})\n"
                f"🆔 <code>{message.from_user.id}</code>\n"
                f"🎫 <code>{code}</code>\n"
                f"⏳ +{days} дней\n"
                f"👥 Активаций: <b>{mu_str}</b>", bot)
    else:
        tail = f"\n👥 Активаций: <b>{mu_str}</b>" if (max_uses > 0 or uses > 0) else ""
        await message.answer(msg + tail, parse_mode="HTML",
                             reply_markup=main_kb(message.from_user.id))


# =========================================================
# ТОП
# =========================================================
@dp.callback_query(F.data.startswith("top:view:"))
async def top_view(callback: types.CallbackQuery):
    top = get_top_week(3)
    week_start = get_week_start().strftime("%d.%m")
    my_views = get_user_week_views(callback.from_user.id)
    my_pos = get_user_week_position(callback.from_user.id)
    r1 = get_top_reward(1)
    r2 = get_top_reward(2)
    r3 = get_top_reward(3)
    text = f"🏆 <b>Топ недели</b>\n<i>с {week_start}</i>\n\n"
    if not top:
        text += "Пока никто не смотрел видео на этой неделе."
    else:
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, uname, fname, banned, cnt) in enumerate(top, 1):
            mark = medals[i-1] if i <= 3 else f"{i}."
            name = fname or "Без имени"
            ban = " 🚫" if banned else ""
            marks_str = ""
            if has_allow(uid):
                marks_str += " 🔓"
            elif has_plus(uid):
                marks_str += " 💎"
            you = " ← ты" if uid == callback.from_user.id else ""
            text += f"{mark} {name}{marks_str} — <b>{cnt}</b>{ban}{you}\n"
    in_top = any(uid == callback.from_user.id for uid, *_ in top)
    if my_pos and not in_top:
        text += f"\n...\n📍 <b>Ты</b> — <b>{my_views}</b> (место #{my_pos})\n"
    text += (f"\n🎁 <b>Награды:</b>\n"
             f"🥇 {r1}✈️ · 🥈 {r2}✈️ · 🥉 {r3}✈️")
    rows = [[InlineKeyboardButton(text="🔄 Обновить", callback_data="top:view:0")]]
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")])
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


# =========================================================
# ЗАДАНИЯ (юзер)
# =========================================================
async def render_tasks_menu(target_message, state, uid):
    await state.clear()
    tasks = get_tasks(active_only=True)
    if not tasks:
        try:
            await target_message.edit_text(
                "📝 Пока нет заданий.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💰 К заработку", callback_data="earn:back")],
                    [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
                ]))
        except Exception:
            await target_message.answer("📝 Пока нет заданий.",
                                         reply_markup=main_kb(uid))
        return
    visible = []
    for t in tasks:
        tid, name, desc, emoji, reward, mtype, mcount, repeat = t
        status = user_completed_task(uid, tid)
        if not repeat and status == "approved":
            continue
        if not repeat and status == "pending":
            continue
        visible.append(t)
    if not visible:
        try:
            await target_message.edit_text(
                "✅ Все задания выполнены!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💰 К заработку", callback_data="earn:back")],
                    [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
                ]))
        except Exception:
            await target_message.answer("✅ Все задания выполнены!",
                                         reply_markup=main_kb(uid))
        return
    buttons = []
    for tid, name, desc, emoji, reward, mtype, mcount, repeat in visible:
        progress, _ = get_task_progress(uid, tid)
        cnt = len(progress)
        status_mark = f" ({cnt}/{mcount})" if 0 < cnt < mcount else ""
        buttons.append([InlineKeyboardButton(
            text=f"{emoji} {name} — {reward}✈️{status_mark}",
            callback_data=f"task:view:{tid}")])
    buttons.append([InlineKeyboardButton(text="💰 К заработку", callback_data="earn:back")])
    buttons.append([InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await target_message.edit_text(
            f"📝 <b>Задания</b>\n\nВыполняй и получай ✈️!",
            parse_mode="HTML", reply_markup=kb)
    except Exception:
        await target_message.answer(
            f"📝 <b>Задания</b>\n\nВыполняй и получай ✈️!",
            parse_mode="HTML", reply_markup=kb)


@dp.message(F.text == "📝 Задания")
async def tasks_menu(message: types.Message, state: FSMContext):
    await render_tasks_menu(message, state, message.from_user.id)


@dp.callback_query(F.data == "goto_tasks")
async def goto_tasks(callback: types.CallbackQuery, state: FSMContext):
    await render_tasks_menu(callback.message, state, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data.startswith("task:view:"))
async def task_view(callback: types.CallbackQuery):
    tid = int(callback.data.split(":")[2])
    task = get_task(tid)
    if not task:
        await callback.answer("Не найдено", show_alert=True)
        return
    _, name, desc, emoji, reward, mtype, mcount, repeat, active = task
    if not active:
        await callback.answer("Отключено", show_alert=True)
        return
    status = user_completed_task(callback.from_user.id, tid)
    if not repeat and status in ("approved", "pending"):
        await callback.answer("Уже отправлено", show_alert=True)
        return
    progress, _ = get_task_progress(callback.from_user.id, tid)
    cnt = len(progress)
    type_label = {"photo": "📸 фото", "video": "📹 видео", "both": "📸+📹"}.get(mtype, "📎")
    text = (f"{emoji} <b>{name}</b>\n\n{desc or '—'}\n\n"
            f"💰 Награда: <b>{reward}✈️</b>\n"
            f"📥 Принимаем: <b>{type_label}</b>\n"
            f"🔢 Нужно: <b>{mcount}</b> шт.\n"
            f"📊 Прогресс: <b>{cnt}/{mcount}</b>")
    if 0 < cnt < mcount:
        rows = [[InlineKeyboardButton(text="📤 Продолжить", callback_data=f"task:continue:{tid}")],
                [InlineKeyboardButton(text="❌ Отменить", callback_data=f"task:cancel:{tid}")]]
    elif cnt >= mcount:
        rows = [[InlineKeyboardButton(text="✅ Отправить", callback_data=f"task:submit:{tid}")],
                [InlineKeyboardButton(text="❌ Отменить", callback_data=f"task:cancel:{tid}")]]
    else:
        rows = [[InlineKeyboardButton(text="🚀 Выполнить", callback_data=f"task:start:{tid}")]]
    rows.append([InlineKeyboardButton(text="💰 К заработку", callback_data="earn:back")])
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@dp.callback_query(F.data.startswith("task:start:"))
async def task_start(callback: types.CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":")[2])
    task = get_task(tid)
    if not task:
        await callback.answer("Не найдено", show_alert=True)
        return
    _, name, desc, emoji, reward, mtype, mcount, repeat, active = task
    await state.set_state(TaskStates.sending_files)
    await state.update_data(task_id=tid)
    progress, _ = get_task_progress(callback.from_user.id, tid)
    cnt = len(progress)
    type_label = {"photo": "📸 фото", "video": "📹 видео", "both": "📸 или 📹"}.get(mtype, "📎")
    await callback.message.answer(
        f"📤 <b>Отправка файлов</b>\n\n{emoji} {name}\n"
        f"📥 Принимаю: <b>{type_label}</b>\n"
        f"🔢 Нужно: <b>{mcount}</b>\n"
        f"📊 Прогресс: <b>{cnt}/{mcount}</b>\n\n"
        f"Когда наберёшь {mcount} — появится «✅ Отправить».",
        parse_mode="HTML", reply_markup=task_sending_kb(tid, cnt, mcount))
    await callback.answer()


@dp.callback_query(F.data.startswith("task:continue:"))
async def task_continue(callback: types.CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":")[2])
    task = get_task(tid)
    if not task:
        await callback.answer("Не найдено", show_alert=True)
        return
    _, name, desc, emoji, reward, mtype, mcount, repeat, active = task
    await state.set_state(TaskStates.sending_files)
    await state.update_data(task_id=tid)
    progress, _ = get_task_progress(callback.from_user.id, tid)
    cnt = len(progress)
    await callback.message.answer(
        f"📤 «{name}»\n📊 <b>{cnt}/{mcount}</b>",
        parse_mode="HTML", reply_markup=task_sending_kb(tid, cnt, mcount))
    await callback.answer()


@dp.message(TaskStates.sending_files, F.photo | F.video)
async def task_receive_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("task_id")
    task = get_task(tid)
    if not task:
        await state.clear()
        await message.answer("⚠️ Задание удалено.")
        return
    _, name, desc, emoji, reward, mtype, mcount, repeat, active = task
    if message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"
    elif message.video:
        file_id, file_type = message.video.file_id, "video"
    else:
        await message.answer("⚠️ Только фото/видео.")
        return
    if mtype == "photo" and file_type != "photo":
        await message.answer("⚠️ Нужно фото.")
        return
    if mtype == "video" and file_type != "video":
        await message.answer("⚠️ Нужно видео.")
        return
    files, _ = get_task_progress(message.from_user.id, tid)
    if len(files) >= mcount:
        await message.answer(f"⚠️ Хватит! {mcount}/{mcount}.")
        return
    new_count = add_task_file(message.from_user.id, tid, file_id, file_type)
    kb = task_sending_kb(tid, new_count, mcount)
    if new_count < mcount:
        await message.answer(f"📥 Прогресс: <b>{new_count}/{mcount}</b>",
                             parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(f"🎉 <b>{mcount}/{mcount}</b>!\nНажми «✅ Отправить».",
                             parse_mode="HTML", reply_markup=kb)


@dp.message(TaskStates.sending_files)
async def task_receive_wrong(message: types.Message):
    await message.answer("⚠️ Отправь фото/видео или отмени.")


@dp.callback_query(F.data.startswith("task:cancel:"))
async def task_cancel(callback: types.CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":")[2])
    clear_task_progress(callback.from_user.id, tid)
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_text(
            "❌ Задание отменено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 К заданиям", callback_data="goto_tasks")],
            ]))
    except Exception:
        pass


@dp.callback_query(F.data.startswith("task:submit:"))
async def task_submit(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    tid = int(callback.data.split(":")[2])
    task = get_task(tid)
    if not task:
        await callback.answer("Не найдено", show_alert=True)
        return
    _, name, desc, emoji, reward, mtype, mcount, repeat, active = task
    files, _ = get_task_progress(callback.from_user.id, tid)
    if len(files) < mcount:
        await callback.answer(f"❌ Нужно ещё {mcount - len(files)}", show_alert=True)
        return
    user = callback.from_user
    info = (f"📝 <b>Новое выполнение!</b>\n\n"
            f"👤 @{user.username or 'нет'} (<code>{user.id}</code>)\n"
            f"{emoji} <b>{name}</b>\n"
            f"💰 <b>{reward}✈️</b>\n"
            f"📎 Файлов: {len(files)}")
    try:
        msg = await bot.send_message(ADMIN_ID, info, parse_mode="HTML")
    except Exception as e:
        log.error(f"Task submit info: {e}")
        await callback.answer("⚠️ Ошибка отправки админу", show_alert=True)
        return
    sent_files = 0
    for i, fid in enumerate(files, 1):
        try:
            if mtype == "photo":
                await bot.send_photo(ADMIN_ID, fid, caption=f"#{i}/{len(files)}")
            elif mtype == "video":
                await bot.send_video(ADMIN_ID, fid, caption=f"#{i}/{len(files)}")
            else:
                try:
                    await bot.send_photo(ADMIN_ID, fid, caption=f"#{i}/{len(files)}")
                except Exception:
                    await bot.send_video(ADMIN_ID, fid, caption=f"#{i}/{len(files)}")
            sent_files += 1
        except Exception as e:
            log.error(f"Task file {i}: {type(e).__name__}: {e}")
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ Файл #{i} не переслать: {e}")
            except Exception:
                pass
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить",
                              callback_data=f"task:approve:{tid}:{user.id}"),
         InlineKeyboardButton(text="❌ Отклонить",
                              callback_data=f"task:reject:{tid}:{user.id}")],
    ])
    try:
        await bot.send_message(ADMIN_ID,
                                f"⬆️ Решение ({sent_files}/{len(files)}):",
                                reply_markup=kb)
        create_task_completion(user.id, tid, msg.message_id)
    except Exception as e:
        log.error(f"Task submit kbd: {e}")
    await state.clear()
    await callback.answer("✅ Отправлено!")
    try:
        await callback.message.edit_text(
            "✅ Задание отправлено на проверку!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 К заданиям", callback_data="goto_tasks")],
            ]))
    except Exception:
        pass


# =========================================================
# ПРЕДЛОЖКА
# =========================================================
@dp.callback_query(F.data == "earn:suggest")
async def suggest_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.suggest_video)
    await state.update_data(suggest_files=[])
    text = (f"📹 <b>Предложить видео</b>\n\n"
            f"Отправь одно или несколько видео.\n"
            f"За одобренное видео админ начислит ✈️.\n\n"
            f"<b>Правила:</b>\n"
            f"• Только видео\n"
            f"• Без спама и рекламы\n"
            f"• От 1 до {SUGGEST_MAX} видео")
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                              [InlineKeyboardButton(text="💰 К заработку", callback_data="earn:back")],
                                          ]))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@dp.message(UserStates.suggest_video, F.video)
async def suggest_receive(message: types.Message, state: FSMContext):
    data = await state.get_data()
    files = data.get("suggest_files", [])
    if len(files) >= SUGGEST_MAX:
        await message.answer(
            f"⚠️ Максимум {SUGGEST_MAX} видео.\n"
            f"Жми «✅ Отправить» или «❌ Отменить».",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Отправить ({len(files)})",
                                      callback_data="suggest:submit")],
                [InlineKeyboardButton(text="❌ Отменить", callback_data="suggest:cancel")],
            ]))
        return
    files.append(message.video.file_id)
    await state.update_data(suggest_files=files)
    await message.answer(
        f"📥 Принято: <b>{len(files)}/{SUGGEST_MAX}</b>\n\n"
        f"Отправь ещё или подожди 5 секунд",
        parse_mode="HTML")
    asyncio.create_task(suggest_timer(message.from_user.id, message.chat.id, state))


async def suggest_timer(user_id: int, chat_id: int, state: FSMContext):
    await asyncio.sleep(5)
    data = await state.get_data()
    files = data.get("suggest_files", [])
    if not files:
        return
    current = await state.get_state()
    if current != UserStates.suggest_video.state:
        return
    try:
        await bot.send_message(
            chat_id,
            f"📤 <b>Готово к отправке</b>\n\n"
            f"Прислано видео: <b>{len(files)}/{SUGGEST_MAX}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Отправить ({len(files)})",
                                      callback_data="suggest:submit")],
                [InlineKeyboardButton(text="❌ Отменить", callback_data="suggest:cancel")],
            ]))
    except Exception:
        pass


@dp.message(UserStates.suggest_video)
async def suggest_wrong(message: types.Message):
    await message.answer("⚠️ Принимаю только видео. Отправь видео или отмени.")


@dp.callback_query(F.data == "suggest:submit")
async def suggest_submit(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    files = data.get("suggest_files", [])
    if not files:
        await callback.answer("Нет видео", show_alert=True)
        return
    user = callback.from_user
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📹 <b>Новая предложка</b>\n\n"
            f"👤 @{user.username or 'нет'} (<code>{user.id}</code>)\n"
            f"📹 Видео: <b>{len(files)}</b>",
            parse_mode="HTML")
    except Exception as e:
        log.error(f"Suggest info: {e}")
    for i, fid in enumerate(files, 1):
        try:
            await bot.send_video(ADMIN_ID, fid, caption=f"#{i}/{len(files)}")
        except Exception as e:
            log.error(f"Suggest file {i}: {e}")
    await state.clear()
    await callback.answer("✅ Отправлено!")
    try:
        await callback.message.edit_text(
            "✅ <b>Видео отправлено на проверку!</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К заработку", callback_data="earn:back")],
                [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
            ]))
    except Exception:
        pass


@dp.callback_query(F.data == "suggest:cancel")
async def suggest_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_text(
            "❌ Отменено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К заработку", callback_data="earn:back")],
                [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
            ]))
    except Exception:
        pass


# =========================================================
# ПОДДЕРЖКА
# =========================================================
@dp.message(F.text == "🆘 Поддержка")
async def support_start(message: types.Message, state: FSMContext):
    await state.clear()
    hints = []
    if has_allow(message.from_user.id):
        hints.append("🔓 ALLOW: приоритет!")
    elif has_plus(message.from_user.id):
        hints.append("💎 PLUS: приоритет!")
    plus_hint = "\n\n" + "\n".join(hints) if hints else ""
    await message.answer(
        f"📝 <b>Напиши обращение</b> или пришли <b>фото</b>:{plus_hint}",
        parse_mode="HTML")
    await state.set_state(SupportStates.waiting_message)


@dp.message(SupportStates.waiting_message, F.photo)
async def support_message_photo(message: types.Message, state: FSMContext, bot: Bot):
    text = message.caption or ""
    user = message.from_user
    tid = create_ticket(user.id, "С фото", text or "—")
    marks = []
    if has_allow(user.id):
        marks.append("🔓 ALLOW")
    if has_plus(user.id):
        marks.append("💎 PLUS")
    plus_mark = ""
    if marks:
        plus_mark = f"\n{' · '.join(marks)}"
    admin_caption = (
        f"🆘 <b>Обращение #{tid}</b>{plus_mark}\n\n"
        f"👤 @{user.username or 'нет'} (<code>{user.id}</code>)\n"
        f"💬 {text or '—'}"
    )
    try:
        await bot.send_photo(ADMIN_ID, message.photo[-1].file_id,
                             caption=admin_caption, parse_mode="HTML")
    except Exception as e:
        log.error(f"Support photo -> admin: {e}")
        try:
            await bot.send_message(ADMIN_ID, admin_caption, parse_mode="HTML")
        except Exception:
            pass
    await state.clear()
    await message.answer("✅ Отправлено!", reply_markup=main_kb(user.id))


@dp.message(SupportStates.waiting_message)
async def support_message(message: types.Message, state: FSMContext, bot: Bot):
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("⚠️ Напиши текст или пришли фото.")
        return
    user = message.from_user
    tid = create_ticket(user.id, "Без темы", text)
    marks = []
    if has_allow(user.id):
        marks.append("🔓 ALLOW")
    if has_plus(user.id):
        marks.append("💎 PLUS")
    plus_mark = ""
    if marks:
        plus_mark = f"\n{' · '.join(marks)}"
    admin_msg = (f"🆘 <b>Обращение #{tid}</b>{plus_mark}\n\n"
                 f"👤 @{user.username or 'нет'} (<code>{user.id}</code>)\n"
                 f"💬 {text}")
    ok = False
    try:
        await bot.send_message(ADMIN_ID, admin_msg, parse_mode="HTML")
        ok = True
    except Exception as e:
        log.error(f"Support -> admin: {e}")
    await state.clear()
    if ok:
        await message.answer("✅ Отправлено!", reply_markup=main_kb(user.id))
    else:
        await message.answer("⚠️ Не удалось отправить. Попробуй позже.",
                             reply_markup=main_kb(user.id))


# =========================================================
# МАГАЗИН (юзер)
# =========================================================
@dp.message(F.text == "🏪 Магазин")
async def shop(message: types.Message):
    items = get_shop_items()
    bal = get_balance(message.from_user.id)
    plus_active = has_plus(message.from_user.id)
    allow_active = has_allow(message.from_user.id)
    plus_lines = []
    if allow_active:
        plus_lines.append("🔓 ALLOW активен — всё бесплатно!")
    elif plus_active:
        plus_lines.append("💎 PLUS активен — скидка!")
    plus_line = "\n" + "\n".join(plus_lines) if plus_lines else ""
    buttons = []
    buttons.append([InlineKeyboardButton(
        text="💰 Купить ✈️ за деньги",
        callback_data="buy_pt_menu")])
    allow_price = get_allow_price()
    allow_mark = "✅ " if allow_active else ""
    buttons.append([InlineKeyboardButton(
        text=f"{allow_mark}🔓 ALLOW — {allow_price}✈️",
        callback_data="allow:menu")])
    plus_price = get_plus_price()
    plus_mark = "✅ " if plus_active else ""
    buttons.append([InlineKeyboardButton(
        text=f"{plus_mark}💎 PLUS — {plus_price}✈️",
        callback_data="plus:menu")])
    for row in items:
        iid, name, desc, price, ctype, content, req_photo = row
        bought = user_bought_item(message.from_user.id, iid)
        mark = "✅ " if bought else ""
        if allow_active:
            price_str = "0✈️ 🔓"
        else:
            sale_price = get_plus_sale_price(price, message.from_user.id)
            price_str = f"~{price}~ {sale_price}✈️" if sale_price < price else f"{price}✈️"
        photo_mark = " 📸" if req_photo else ""
        buttons.append([InlineKeyboardButton(
            text=f"{mark}🛒 {name}{photo_mark} — {price_str}",
            callback_data=f"shop:{iid}")])
    await message.answer(
        f"🏪 <b>Магазин</b>\n💰 Баланс: {bal}✈️{plus_line}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("shop:"))
async def shop_item_view(callback: types.CallbackQuery):
    iid = int(callback.data.split(":")[1])
    item = get_shop_item(iid)
    if not item:
        await callback.answer("Не найдено", show_alert=True)
        return
    _, name, desc, price, ctype, content, req_photo = item
    bal = get_balance(callback.from_user.id)
    bought = user_bought_item(callback.from_user.id, iid)
    if has_allow(callback.from_user.id):
        sale_price = 0
        price_line = f"💰 Цена: <b>0✈️</b> 🔓 (ALLOW)"
    else:
        sale_price = get_plus_sale_price(price, callback.from_user.id)
        price_line = f"💰 Цена: <b>{sale_price}✈️</b>"
        if sale_price < price:
            price_line = f"💰 Цена: <s>{price}✈️</s> <b>{sale_price}✈️</b> 💎"
    photo_line = "\n📸 <i>Нужно фото от тебя</i>" if req_photo else ""
    text = f"🛒 <b>{name}</b>\n\n{desc or '—'}\n\n{price_line}{photo_line}\n💳 Баланс: <b>{bal}✈️</b>"
    if bought:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Снова", callback_data=f"shopget:{iid}")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
        ])
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Купить за {sale_price}✈️",
                                  callback_data=f"shopbuy:{iid}")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
        ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def deliver_shop_item(bot_instance, user_id, ctype, content, name):
    try:
        if ctype == "text":
            await bot_instance.send_message(user_id, f"🎁 {content}", parse_mode="HTML")
        elif ctype == "link":
            await bot_instance.send_message(user_id, f"🎁 <b>{name}</b>\n\n🔗 {content}", parse_mode="HTML")
        elif ctype == "media":
            await bot_instance.send_video(user_id, content, caption=f"🎁 {name}", protect_content=True)
        elif ctype == "photo":
            await bot_instance.send_photo(user_id, content, caption=f"🎁 {name}", protect_content=True)
    except Exception as e:
        log.error(f"deliver_shop_item: {e}")


@dp.callback_query(F.data.startswith("shopbuy:"))
async def shop_buy(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    iid = int(callback.data.split(":")[1])
    item = get_shop_item(iid)
    if not item:
        await callback.answer("Не найдено", show_alert=True)
        return
    _, name, desc, base_price, ctype, content, req_photo = item
    if has_allow(callback.from_user.id):
        price = 0
    else:
        price = get_plus_sale_price(base_price, callback.from_user.id)
    if price > 0 and not spend_balance(callback.from_user.id, price, f"🏪 {name}"):
        await callback.answer(f"❌ Не хватает ✈️", show_alert=True)
        return
    log_shop_purchase(callback.from_user.id, iid, price)
    await callback.answer(f"✅ -{price}✈️" if price > 0 else "🔓 Бесплатно")
    user = callback.from_user
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🛒 <b>Покупка в магазине</b>\n\n"
            f"👤 @{user.username or 'нет'} (<code>{user.id}</code>)\n"
            f"📦 <b>{name}</b>\n"
            f"💵 Цена: <b>{price}✈️</b>\n"
            f"📄 Тип: <b>{ctype}</b>\n"
            f"📸 Фото: {'требуется' if req_photo else '—'}",
            parse_mode="HTML")
    except Exception as e:
        log.error(f"Shop notify admin: {e}")
    if req_photo:
        await state.set_state(UserStates.shop_photo)
        await state.update_data(shop_iid=iid, shop_price=price, shop_name=name)
        await callback.message.answer(
            f"📸 <b>Пришли фото</b> для товара «{name}»\n\n"
            f"<i>Оно уйдёт админу вместе с заявкой.</i>",
            parse_mode="HTML")
        return
    await deliver_shop_item(bot, callback.from_user.id, ctype, content, name)
    new_bal = get_balance(callback.from_user.id)
    await callback.message.answer(f"💰 Баланс: <b>{new_bal}✈️</b>", parse_mode="HTML")


@dp.message(UserStates.shop_photo, F.photo)
async def shop_photo_receive(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    iid = data.get("shop_iid")
    price = data.get("shop_price", 0)
    item = get_shop_item(iid)
    if not item:
        await state.clear()
        await message.answer("⚠️ Товар удалён.")
        return
    _, name, desc, base_price, ctype, content, req_photo = item
    photo_id = message.photo[-1].file_id
    await state.clear()
    user = message.from_user
    info = (f"📸 <b>Фото к покупке</b>\n\n"
            f"👤 @{user.username or 'нет'} (<code>{user.id}</code>)\n"
            f"📦 <b>{name}</b>\n"
            f"💵 Цена: <b>{price}✈️</b>")
    try:
        await bot.send_photo(ADMIN_ID, photo_id, caption=info, parse_mode="HTML")
    except Exception as e:
        log.error(f"Shop photo -> admin: {e}")
    await deliver_shop_item(bot, user.id, ctype, content, name)
    new_bal = get_balance(user.id)
    await message.answer(f"💰 Баланс: <b>{new_bal}✈️</b>", parse_mode="HTML")


@dp.message(UserStates.shop_photo)
async def shop_photo_wrong(message: types.Message):
    await message.answer("⚠️ Пришли фото или /cancel")


@dp.callback_query(F.data.startswith("shopget:"))
async def shop_get_again(callback: types.CallbackQuery, bot: Bot):
    iid = int(callback.data.split(":")[1])
    item = get_shop_item(iid)
    if not item:
        await callback.answer("Не найдено", show_alert=True)
        return
    _, name, desc, price, ctype, content, req_photo = item
    await deliver_shop_item(bot, callback.from_user.id, ctype, content, name)
    await callback.answer()
    # =========================================================
# ПОКУПКА ✈️ — МЕНЮ
# =========================================================
@dp.callback_query(F.data == "buy_pt_menu")
async def buy_pt_menu(callback: types.CallbackQuery):
    crypto_line = "\n🪙 <b>Крипта — авто-зачисление через @CryptoBot</b>" if cp else ""
    text = (
        f"💰 <b>Купить ✈️</b>\n\n"
        f"💱 Курс: <b>1₽ = {PT_RATE_RUB}✈️</b>\n"
        f"📦 Минимум: <b>{PT_MIN}✈️</b>"
        f"{crypto_line}\n\n"
        f"Выбери способ оплаты:"
    )
    rows = []
    if cp:
        rows.append([InlineKeyboardButton(text="🪙 Крипта (авто)",
                                          callback_data="buy_pt:crypto")])
    rows.append([InlineKeyboardButton(text="💳 Рубли (написать админу)",
                                      callback_data="buy_pt:rub")])
    rows.append([InlineKeyboardButton(text="⭐ Звёзды (написать админу)",
                                      callback_data="buy_pt:stars")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "buy_pt:crypto")
async def buy_pt_crypto(callback: types.CallbackQuery, state: FSMContext):
    if not cp:
        await callback.answer("⚠️ Крипта временно недоступна", show_alert=True)
        return
    await state.set_state(UserStates.buy_pt_amount)
    await state.update_data(buy_method="crypto")
    await callback.message.answer(
        "🪙 <b>Пополнение через CryptoBot</b>\n\n"
        "Оплата в USDT. 100✈️ = 1 USDT\n\n"
        f"Введи сколько ✈️ хочешь купить (от {PT_MIN}):\n"
        "<i>Пример: 500</i>\n\n"
        "✅ Зачисление <b>автоматическое</b>",
        parse_mode="HTML")
    await callback.answer()


@dp.message(UserStates.buy_pt_amount)
async def buy_pt_amount(message: types.Message, state: FSMContext, bot: Bot):
    try:
        amount = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Введи число.")
        return
    if amount < PT_MIN:
        await message.answer(f"⚠️ Минимум {PT_MIN}✈️.")
        return
    data = await state.get_data()
    method = data.get("buy_method")
    await state.clear()
    if method == "crypto":
        await create_crypto_invoice_msg(message, amount, bot)


async def create_crypto_invoice_msg(message: types.Message, amount_pt: int, bot: Bot):
    uid = message.from_user.id
    if not cp:
        await message.answer("⚠️ Крипта временно недоступна.")
        return
    amount_usdt = round(amount_pt / 100, 2)
    invoice = await cp_create_invoice(uid, amount_pt, amount_usdt)
    if not invoice:
        await message.answer("❌ Не удалось создать счёт. Попробуй позже.")
        return
    create_crypto_topup(uid, str(invoice.invoice_id), amount_pt, amount_usdt)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪙 Оплатить", url=invoice.bot_invoice_url)],
        [InlineKeyboardButton(text="🔄 Проверить",
                              callback_data=f"check_crypto:{invoice.invoice_id}")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
    ])
    await message.answer(
        f"🪙 <b>Счёт на {amount_pt}✈️</b>\n\n"
        f"💵 К оплате: <b>{amount_usdt} USDT</b>\n"
        f"💰 Баланс после: <b>{get_balance(uid) + amount_pt}✈️</b>\n\n"
        f"Оплати по кнопке.\n"
        f"⏳ Счёт действует 1 час.\n\n"
        f"<b>Зачисление автоматическое.</b> Если не пришло — нажми «🔄 Проверить».",
        parse_mode="HTML",
        reply_markup=kb)


@dp.callback_query(F.data.startswith("check_crypto:"))
async def check_crypto(callback: types.CallbackQuery, bot: Bot):
    invoice_id = callback.data.split(":")[1]
    topup = get_crypto_topup(invoice_id)
    if not topup:
        await callback.answer("Счёт не найден", show_alert=True)
        return
    _, uid, amount_pt, status = topup
    if status == "paid":
        await callback.answer("✅ Уже оплачено", show_alert=True)
        return
    inv = await cp_get_invoice(invoice_id)
    if not inv:
        await callback.answer("Ошибка проверки", show_alert=True)
        return
    if inv.status == "paid":
        if mark_crypto_topup_paid(invoice_id):
            add_balance(uid, amount_pt, "🪙 Крипта")
            new_bal = get_balance(uid)
            try:
                await callback.message.edit_text(
                    f"✅ <b>Оплачено!</b>\n\n"
                    f"💰 +{amount_pt}✈️\n"
                    f"💳 Баланс: <b>{new_bal}✈️</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🏠 В меню", callback_data="m:home")],
                    ]))
            except Exception:
                pass
            await send_log(
                f"💰 Crypto пополнение\n"
                f"👤 <code>{uid}</code>\n"
                f"💵 +{amount_pt}✈️", bot)
    else:
        await callback.answer("⏳ Ещё не оплачен", show_alert=True)


@dp.callback_query(F.data == "buy_pt:rub")
async def buy_pt_rub(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.manual_topup_amount)
    await state.update_data(manual_method="rub")
    await callback.message.answer(
        f"💳 <b>Пополнение за рубли</b>\n\n"
        f"💱 Курс: <b>1₽ = {PT_RATE_RUB}✈️</b>\n"
        f"📦 Минимум: <b>{PT_MIN}✈️</b>\n\n"
        f"Введи сколько ✈️ хочешь купить:\n"
        f"<i>Например: 500</i>",
        parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "buy_pt:stars")
async def buy_pt_stars(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.manual_topup_amount)
    await state.update_data(manual_method="stars")
    await callback.message.answer(
        f"⭐ <b>Пополнение за звёзды</b>\n\n"
        f"💱 Курс: <b>1⭐ = {PT_RATE_STAR}✈️</b>\n"
        f"📦 Минимум: <b>{PT_MIN}✈️</b>\n\n"
        f"📤 <b>Отправь звёзды на аккаунт:</b> <code>@{STARS_RECEIVER}</code>\n"
        f"⏳ <b>Выдача валюты — до {STARS_WAIT_HOURS} часов</b> после отправки.\n\n"
        f"Введи сколько ✈️ хочешь купить:\n"
        f"<i>Например: 500</i>",
        parse_mode="HTML")
    await callback.answer()


@dp.message(UserStates.manual_topup_amount)
async def manual_topup_amount(message: types.Message, state: FSMContext, bot: Bot):
    try:
        amount = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Введи число.")
        return
    if amount < PT_MIN:
        await message.answer(f"⚠️ Минимум {PT_MIN}✈️.")
        return
    data = await state.get_data()
    method = data.get("manual_method", "rub")
    await state.clear()
    uid = message.from_user.id
    user = message.from_user
    method_label = "💳 Рубли" if method == "rub" else "⭐ Звёзды"
    rate = PT_RATE_RUB if method == "rub" else PT_RATE_STAR
    amount_pay = round(amount / rate, 2)
    pay_label = f"{amount_pay}₽" if method == "rub" else f"{amount_pay}⭐"
    tid = create_ticket(uid, f"Пополнение {method_label}",
                        f"Хочет {amount}✈️ за {pay_label}")
    if method == "stars":
        admin_msg = (
            f"💰 <b>Заявка на пополнение #{tid}</b>\n\n"
            f"👤 @{user.username or 'нет'} (<code>{uid}</code>)\n"
            f"💳 Способ: <b>{method_label}</b>\n"
            f"💰 Сумма: <b>{amount}✈️</b>\n"
            f"💵 К оплате: <b>{pay_label}</b>\n"
            f"📤 Получатель: <code>@{STARS_RECEIVER}</code>\n\n"
            f"💬 <b>Ответь реплаем на это сообщение</b> — юзер получит ответ."
        )
        user_text = (
            f"✅ <b>Заявка отправлена!</b>\n\n"
            f"💳 Способ: <b>{method_label}</b>\n"
            f"💰 Сумма: <b>{amount}✈️</b>\n"
            f"💵 К оплате: <b>{pay_label}</b>\n\n"
            f"📤 <b>Отправь звёзды на:</b> <code>@{STARS_RECEIVER}</code>\n"
            f"⏳ <b>Зачисление — до {STARS_WAIT_HOURS} часов</b> после отправки.\n\n"
            f"<i>Админ проверит платёж и начислит ✈️.</i>"
        )
    else:
        admin_msg = (
            f"💰 <b>Заявка на пополнение #{tid}</b>\n\n"
            f"👤 @{user.username or 'нет'} (<code>{uid}</code>)\n"
            f"💳 Способ: <b>{method_label}</b>\n"
            f"💰 Сумма: <b>{amount}✈️</b>\n"
            f"💵 К оплате: <b>{pay_label}</b>\n\n"
            f"💬 <b>Ответь реплаем на это сообщение</b> — юзер получит ответ."
        )
        user_text = (
            f"✅ <b>Заявка отправлена админу!</b>\n\n"
            f"💳 Способ: <b>{method_label}</b>\n"
            f"💰 Сумма: <b>{amount}✈️</b>\n"
            f"💵 К оплате: <b>{pay_label}</b>\n\n"
            f"⏳ Жди ответа — админ напишет тебе реквизиты."
        )
    try:
        await bot.send_message(ADMIN_ID, admin_msg, parse_mode="HTML")
    except Exception as e:
        log.error(f"Manual topup send: {e}")
        await message.answer("⚠️ Не удалось отправить. Попробуй позже.",
                             reply_markup=main_kb(uid))
        return
    await message.answer(user_text, parse_mode="HTML", reply_markup=main_kb(uid))


# =========================================================
# REPLY АДМИНА
# =========================================================
@dp.message(F.reply_to_message, F.from_user.id == ADMIN_ID)
async def admin_reply_handler(message: types.Message, bot: Bot):
    orig = message.reply_to_message
    if not orig or not orig.text:
        return
    uid = None
    m = re.search(r"<code>(\d+)</code>", orig.text)
    if m:
        uid = int(m.group(1))
    if not uid:
        m = re.search(r"\((\d+)\)", orig.text)
        if m:
            uid = int(m.group(1))
    if not uid:
        return
    try:
        await bot.send_message(
            uid, f"💬 <b>Ответ админа:</b>\n\n{message.text}",
            parse_mode="HTML")
        await message.answer("✅ Отправлено юзеру")
    except TelegramForbiddenError:
        await message.answer("❌ Юзер заблокировал бота")
    except Exception as e:
        await message.answer(f"❌ {e}")


# =========================================================
# ПРИЁМ ПЕРЕСЛАННЫХ ИЗ КАНАЛА
# =========================================================
@dp.message(F.from_user.id == ADMIN_ID, F.forward_origin | F.forward_from_chat)
async def admin_forward_from_channel(message: types.Message):
    ok, cat_id, reason = await save_forwarded_from_channel(bot, message)
    if ok:
        await message.answer(
            f"✅ <b>Сохранено!</b>\n📂 Категория ID: <code>{cat_id}</code>",
            parse_mode="HTML")
    else:
        if reason == "no_category":
            await message.answer(
                "⚠️ Канал не привязан ни к одной категории.\n\n"
                "Сначала создай категорию с этим каналом.",
                parse_mode="HTML")
        elif reason == "exists":
            await message.answer("ℹ️ Это видео уже есть в БД.")
        elif reason == "not_media":
            await message.answer("⚠️ Принимаю только видео/фото.")
        elif reason == "no_msg_id":
            await message.answer("⚠️ Не удалось определить ID сообщения.")
        else:
            await message.answer(f"⚠️ Не сохранил ({reason}).")


# ИЗМЕНЕНО: проверка дубликатов в on_channel_post
@dp.channel_post(F.video | F.photo)
async def on_channel_post(message: types.Message):
    if message.chat.type != "channel":
        return
    channel_id = message.chat.id
    cat = get_category_by_channel(channel_id)
    if not cat:
        log.info(f"📥 Канал {channel_id}: не привязан к категории")
        return
    cat_id = cat[0]
    channel_msg_id = message.message_id
    if media_exists_in_category(cat_id, channel_id, channel_msg_id):
        return
    if message.video:
        file_type = "video"
        file_id = message.video.file_id
    elif message.photo:
        file_type = "photo"
        file_id = message.photo[-1].file_id
    else:
        return
    caption = message.caption or ""
    add_media(cat_id, channel_id, channel_msg_id, file_type,
              caption=caption, file_id=file_id)
    total = len(get_media_in_category(cat_id))
    log.info(f"📥 Канал {channel_id}: msg {channel_msg_id} → "
             f"категория {cat_id} ({cat[1]}), всего: {total}")
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📥 <b>Авто-импорт из канала</b>\n\n"
            f"📢 Канал: <code>{channel_id}</code>\n"
            f"📂 Категория: {cat[2]} <b>{cat[1]}</b>\n"
            f"📄 Msg ID: <code>{channel_msg_id}</code>\n"
            f"📦 Всего в категории: <b>{total}</b>",
            parse_mode="HTML")
    except Exception:
        pass


# =========================================================
# АДМИН-ПАНЕЛЬ
# =========================================================
@dp.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("⚙️ <b>Админ-панель</b>", parse_mode="HTML",
                         reply_markup=admin_kb())


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    if is_bot_stopped():
        await message.answer("⚠️ Бот уже на тех. перерыве.")
        return
    set_bot_stopped(True)
    await message.answer("🔧 <b>Бот остановлен</b>\n\n/restop — вернуть.",
                         parse_mode="HTML")
    await send_log("🔧 <b>Бот поставлен на тех. перерыв</b>")


@dp.message(Command("restop"))
async def cmd_restop(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    if not is_bot_stopped():
        await message.answer("⚠️ Бот и так работает.")
        return
    set_bot_stopped(False)
    await message.answer("✅ <b>Бот снова работает</b>", parse_mode="HTML")
    await send_log("✅ <b>Бот снят с тех. перерыва</b>")


@dp.callback_query(F.data == "a:close")
async def a_close(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:back")
async def a_back(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    try:
        await callback.message.edit_text("⚙️ <b>Админ-панель</b>",
                                          parse_mode="HTML", reply_markup=admin_kb())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:noop")
async def a_noop(callback: types.CallbackQuery):
    await callback.answer()


# =========================================================
# ЮЗЕРЫ (админ)
# =========================================================
@dp.callback_query(F.data == "adm:users")
async def adm_users(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список", callback_data="a:users:0"),
         InlineKeyboardButton(text="💰 Топ-10 по балансу", callback_data="a:top_balance")],
        [InlineKeyboardButton(text="🔍 Стата юзера", callback_data="a:userstat")],
        [InlineKeyboardButton(text="🚫 Бан/Разбан", callback_data="a:ban_menu"),
         InlineKeyboardButton(text="👑 Привилегии", callback_data="a:privs")],
        [InlineKeyboardButton(text="✉️ Написать юзеру", callback_data="a:dm")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:back")],
    ])
    try:
        await callback.message.edit_text("👥 <b>Юзеры</b>",
                                          parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:top_balance")
async def a_top_balance(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    rows = get_top_users_by_balance(10)
    text = "💰 <b>Топ-10 по балансу</b>\n\n"
    medals = ["🥇", "🥈", "🥉"]
    if not rows:
        text += "<i>пусто</i>"
    else:
        for i, (uid, uname, fname, bal) in enumerate(rows, 1):
            mark = medals[i-1] if i <= 3 else f"{i}."
            u = f"@{uname}" if uname else "—"
            name = fname or "—"
            text += f"{mark} <code>{uid}</code> {u} ({name}) — <b>{bal}✈️</b>\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="a:top_balance")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:users")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("a:users:"))
async def a_users_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    page = int(callback.data.split(":")[2])
    per_page = 10
    rows, total = get_users_page(page, per_page)
    tp = max(1, (total + per_page - 1) // per_page)
    text = f"👥 <b>Юзеры</b> · стр. {page+1}/{tp}\n\n"
    for uid, uname, fname, bal, banned in rows:
        mark = "🚫" if banned else "✅"
        u = f"@{uname}" if uname else "—"
        text += f"{mark} <code>{uid}</code> {u} · <b>{bal}✈️</b>\n"
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"a:users:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{tp}", callback_data="a:noop"))
    if page < tp - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"a:users:{page+1}"))
    kb_rows = [nav] if nav else []
    kb_rows.append([InlineKeyboardButton(text="🏠 Назад", callback_data="adm:users")])
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:userstat")
async def a_userstat_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("🔍 Введи <b>user_id</b>:", parse_mode="HTML")
    await state.set_state(AdminStates.stat_user_id)
    await callback.answer()


@dp.message(AdminStates.stat_user_id)
async def a_userstat_show(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    uid = int(message.text)
    s = get_user_full_stats(uid)
    if not s:
        await message.answer("❌ Не найден.")
        await state.clear()
        return
    try:
        joined = datetime.fromisoformat(s["joined_at"]).strftime("%d.%m.%Y %H:%M")
    except Exception:
        joined = s["joined_at"] or "—"
    try:
        last = datetime.fromisoformat(s["last_seen"]).strftime("%d.%m.%Y %H:%M")
    except Exception:
        last = s["last_seen"] or "—"
    ban_str = "🚫 ЗАБАНЕН" if s["banned"] else "✅ активен"

    history = get_balance_history(uid, 10)
    hist_lines = []
    if history:
        for amount, reason, admin_id, ts in history:
            sign = "+" if amount > 0 else ""
            try:
                t = datetime.fromisoformat(ts).strftime("%d.%m %H:%M")
            except Exception:
                t = "?"
            who = " (админ)" if admin_id else ""
            hist_lines.append(f"  {sign}{amount}✈️ · {reason}{who} · {t}")
        hist_str = "\n".join(hist_lines)
    else:
        hist_str = "  <i>пусто</i>"

    text = (
        f"👤 <b>Стата</b>\n\n"
        f"🆔 <code>{s['user_id']}</code>\n"
        f"📛 {s['first_name'] or '—'} (@{s['username'] or '—'})\n"
        f"📅 Регистрация: {joined}\n"
        f"🕒 Был: {last}\n"
        f"🔓 {ban_str}\n\n"
        f"💰 Баланс: <b>{s['balance']}✈️</b>\n"
        f"📥 Скачано: <b>{s['downloads']}</b>\n"
        f"👥 Рефералы: <b>{s['ref_count']}</b>\n"
        f"🎬 Куплено видео: <b>{s['media_bought']}</b>\n"
        f"📂 Куплено категорий: <b>{s['cat_bought']}</b>\n"
        f"🏪 Куплено в магазине: <b>{s['shop_bought']}</b>\n"
        f"💸 Потрачено: <b>{s['spent_media'] + s['spent_cat'] + s['spent_shop']}✈️</b>\n"
        f"📊 Просмотров (неделя): <b>{s['week_views']}</b>\n"
        f"🎟 Промокодов: <b>{s['promos_used']}</b>\n"
        f"📝 Заданий: <b>{s['tasks_done']}</b>\n\n"
        f"📜 <b>Последние 10 изменений баланса:</b>\n"
        f"{hist_str}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Другой", callback_data="a:userstat")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:users")],
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
    await state.clear()


@dp.callback_query(F.data == "a:ban_menu")
async def a_ban_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Забанить", callback_data="a:ban"),
         InlineKeyboardButton(text="✅ Разбанить", callback_data="a:unban")],
        [InlineKeyboardButton(text="🗑 Удалить юзера", callback_data="a:del_user")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:users")],
    ])
    try:
        await callback.message.edit_text("🚫 <b>Бан / Разбан</b>",
                                          parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:ban")
async def a_ban_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("🚫 Введи <b>user_id</b> для бана:",
                                   parse_mode="HTML")
    await state.set_state(AdminStates.ban_id)
    await callback.answer()


@dp.message(AdminStates.ban_id)
async def a_ban_do(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    uid = int(message.text)
    if uid == ADMIN_ID:
        await message.answer("⚠️ Себя нельзя.")
        await state.clear()
        return
    set_banned(uid, True)
    await message.answer(f"🚫 Забанен: <code>{uid}</code>", parse_mode="HTML",
                         reply_markup=admin_kb())
    try:
        await bot.send_message(uid, "🚫 Вы забанены в боте.")
    except Exception:
        pass
    await send_log(f"🚫 <b>Бан</b> <code>{uid}</code>")
    await state.clear()


@dp.callback_query(F.data == "a:unban")
async def a_unban_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("✅ Введи <b>user_id</b> для разбана:",
                                   parse_mode="HTML")
    await state.set_state(AdminStates.unban_id)
    await callback.answer()


@dp.message(AdminStates.unban_id)
async def a_unban_do(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    uid = int(message.text)
    set_banned(uid, False)
    unmark_user_blocked(uid)
    await message.answer(f"✅ Разбанен: <code>{uid}</code>", parse_mode="HTML",
                         reply_markup=admin_kb())
    try:
        await bot.send_message(uid, "✅ Вы разбанены.")
    except Exception:
        pass
    await state.clear()


@dp.callback_query(F.data == "a:del_user")
async def a_del_user_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("🗑 Введи <b>user_id</b> для удаления:",
                                   parse_mode="HTML")
    await state.set_state(AdminStates.del_user_id)
    await callback.answer()


@dp.message(AdminStates.del_user_id)
async def a_del_user_do(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    uid = int(message.text)
    if uid == ADMIN_ID:
        await message.answer("⚠️ Себя нельзя.")
        await state.clear()
        return
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()
    await message.answer(f"🗑 Удалён: <code>{uid}</code>", parse_mode="HTML",
                         reply_markup=admin_kb())
    await state.clear()


# =========================================================
# ПРИВИЛЕГИИ (админ)
# =========================================================
@dp.callback_query(F.data == "a:privs")
async def a_privs(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Список PLUS", callback_data="a:privs_plus_list"),
         InlineKeyboardButton(text="🔓 Список ALLOW", callback_data="a:privs_allow_list")],
        [InlineKeyboardButton(text="🔍 Поиск по user_id", callback_data="a:privs_search")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:users")],
    ])
    try:
        await callback.message.edit_text(
            "👑 <b>Привилегии</b>\n\nВыбери раздел:",
            parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:privs_search")
async def a_privs_search(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("👑 Введи <b>user_id</b>:", parse_mode="HTML")
    await state.set_state(AdminStates.priv_user_id)
    await callback.answer()


@dp.message(AdminStates.priv_user_id)
async def a_privs_show(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    uid = int(message.text)
    plus_active, plus_exp, allow_active, allow_exp = get_user_privileges(uid)
    plus_str = "❌ нет"
    if plus_active:
        plus_str = f"✅ до {plus_exp.strftime('%d.%m.%Y %H:%M')}"
    allow_str = "❌ нет"
    if allow_active:
        allow_str = f"✅ до {allow_exp.strftime('%d.%m.%Y %H:%M')}"
    text = (f"👑 <b>Привилегии</b> <code>{uid}</code>\n\n"
            f"💎 PLUS: {plus_str}\n"
            f"🔓 ALLOW: {allow_str}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Выдать PLUS", callback_data=f"a:grant_plus:{uid}"),
         InlineKeyboardButton(text="🔓 Выдать ALLOW", callback_data=f"a:grant_allow:{uid}")],
        [InlineKeyboardButton(text="🚫 Забрать PLUS", callback_data=f"a:revoke_plus:{uid}"),
         InlineKeyboardButton(text="🚫 Забрать ALLOW", callback_data=f"a:revoke_allow:{uid}")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:privs")],
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
    await state.clear()


@dp.callback_query(F.data == "a:privs_plus_list")
async def a_privs_plus_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    rows = get_all_plus_users()
    text = f"💎 <b>Активные PLUS</b> ({len(rows)})\n\n"
    if not rows:
        text += "<i>пусто</i>"
    else:
        for uid, exp in rows[:30]:
            try:
                exp_str = datetime.fromisoformat(exp).strftime("%d.%m %H:%M")
            except Exception:
                exp_str = "?"
            text += f"• <code>{uid}</code> — до {exp_str}\n"
        if len(rows) > 30:
            text += f"\n<i>...и ещё {len(rows) - 30}</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="a:privs_plus_list")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:privs")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:privs_allow_list")
async def a_privs_allow_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    rows = get_all_allow_users()
    text = f"🔓 <b>Активные ALLOW</b> ({len(rows)})\n\n"
    if not rows:
        text += "<i>пусто</i>"
    else:
        for uid, exp in rows[:30]:
            try:
                exp_str = datetime.fromisoformat(exp).strftime("%d.%m %H:%M")
            except Exception:
                exp_str = "?"
            text += f"• <code>{uid}</code> — до {exp_str}\n"
        if len(rows) > 30:
            text += f"\n<i>...и ещё {len(rows) - 30}</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="a:privs_allow_list")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:privs")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("a:grant_plus:"))
async def a_grant_plus(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    uid = int(callback.data.split(":")[2])
    await state.update_data(priv_uid=uid)
    await callback.message.answer("💎 На сколько <b>дней</b> выдать PLUS?",
                                   parse_mode="HTML")
    await state.set_state(AdminStates.priv_plus_days)
    await callback.answer()


@dp.message(AdminStates.priv_plus_days)
async def a_grant_plus_do(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    days = int(message.text)
    data = await state.get_data()
    uid = data.get("priv_uid")
    exp = admin_grant_plus(uid, days)
    await message.answer(
        f"✅ PLUS выдан <code>{uid}</code> до {exp.strftime('%d.%m.%Y %H:%M')}",
        parse_mode="HTML", reply_markup=admin_kb())
    try:
        await bot.send_message(uid, f"💎 Тебе выдан PLUS на {days} дней!")
    except Exception:
        pass
    await state.clear()


@dp.callback_query(F.data.startswith("a:grant_allow:"))
async def a_grant_allow(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    uid = int(callback.data.split(":")[2])
    await state.update_data(priv_uid=uid)
    await callback.message.answer("🔓 На сколько <b>дней</b> выдать ALLOW?",
                                   parse_mode="HTML")
    await state.set_state(AdminStates.priv_allow_days)
    await callback.answer()


@dp.message(AdminStates.priv_allow_days)
async def a_grant_allow_do(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    days = int(message.text)
    data = await state.get_data()
    uid = data.get("priv_uid")
    exp = admin_grant_allow(uid, days)
    await message.answer(
        f"✅ ALLOW выдан <code>{uid}</code> до {exp.strftime('%d.%m.%Y %H:%M')}",
        parse_mode="HTML", reply_markup=admin_kb())
    try:
        await bot.send_message(uid, f"🔓 Тебе выдан ALLOW на {days} дней!")
    except Exception:
        pass
    await state.clear()


@dp.callback_query(F.data.startswith("a:revoke_plus:"))
async def a_revoke_plus(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    uid = int(callback.data.split(":")[2])
    admin_revoke_plus(uid)
    await callback.answer("🚫 PLUS забран")


@dp.callback_query(F.data.startswith("a:revoke_allow:"))
async def a_revoke_allow(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    uid = int(callback.data.split(":")[2])
    admin_revoke_allow(uid)
    await callback.answer("🚫 ALLOW забран")


@dp.callback_query(F.data == "a:dm")
async def a_dm_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("✉️ Введи <b>user_id</b>:", parse_mode="HTML")
    await state.set_state(AdminStates.dm_user_id)
    await callback.answer()


@dp.message(AdminStates.dm_user_id)
async def a_dm_user(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    uid = int(message.text)
    await state.update_data(dm_uid=uid)
    await message.answer("✉️ Введи <b>текст</b> сообщения:")
    await state.set_state(AdminStates.dm_message)


@dp.message(AdminStates.dm_message)
async def a_dm_do(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    uid = data.get("dm_uid")
    try:
        await bot.send_message(
            uid,
            f"✉️ <b>ЛС от админа</b>\n\n{message.text}",
            parse_mode="HTML")
        await message.answer("✅ Отправлено", reply_markup=admin_kb())
    except Exception as e:
        await message.answer(f"❌ {e}", reply_markup=admin_kb())
    await state.clear()


# =========================================================
# ЭКОНОМИКА
# =========================================================
@dp.callback_query(F.data == "adm:economy")
async def adm_economy(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Баланс", callback_data="a:balance"),
         InlineKeyboardButton(text="🎁 Бонусы", callback_data="a:bonuses")],
        [InlineKeyboardButton(text="🏆 Топ недели", callback_data="a:top_week")],
        [InlineKeyboardButton(text="👥 Рефералка", callback_data="a:referral")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:back")],
    ])
    try:
        await callback.message.edit_text("💰 <b>Экономика</b>",
                                          parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:top_week")
async def a_top_week(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    top = get_top_week(3)
    week_start = get_week_start().strftime("%d.%m.%Y")
    rewards = {1: get_top_reward(1), 2: get_top_reward(2), 3: get_top_reward(3)}
    text = f"🏆 <b>Топ недели</b>\n<i>с {week_start}</i>\n\n"
    if not top:
        text += "📭 Пока никто не смотрел видео."
    else:
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, uname, fname, banned, cnt) in enumerate(top, 1):
            mark = medals[i-1] if i <= 3 else f"{i}."
            name = fname or "Без имени"
            reward = rewards.get(i, 0)
            text += (f"{mark} {name} — <b>{cnt}</b> просмотров "
                     f"→ <b>+{reward}✈️</b>\n")
    text += (f"\n💰 <b>Награды:</b>\n"
             f"🥇 {rewards[1]}✈️ · 🥈 {rewards[2]}✈️ · 🥉 {rewards[3]}✈️")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏁 Завершить неделю",
                              callback_data="a:top_finish")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="a:top_week")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:economy")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:top_finish")
async def a_top_finish(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer("⏳ Завершаю...", show_alert=False)
    try:
        ok, msg = await finish_week()
    except Exception as e:
        log.error(f"finish_week FAIL: {type(e).__name__}: {e}", exc_info=True)
        await callback.message.answer(f"❌ Ошибка: {e}")
        return
    await callback.message.answer(msg, parse_mode="HTML", reply_markup=admin_kb())


@dp.callback_query(F.data == "a:balance")
async def a_balance_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Действие", callback_data="a:bal_action")],
        [InlineKeyboardButton(text="📋 Шаблоны причин", callback_data="a:bal_reasons")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:economy")],
    ])
    try:
        await callback.message.edit_text("💰 <b>Баланс</b>",
                                          parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:bal_action")
async def a_bal_action(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 С причиной", callback_data="a:bal_with_reason")],
        [InlineKeyboardButton(text="⚡ Просто", callback_data="a:bal_simple")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:balance")],
    ])
    try:
        await callback.message.edit_text("💰 <b>Действие</b>",
                                          parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:bal_simple")
async def a_bal_simple(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.update_data(bal_reason="", bal_silent=False)
    await callback.message.answer(
        "⚡ <b>Просто</b>\n\n"
        "Введи: <code>user_id +100</code> или <code>user_id -100</code>\n\n"
        "<i>Пример:</i> <code>8621228825 +500</code>",
        parse_mode="HTML")
    await state.set_state(AdminStates.bal_simple_input)
    await callback.answer()


@dp.callback_query(F.data == "a:bal_with_reason")
async def a_bal_with_reason(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    reasons = get_balance_reasons()
    if not reasons:
        await callback.answer("Нет шаблонов. Сначала добавь.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for rid, reason in reasons:
        b.button(text=reason[:40],
                 callback_data=f"a:bal_pick_reason:{rid}")
    b.button(text="🏠 Назад", callback_data="a:bal_action")
    b.adjust(1)
    try:
        await callback.message.edit_text("📋 <b>Выбери причину</b>:",
                                          parse_mode="HTML",
                                          reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("a:bal_pick_reason:"))
async def a_bal_pick_reason(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    rid = int(callback.data.split(":")[2])
    r = get_balance_reason(rid)
    if not r:
        await callback.answer("Причина не найдена", show_alert=True)
        return
    reason = r[1]
    silent = reason.strip() in ("", "-", "—", "–", "−")
    await state.update_data(bal_reason=reason, bal_silent=silent)
    await callback.message.answer(
        f"📋 Причина: <b>{reason}</b>\n\n"
        f"Введи: <code>user_id +100</code> или <code>user_id -100</code>\n\n"
        f"<i>Пример:</i> <code>8621228825 +500</code>",
        parse_mode="HTML")
    await state.set_state(AdminStates.bal_simple_input)
    await callback.answer()


@dp.message(AdminStates.bal_simple_input)
async def a_bal_simple_input(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer(
            "⚠️ Формат: <code>user_id +100</code>\n"
            "Или: <code>user_id -100</code>",
            parse_mode="HTML")
        return
    uid = int(parts[0])
    amount_str = parts[1].strip()
    if amount_str.startswith("+"):
        mode = "add"
        amount_str = amount_str[1:]
    elif amount_str.startswith("-"):
        mode = "take"
        amount_str = amount_str[1:]
    else:
        await message.answer(
            "⚠️ Нужен знак: <code>+100</code> или <code>-100</code>",
            parse_mode="HTML")
        return
    if not amount_str.isdigit():
        await message.answer("⚠️ Число после знака.")
        return
    n = int(amount_str)
    if n <= 0:
        await message.answer("⚠️ Сумма должна быть > 0.")
        return

    data = await state.get_data()
    reason = data.get("bal_reason", "").strip()
    silent = data.get("bal_silent", False) or reason in ("", "-", "—", "–", "−")
    reason_line = f"\n📋 Причина: <b>{reason}</b>" if (reason and not silent) else ""

    if mode == "add":
        new_bal = add_balance(uid, n, reason or "Пополнение админом", ADMIN_ID)
        await message.answer(
            f"✅ <code>{uid}</code> <b>+{n}✈️</b>{reason_line}\n"
            f"💰 Баланс: <b>{new_bal}✈️</b>",
            parse_mode="HTML", reply_markup=admin_kb())
        if not silent:
            try:
                msg_text = f"💰 Тебе начислено <b>+{n}✈️</b>"
                if reason:
                    msg_text += f"\n📋 {reason}"
                await bot.send_message(uid, msg_text, parse_mode="HTML")
            except Exception:
                pass
    else:
        if not spend_balance(uid, n, reason or "Списание админом", ADMIN_ID):
            await message.answer("❌ Недостаточно средств.",
                                  reply_markup=admin_kb())
            await state.clear()
            return
        new_bal = get_balance(uid)
        await message.answer(
            f"✅ <code>{uid}</code> <b>-{n}✈️</b>{reason_line}\n"
            f"💰 Баланс: <b>{new_bal}✈️</b>",
            parse_mode="HTML", reply_markup=admin_kb())
        if not silent:
            try:
                await bot.send_message(
                    uid, f"💰 У тебя списано <b>-{n}✈️</b>", parse_mode="HTML")
            except Exception:
                pass
    await state.clear()


@dp.callback_query(F.data == "a:bal_reasons")
async def a_bal_reasons(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    reasons = get_balance_reasons()
    text = "📋 <b>Шаблоны причин</b>\n\n"
    if reasons:
        for rid, reason in reasons:
            text += f"{rid}. {reason}\n"
    else:
        text += "Пока пусто."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="a:br_add")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="a:br_del_list")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:balance")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:br_add")
async def a_br_add(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("📝 Введи <b>текст причины</b>:")
    await state.set_state(AdminStates.bal_reason_add)
    await callback.answer()


@dp.message(AdminStates.bal_reason_add)
async def a_br_add_do(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    ok = add_balance_reason(message.text)
    if ok:
        await message.answer("✅ Добавлено", reply_markup=admin_kb())
    else:
        await message.answer("⚠️ Уже есть или пусто", reply_markup=admin_kb())
    await state.clear()


@dp.callback_query(F.data == "a:br_del_list")
async def a_br_del_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    reasons = get_balance_reasons()
    if not reasons:
        await callback.answer("Пусто", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for rid, reason in reasons:
        b.button(text=f"🗑 {reason[:40]}", callback_data=f"a:br_del:{rid}")
    b.button(text="🏠 Назад", callback_data="a:bal_reasons")
    b.adjust(1)
    try:
        await callback.message.edit_text("Удалить:", reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("a:br_del:"))
async def a_br_del(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    rid = int(callback.data.split(":")[2])
    delete_balance_reason(rid)
    await callback.answer("🗑 Удалено")
    await a_bal_reasons(callback)


@dp.callback_query(F.data == "a:bonuses")
async def a_bonuses(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    h = get_hourly_reward()
    d = get_daily_bonus()
    text = (f"🎁 <b>Бонусы</b>\n\n"
            f"🕐 Ежечасный: <b>{h}✈️</b>\n"
            f"📅 Ежедневный: <b>{d}✈️</b>\n"
            f"🥇 Топ-1: <b>{get_top_reward(1)}✈️</b>\n"
            f"🥈 Топ-2: <b>{get_top_reward(2)}✈️</b>\n"
            f"🥉 Топ-3: <b>{get_top_reward(3)}✈️</b>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕐 Ежечасный", callback_data="a:bonus_hourly"),
         InlineKeyboardButton(text="📅 Ежедневный", callback_data="a:bonus_daily")],
        [InlineKeyboardButton(text="🏆 Топ награды", callback_data="a:bonus_top")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:economy")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:bonus_hourly")
async def a_bonus_hourly(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer(f"🕐 Текущий: <b>{get_hourly_reward()}✈️</b>\n\n"
                                   f"Введи новое:", parse_mode="HTML")
    await state.set_state(AdminStates.bonus_hourly)
    await callback.answer()


@dp.message(AdminStates.bonus_hourly)
async def a_bonus_hourly_do(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    set_setting("hourly_reward", message.text)
    await message.answer(f"✅ {message.text}✈️", reply_markup=admin_kb())
    await state.clear()


@dp.callback_query(F.data == "a:bonus_daily")
async def a_bonus_daily(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer(f"📅 Текущий: <b>{get_daily_bonus()}✈️</b>\n\n"
                                   f"Введи новое:", parse_mode="HTML")
    await state.set_state(AdminStates.bonus_daily)
    await callback.answer()


@dp.message(AdminStates.bonus_daily)
async def a_bonus_daily_do(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    set_setting("daily_bonus", message.text)
    await message.answer(f"✅ {message.text}✈️", reply_markup=admin_kb())
    await state.clear()


@dp.callback_query(F.data == "a:bonus_top")
async def a_bonus_top_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🥇 {get_top_reward(1)}✈️",
                              callback_data="a:bonus_top:1")],
        [InlineKeyboardButton(text=f"🥈 {get_top_reward(2)}✈️",
                              callback_data="a:bonus_top:2")],
        [InlineKeyboardButton(text=f"🥉 {get_top_reward(3)}✈️",
                              callback_data="a:bonus_top:3")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:bonuses")],
    ])
    try:
        await callback.message.edit_text("🏆 Выбери место:", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("a:bonus_top:"))
async def a_bonus_top_edit(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    place = int(callback.data.split(":")[2])
    await state.update_data(top_place=place)
    await callback.message.answer(f"🥇 Текущее: <b>{get_top_reward(place)}✈️</b>\n\n"
                                   f"Введи новое:", parse_mode="HTML")
    await state.set_state(AdminStates.top_reward_amount)
    await callback.answer()


@dp.message(AdminStates.top_reward_amount)
async def a_bonus_top_do(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    data = await state.get_data()
    place = data.get("top_place")
    set_top_reward(place, message.text)
    await message.answer(f"✅ {message.text}✈️", reply_markup=admin_kb())
    await state.clear()


@dp.callback_query(F.data == "a:referral")
async def a_referral(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer(
        f"👥 Текущий бонус: <b>{get_referral_bonus()}✈️</b>\n\n"
        f"Введи новое:", parse_mode="HTML")
    await state.set_state(AdminStates.ref_bonus)
    await callback.answer()


@dp.message(AdminStates.ref_bonus)
async def a_referral_do(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    set_setting("referral_bonus", message.text)
    await message.answer(f"✅ {message.text}✈️", reply_markup=admin_kb())
    await state.clear()
    # =========================================================
# СТАТИСТИКА
# =========================================================
def _build_stats_text(period: str) -> str:
    if period == "day":
        title = "🕐 Сутки"
    elif period == "week":
        title = "📅 Неделя"
    else:
        title = "♾ Всё"
    s = get_stats_period(period)
    text = f"📊 <b>Статистика</b> · {title}\n\n"
    text += (f"👥 Всего: <b>{s['users_total']}</b>\n"
             f"   Новых: <b>{s['users_new']}</b>\n"
             f"   Активных: <b>{s['users_active']}</b>\n\n"
             f"🛒 Покупки:\n"
             f"   🎬 Видео: <b>{s['media_bought']}</b>\n"
             f"   📂 Категорий: <b>{s['cat_bought']}</b>\n"
             f"   🏪 Магазин: <b>{s['shop_bought']}</b>\n\n"
             f"💰 Потрачено: <b>{s['total_spent']}✈️</b>\n\n"
             f"📥 Просмотры: <b>{s['views']}</b>\n")
    top, bottom = get_categories_ranking(period, 3)
    text += "\n🏆 <b>Топ-3 категории:</b>\n"
    if top:
        for i, (cid, name, emoji, cnt) in enumerate(top, 1):
            text += f"{i}. {emoji} <b>{name}</b> — {cnt}\n"
    else:
        text += "<i>нет данных</i>\n"
    text += "\n💀 <b>Топ-3 «бич» категории:</b>\n"
    if bottom:
        for i, (cid, name, emoji, cnt) in enumerate(bottom, 1):
            text += f"{i}. {emoji} <b>{name}</b> — {cnt}\n"
    else:
        text += "<i>нет данных</i>\n"
    return text


def _build_stats_kb(period: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("✅ " if period == "day" else "") + "🕐",
                              callback_data="a:stats:day"),
         InlineKeyboardButton(text=("✅ " if period == "week" else "") + "📅",
                              callback_data="a:stats:week"),
         InlineKeyboardButton(text=("✅ " if period == "all" else "") + "♾",
                              callback_data="a:stats:all")],
        [InlineKeyboardButton(text="🧹 Сброс",
                              callback_data=f"a:stats_reset:{period}")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:back")],
    ])


@dp.callback_query(F.data.startswith("a:stats"))
async def a_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    parts = callback.data.split(":")
    period = parts[2] if len(parts) > 2 else "all"
    text = _build_stats_text(period)
    kb = _build_stats_kb(period)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            log.error(f"a_stats edit: {e}")
    await callback.answer()


@dp.callback_query(F.data.startswith("a:stats_reset:"))
async def a_stats_reset(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    period = callback.data.split(":")[2]
    if period == "all":
        await callback.answer("♾ За всё время нельзя сбросить", show_alert=True)
        return
    try:
        ok, msg = reset_stats_period(period)
    except Exception as e:
        log.error(f"reset_stats_period FAIL: {type(e).__name__}: {e}", exc_info=True)
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)
        return
    if not ok:
        await callback.answer(f"❌ {msg}", show_alert=True)
        return
    text = "✅ <b>Статистика сброшена в 0.</b>\n\n" + _build_stats_text(period)
    kb = _build_stats_kb(period)
    try:
        await callback.message.delete()
    except Exception:
        pass
    try:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log.error(f"a_stats_reset answer: {e}")
    await callback.answer("✅ Сброшено в 0")


# =========================================================
# КОНТЕНТ
# =========================================================
@dp.callback_query(F.data == "adm:content")
async def adm_content(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📂 Категории", callback_data="a:cats"),
         InlineKeyboardButton(text="🏪 Магазин", callback_data="a:shop")],
        [InlineKeyboardButton(text="📝 Задания", callback_data="a:tasks")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:back")],
    ])
    try:
        await callback.message.edit_text("📂 <b>Контент</b>",
                                          parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


# ---- КАТЕГОРИИ ----
@dp.callback_query(F.data == "a:cats")
async def a_cats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    cats = get_categories()
    text = "📂 <b>Категории</b>\n\n"
    if cats:
        for c in cats:
            cid, name, emoji, pp, cnt, ch_id, cover = c
            full = pp * cnt
            ch_str = f"📢 <code>{ch_id}</code>" if ch_id else "❌ канал не задан"
            cover_str = "🖼" if cover else "❌"
            text += (f"{emoji} <b>{name}</b> (ID {cid}) {cover_str}\n"
                     f"   💰 {pp}✈️ · 🔓 {full}✈️ · 📦 {cnt}\n"
                     f"   {ch_str}\n")
    else:
        text += "Нет категорий."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="a:cat_create")],
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data="a:cat_edit_list")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="a:cat_del_list")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:content")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:cat_create")
async def a_cat_create(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("Название:")
    await state.set_state(AdminStates.cat_name)
    await callback.answer()


@dp.message(AdminStates.cat_name)
async def cat_name(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(cat_name=message.text.strip())
    await message.answer("Эмодзи (или «-»):")
    await state.set_state(AdminStates.cat_emoji)


@dp.message(AdminStates.cat_emoji)
async def cat_emoji(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    emoji = message.text.strip()
    if emoji == "-" or not emoji:
        emoji = "📁"
    await state.update_data(cat_emoji=emoji[:4])
    await message.answer(f"💰 Цена за 1 видео ({DEFAULT_PAID_PRICE}):")
    await state.set_state(AdminStates.cat_paid)


@dp.message(AdminStates.cat_paid)
async def cat_paid(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text.isdigit():
        await message.answer("⚠️ Число.")
        return
    await state.update_data(cat_paid=int(message.text))
    await message.answer(
        "📢 Введи <b>ID канала</b> (или <code>0</code>):\n\n"
        "<i>Формат: -1001234567890</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.cat_channel)


@dp.message(AdminStates.cat_channel)
async def cat_channel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    txt = message.text.strip()
    try:
        channel_id = int(txt)
    except ValueError:
        await message.answer("⚠️ Число.")
        return
    await state.update_data(cat_channel_id=channel_id)
    await message.answer("🖼 Отправь фото-обложку или <code>-</code>.",
                         parse_mode="HTML")
    await state.set_state(AdminStates.cat_cover)


@dp.message(AdminStates.cat_cover, F.photo)
async def cat_cover_photo(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    cover = message.photo[-1].file_id
    data = await state.get_data()
    ok = create_category(data.get("cat_name"), data.get("cat_emoji", "📁"),
                          data.get("cat_paid", DEFAULT_PAID_PRICE),
                          data.get("cat_channel_id", 0), cover)
    if ok:
        await message.answer("✅ Создано!", reply_markup=admin_kb())
    else:
        await message.answer("⚠️ Уже есть.", reply_markup=admin_kb())
    await state.clear()


@dp.message(AdminStates.cat_cover)
async def cat_cover_skip(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    txt = (message.text or "").strip()
    if txt != "-":
        await message.answer("⚠️ Фото или «-».")
        return
    data = await state.get_data()
    ok = create_category(data.get("cat_name"), data.get("cat_emoji", "📁"),
                          data.get("cat_paid", DEFAULT_PAID_PRICE),
                          data.get("cat_channel_id", 0), "")
    if ok:
        await message.answer("✅ Создано!", reply_markup=admin_kb())
    else:
        await message.answer("⚠️ Уже есть.", reply_markup=admin_kb())
    await state.clear()


@dp.callback_query(F.data == "a:cat_edit_list")
async def a_cat_edit_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    cats = get_categories()
    if not cats:
        await callback.answer("Нет", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for c in cats:
        b.button(text=f"{c[2]} {c[1]}", callback_data=f"a:catedit:{c[0]}")
    b.button(text="🏠 Назад", callback_data="a:cats")
    b.adjust(1)
    try:
        await callback.message.edit_text("Выбери:", reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("a:catedit:"))
async def a_catedit(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    cid = int(callback.data.split(":")[2])
    cat = get_category(cid)
    if not cat:
        await callback.answer("Не найдено", show_alert=True)
        return
    _, name, emoji, pp, ch_id, cover = cat
    cnt = len(get_media_in_category_ordered(cid))
    full = pp * cnt
    ch_str = f"📢 <code>{ch_id}</code>" if ch_id else "❌ не задан"
    cover_str = "🖼 есть" if cover else "❌ нет"
    text = (f"{emoji} <b>{name}</b>\n\n📦 Видео: <b>{cnt}</b>\n"
            f"💰 Цена: <b>{pp}✈️</b>\n🔓 Всё: <b>{full}✈️</b>\n"
            f"Канал: {ch_str}\nОбложка: {cover_str}")
    rows = [
        [InlineKeyboardButton(text="📥 Импорт из канала",
                              callback_data=f"a:cat_import:{cid}")],
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"a:cedit:{cid}:name"),
         InlineKeyboardButton(text="😊 Эмодзи", callback_data=f"a:cedit:{cid}:emoji")],
        [InlineKeyboardButton(text="💰 Цена", callback_data=f"a:cedit:{cid}:paid"),
         InlineKeyboardButton(text="📢 Канал", callback_data=f"a:cedit:{cid}:channel")],
        [InlineKeyboardButton(text="🖼 Обложка", callback_data=f"a:cedit:{cid}:cover")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:cat_edit_list")],
    ]
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("a:cat_import:"))
async def a_cat_import(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    cid = int(callback.data.split(":")[2])
    cat = get_category(cid)
    if not cat:
        await callback.answer("Не найдено", show_alert=True)
        return
    ch_id = cat[4]
    if not ch_id:
        await callback.answer("❌ Канал не привязан", show_alert=True)
        return
    await callback.message.answer(
        f"📥 <b>Импорт из канала</b>\n\n"
        f"Категория: {cat[2]} <b>{cat[1]}</b>\n"
        f"Канал: <code>{ch_id}</code>\n\n"
        f"Введи <b>стартовый message_id</b>:",
        parse_mode="HTML")
    await state.update_data(import_cat_id=cid, import_channel_id=ch_id)
    await state.set_state(AdminStates.cat_import_start)
    await callback.answer()


@dp.message(AdminStates.cat_import_start)
async def cat_import_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text.isdigit():
        await message.answer("⚠️ Число.")
        return
    start_id = int(message.text)
    await state.update_data(import_start_id=start_id)
    await message.answer("🔢 Сколько сообщений проверить (макс)?",
                         parse_mode="HTML")
    await state.set_state(AdminStates.cat_import_limit)


@dp.message(AdminStates.cat_import_limit)
async def cat_import_limit(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text.isdigit():
        await message.answer("⚠️ Число.")
        return
    limit = int(message.text)
    if limit <= 0 or limit > 5000:
        await message.answer("⚠️ От 1 до 5000.")
        return
    data = await state.get_data()
    cat_id = data.get("import_cat_id")
    channel_id = data.get("import_channel_id")
    start_id = data.get("import_start_id", 1)
    await state.clear()
    await message.answer("⏳ <b>Импорт запущен...</b>", parse_mode="HTML")
    added, skipped, errors, last_id = await import_last_channel_messages(
        bot, cat_id, channel_id, limit=limit, offset_id=start_id)
    await message.answer(
        f"✅ <b>Импорт завершён</b>\n\n"
        f"➕ Добавлено: <b>{added}</b>\n"
        f"⏭ Пропущено: <b>{skipped}</b>\n"
        f"⚠️ Ошибок: <b>{errors}</b>\n"
        f"🔚 Последний ID: <code>{last_id}</code>",
        parse_mode="HTML", reply_markup=admin_kb())


@dp.callback_query(F.data.startswith("a:cedit:"))
async def a_cedit(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    _, _, cid, field = callback.data.split(":")
    await state.update_data(edit_cat_id=int(cid), edit_field=field)
    if field == "cover":
        await callback.message.answer("🖼 Отправь фото или <code>-</code>.",
                                       parse_mode="HTML")
        await state.set_state(AdminStates.edit_cat_cover)
        await callback.answer()
        return
    labels = {"name": "Новое название:", "emoji": "Новый эмодзи:",
              "paid": "Новая цена:", "channel": "ID канала:"}
    await callback.message.answer(labels.get(field, "Значение:"))
    await state.set_state(AdminStates.edit_cat_value)
    await callback.answer()


@dp.message(AdminStates.edit_cat_value)
async def cedit_value(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    cid = data.get("edit_cat_id")
    field = data.get("edit_field")
    value = message.text.strip()
    try:
        if field == "paid":
            if not value.isdigit():
                await message.answer("⚠️ Число.")
                return
            update_category(cid, paid_price=int(value))
        elif field == "name":
            update_category(cid, name=value)
        elif field == "emoji":
            update_category(cid, emoji=value[:4])
        elif field == "channel":
            update_category(cid, channel_id=int(value))
        await message.answer("✅ Сохранено!", reply_markup=admin_kb())
    except Exception as e:
        await message.answer(f"❌ {e}", reply_markup=admin_kb())
    await state.clear()


@dp.message(AdminStates.edit_cat_cover, F.photo)
async def edit_cat_cover_photo(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    cid = data.get("edit_cat_id")
    update_category(cid, cover=message.photo[-1].file_id)
    await message.answer("✅ Обложка обновлена!", reply_markup=admin_kb())
    await state.clear()


@dp.message(AdminStates.edit_cat_cover)
async def edit_cat_cover_skip(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    txt = (message.text or "").strip()
    if txt != "-":
        await message.answer("⚠️ Фото или «-».")
        return
    data = await state.get_data()
    cid = data.get("edit_cat_id")
    update_category(cid, cover="")
    await message.answer("✅ Обложка удалена.", reply_markup=admin_kb())
    await state.clear()


@dp.callback_query(F.data == "a:cat_del_list")
async def a_cat_del_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    cats = get_categories()
    if not cats:
        await callback.answer("Нет", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for c in cats:
        b.button(text=f"❌ {c[2]} {c[1]} ({c[4]})",
                 callback_data=f"a:catdel:{c[0]}")
    b.button(text="🏠 Назад", callback_data="a:cats")
    b.adjust(1)
    try:
        await callback.message.edit_text("Удалить:", reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("a:catdel:"))
async def a_catdel(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    delete_category(int(callback.data.split(":")[2]))
    await callback.answer("Удалено")
    await a_cats(callback)


# ---- МАГАЗИН (админ) ----
@dp.callback_query(F.data == "a:shop")
async def a_shop(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    items = get_shop_items()
    text = "🏪 <b>Магазин</b>\n\n"
    if items:
        for row in items:
            iid, name, desc, price, ctype, content, req_photo = row
            mark = "📸" if req_photo else ""
            text += f"#{iid} <b>{name}</b>{mark} — {price}✈️ ({ctype})\n"
    else:
        text += "Пусто."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="a:shop_add")],
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data="a:shop_edit_list")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="a:shop_del_list")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:content")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:shop_add")
async def a_shop_add(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("🏪 Название товара:")
    await state.set_state(AdminStates.shop_name)
    await callback.answer()


@dp.message(AdminStates.shop_name)
async def shop_name(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(shop_name=message.text.strip())
    await message.answer("Описание (или «-»):")
    await state.set_state(AdminStates.shop_desc)


@dp.message(AdminStates.shop_desc)
async def shop_desc(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    desc = message.text.strip()
    if desc == "-":
        desc = ""
    await state.update_data(shop_desc=desc)
    await message.answer("💰 Цена в ✈️:")
    await state.set_state(AdminStates.shop_price)


@dp.message(AdminStates.shop_price)
async def shop_price(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    await state.update_data(shop_price=int(message.text))
    await message.answer(
        "Тип контента:\n"
        "• <code>text</code>\n"
        "• <code>link</code>\n"
        "• <code>photo</code>\n"
        "• <code>media</code> (видео)",
        parse_mode="HTML")
    await state.set_state(AdminStates.shop_type)


@dp.message(AdminStates.shop_type)
async def shop_type(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    t = message.text.strip().lower()
    if t not in ("text", "link", "photo", "media"):
        await message.answer("⚠️ text/link/photo/media")
        return
    await state.update_data(shop_type=t)
    await message.answer("Отправь контент (текст/ссылку/фото/видео):")
    await state.set_state(AdminStates.shop_content)


@dp.message(AdminStates.shop_content)
async def shop_content(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    t = data.get("shop_type")
    content = ""
    if t == "text" or t == "link":
        content = message.text or ""
    elif t == "photo" and message.photo:
        content = message.photo[-1].file_id
    elif t == "media" and message.video:
        content = message.video.file_id
    else:
        await message.answer("⚠️ Не тот тип контента.")
        return
    await state.update_data(shop_content=content)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Да — нужно фото", callback_data="a:shop_reqphoto:1")],
        [InlineKeyboardButton(text="⚡ Нет — без фото", callback_data="a:shop_reqphoto:0")],
    ])
    await message.answer(
        "📸 <b>Нужно ли фото от юзера при покупке?</b>\n\n"
        "<i>Если да — юзер пришлёт фото, оно уйдёт тебе вместе с заявкой.</i>",
        parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("a:shop_reqphoto:"))
async def a_shop_reqphoto(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    req = int(callback.data.split(":")[2])
    data = await state.get_data()
    add_shop_item(data.get("shop_name"), data.get("shop_desc", ""),
                  data.get("shop_price"), data.get("shop_type", "text"),
                  data.get("shop_content", ""), req)
    await callback.answer("✅")
    try:
        await callback.message.edit_text(
            f"✅ <b>Товар добавлен</b>\n\n"
            f"📸 Фото от юзера: <b>{'да' if req else 'нет'}</b>",
            parse_mode="HTML")
    except Exception:
        pass
    await state.clear()


@dp.callback_query(F.data == "a:shop_edit_list")
async def a_shop_edit_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    items = get_shop_items()
    if not items:
        await callback.answer("Пусто", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for row in items:
        iid, name, desc, price, ctype, content, req_photo = row
        b.button(text=f"✏️ #{iid} {name}", callback_data=f"a:shopedit:{iid}")
    b.button(text="🏠 Назад", callback_data="a:shop")
    b.adjust(1)
    try:
        await callback.message.edit_text("Выбери товар:", reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("a:shopedit:"))
async def a_shopedit(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    iid = int(callback.data.split(":")[2])
    item = get_shop_item(iid)
    if not item:
        await callback.answer("Не найдено", show_alert=True)
        return
    _, name, desc, price, ctype, content, req_photo = item
    text = (f"✏️ <b>Товар #{iid}</b>\n\n"
            f"📦 Название: <b>{name}</b>\n"
            f"📝 Описание: {desc or '—'}\n"
            f"💰 Цена: <b>{price}✈️</b>\n"
            f"📄 Тип: <b>{ctype}</b>\n"
            f"📸 Фото от юзера: <b>{'да' if req_photo else 'нет'}</b>")
    rows = [
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"a:shopedit_field:{iid}:name"),
         InlineKeyboardButton(text="📝 Описание", callback_data=f"a:shopedit_field:{iid}:desc")],
        [InlineKeyboardButton(text="💰 Цена", callback_data=f"a:shopedit_field:{iid}:price"),
         InlineKeyboardButton(text="📄 Тип", callback_data=f"a:shopedit_field:{iid}:type")],
        [InlineKeyboardButton(text="🖼 Контент", callback_data=f"a:shopedit_field:{iid}:content"),
         InlineKeyboardButton(text="📸 Фото", callback_data=f"a:shopedit_field:{iid}:photo")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:shop_edit_list")],
    ]
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("a:shopedit_field:"))
async def a_shopedit_field(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    _, _, iid, field = callback.data.split(":")
    iid = int(iid)
    await state.update_data(edit_shop_iid=iid, edit_shop_field=field)
    if field == "name":
        await callback.message.answer("✏️ Введи <b>новое название</b>:")
    elif field == "desc":
        await callback.message.answer("📝 Введи <b>новое описание</b> (или «-»):")
    elif field == "price":
        await callback.message.answer("💰 Введи <b>новую цену</b> в ✈️:")
    elif field == "type":
        await callback.message.answer(
            "📄 Выбери <b>тип</b>:\n• <code>text</code>\n• <code>link</code>\n"
            "• <code>photo</code>\n• <code>media</code>", parse_mode="HTML")
    elif field == "content":
        await callback.message.answer(
            "🖼 Отправь <b>новый контент</b>:\n"
            "• Для text/link — текст/ссылку\n• Для photo — фото\n• Для media — видео",
            parse_mode="HTML")
    elif field == "photo":
        await callback.message.answer(
            "📸 <b>Нужно ли фото от юзера?</b>\n• <code>1</code> — да\n• <code>0</code> — нет",
            parse_mode="HTML")
    else:
        await callback.answer("Неизвестное поле", show_alert=True)
        return
    await state.set_state(AdminStates.shop_edit_value)
    await callback.answer()


@dp.message(AdminStates.shop_edit_value)
async def a_shopedit_value(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    iid = data.get("edit_shop_iid")
    field = data.get("edit_shop_field")
    item = get_shop_item(iid)
    if not item:
        await state.clear()
        await message.answer("⚠️ Товар удалён.")
        return
    _, name, desc, price, ctype, content, req_photo = item
    txt = (message.text or "").strip()
    if field == "name":
        if not txt:
            await message.answer("⚠️ Пусто.")
            return
        update_shop_item(iid, name=txt)
        await message.answer(f"✅ Название: <b>{txt}</b>", parse_mode="HTML",
                             reply_markup=admin_kb())
    elif field == "desc":
        if txt == "-":
            txt = ""
        update_shop_item(iid, description=txt)
        await message.answer(f"✅ Описание: {txt or '—'}", parse_mode="HTML",
                             reply_markup=admin_kb())
    elif field == "price":
        if not txt.isdigit():
            await message.answer("⚠️ Число.")
            return
        update_shop_item(iid, price=int(txt))
        await message.answer(f"✅ Цена: <b>{txt}✈️</b>", parse_mode="HTML",
                             reply_markup=admin_kb())
    elif field == "type":
        t = txt.lower()
        if t not in ("text", "link", "photo", "media"):
            await message.answer("⚠️ text/link/photo/media")
            return
        update_shop_item(iid, content_type=t)
        await message.answer(f"✅ Тип: <b>{t}</b>", parse_mode="HTML",
                             reply_markup=admin_kb())
    elif field == "content":
        t = ctype
        new_content = ""
        if t == "text" or t == "link":
            new_content = message.text or ""
        elif t == "photo" and message.photo:
            new_content = message.photo[-1].file_id
        elif t == "media" and message.video:
            new_content = message.video.file_id
        else:
            await message.answer("⚠️ Не тот тип контента.")
            return
        update_shop_item(iid, content=new_content)
        await message.answer("✅ Контент обновлён!", reply_markup=admin_kb())
    elif field == "photo":
        if txt not in ("0", "1"):
            await message.answer("⚠️ 0 или 1.")
            return
        update_shop_item(iid, requires_photo=int(txt))
        await message.answer(
            f"✅ Фото от юзера: <b>{'да' if txt == '1' else 'нет'}</b>",
            parse_mode="HTML", reply_markup=admin_kb())
    else:
        await message.answer("⚠️ Неизвестное поле.")
    await state.clear()


@dp.callback_query(F.data == "a:shop_del_list")
async def a_shop_del_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    items = get_shop_items()
    if not items:
        await callback.answer("Пусто", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for row in items:
        iid, name, desc, price, ctype, content, req_photo = row
        b.button(text=f"🗑 #{iid} {name}", callback_data=f"a:shopdel:{iid}")
    b.button(text="🏠 Назад", callback_data="a:shop")
    b.adjust(1)
    try:
        await callback.message.edit_text("Удалить:", reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("a:shopdel:"))
async def a_shopdel(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    delete_shop_item(int(callback.data.split(":")[2]))
    await callback.answer("Удалено")
    await a_shop(callback)


# ---- ЗАДАНИЯ (админ) ----
@dp.callback_query(F.data == "a:tasks")
async def a_tasks(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    tasks = get_tasks(active_only=False)
    text = "📝 <b>Задания</b>\n\n"
    if tasks:
        for t in tasks:
            if len(t) == 9:
                tid, name, desc, emoji, reward, mtype, mcount, rep, act = t
                mark = "✅" if act else "❌"
            else:
                tid, name, desc, emoji, reward, mtype, mcount, rep = t
                mark = "✅"
            text += f"{mark} #{tid} {emoji} {name} — {reward}✈️ ({mtype} ×{mcount})\n"
    else:
        text += "Пусто."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="a:task_create")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="a:task_del_list")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:content")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:task_create")
async def a_task_create(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("📝 Название задания:")
    await state.set_state(AdminStates.task_name)
    await callback.answer()


@dp.message(AdminStates.task_name)
async def task_name(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(task_name=message.text.strip())
    await message.answer("Описание (или «-»):")
    await state.set_state(AdminStates.task_desc)


@dp.message(AdminStates.task_desc)
async def task_desc(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    d = message.text.strip()
    if d == "-":
        d = ""
    await state.update_data(task_desc=d)
    await message.answer("Эмодзи:")
    await state.set_state(AdminStates.task_emoji)


@dp.message(AdminStates.task_emoji)
async def task_emoji(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(task_emoji=message.text.strip()[:4])
    await message.answer("💰 Награда в ✈️:")
    await state.set_state(AdminStates.task_reward)


@dp.message(AdminStates.task_reward)
async def task_reward(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    await state.update_data(task_reward=int(message.text))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Фото", callback_data="a:task_type:photo")],
        [InlineKeyboardButton(text="📹 Видео", callback_data="a:task_type:video")],
        [InlineKeyboardButton(text="📸📹 Both", callback_data="a:task_type:both")],
    ])
    await message.answer("Выбери <b>тип контента</b>:", parse_mode="HTML",
                         reply_markup=kb)
    await state.set_state(AdminStates.task_media_type)


@dp.callback_query(F.data.startswith("a:task_type:"))
async def a_task_type(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    t = callback.data.split(":")[2]
    if t not in ("photo", "video", "both"):
        await callback.answer("⚠️", show_alert=True)
        return
    await state.update_data(task_media_type=t)
    label = {"photo": "📸 Фото", "video": "📹 Видео", "both": "📸📹 Both"}[t]
    try:
        await callback.message.edit_text(
            f"Тип: <b>{label}</b>\n\n🔢 Сколько файлов нужно?", parse_mode="HTML")
    except Exception:
        await callback.message.answer(
            f"Тип: <b>{label}</b>\n\n🔢 Сколько файлов нужно?", parse_mode="HTML")
    await state.set_state(AdminStates.task_media_count)
    await callback.answer("✅")


@dp.message(AdminStates.task_media_count)
async def task_media_count(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    await state.update_data(task_media_count=int(message.text))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Да — повторяемое", callback_data="a:task_rep:1")],
        [InlineKeyboardButton(text="1️⃣ Нет — один раз", callback_data="a:task_rep:0")],
    ])
    await message.answer("🔁 <b>Повторяемое?</b>", parse_mode="HTML", reply_markup=kb)
    await state.set_state(AdminStates.task_repeatable)


@dp.callback_query(F.data.startswith("a:task_rep:"))
async def a_task_rep(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    rep = int(callback.data.split(":")[2])
    data = await state.get_data()
    create_task(data.get("task_name"), data.get("task_desc", ""),
                data.get("task_emoji", "📝"), data.get("task_reward", 10),
                data.get("task_media_type", "photo"),
                data.get("task_media_count", 1), rep)
    try:
        await callback.message.edit_text(
            f"✅ <b>Задание создано!</b>\n\n"
            f"📝 {data.get('task_name')}\n"
            f"💰 {data.get('task_reward', 10)}✈️\n"
            f"📎 {data.get('task_media_type', 'photo')} × {data.get('task_media_count', 1)}\n"
            f"🔁 {'Повторяемое' if rep else 'Один раз'}", parse_mode="HTML")
    except Exception:
        pass
    await callback.answer("✅")
    await state.clear()


@dp.callback_query(F.data == "a:task_del_list")
async def a_task_del_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    tasks = get_tasks(active_only=False)
    if not tasks:
        await callback.answer("Пусто", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for t in tasks:
        tid = t[0]
        name = t[1]
        b.button(text=f"🗑 #{tid} {name}", callback_data=f"a:taskdel:{tid}")
    b.button(text="🏠 Назад", callback_data="a:tasks")
    b.adjust(1)
    try:
        await callback.message.edit_text("Удалить:", reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("a:taskdel:"))
async def a_taskdel(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    delete_task(int(callback.data.split(":")[2]))
    await callback.answer("Удалено")
    await a_tasks(callback)


@dp.callback_query(F.data.startswith("task:approve:"))
async def task_approve(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_ID:
        return
    _, _, tid, uid = callback.data.split(":")
    tid, uid = int(tid), int(uid)
    task = get_task(tid)
    if not task:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    reward = task[4]
    update_task_completion_status(uid, tid, "approved")
    add_balance(uid, reward, f"📝 Задание: {task[1]}")
    clear_task_progress(uid, tid)
    new_bal = get_balance(uid)
    try:
        await bot.send_message(
            uid,
            f"✅ <b>Задание одобрено!</b>\n\n"
            f"💰 +{reward}✈️\n💳 Баланс: <b>{new_bal}✈️</b>",
            parse_mode="HTML")
    except Exception as e:
        log.error(f"Task approve notify: {e}")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("✅ Одобрено")


@dp.callback_query(F.data.startswith("task:reject:"))
async def task_reject(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_ID:
        return
    _, _, tid, uid = callback.data.split(":")
    tid, uid = int(tid), int(uid)
    update_task_completion_status(uid, tid, "rejected")
    clear_task_progress(uid, tid)
    try:
        await bot.send_message(
            uid,
            "❌ <b>Задание отклонено.</b>\nПопробуй снова.",
            parse_mode="HTML")
    except Exception as e:
        log.error(f"Task reject notify: {e}")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("❌ Отклонено")


# =========================================================
# ПОДПИСКИ
# =========================================================
@dp.callback_query(F.data == "adm:subs")
async def adm_subs(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 PLUS", callback_data="a:plus"),
         InlineKeyboardButton(text="🔓 ALLOW", callback_data="a:allow")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="a:promos")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:back")],
    ])
    try:
        await callback.message.edit_text("💎 <b>Подписки</b>",
                                          parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:plus")
async def a_plus(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    text = (f"💎 <b>PLUS настройки</b>\n\n"
            f"💰 Цена: <b>{get_plus_price()}✈️</b>\n"
            f"⏳ Дней: <b>{get_plus_days()}</b>\n"
            f"🕐 +час: <b>{get_plus_hourly_bonus()}✈️</b>\n"
            f"📅 +день: <b>{get_plus_daily_bonus()}✈️</b>\n"
            f"🛒 Скидка ÷<b>{get_plus_discount_mult()}</b>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Цена", callback_data="a:plus_price"),
         InlineKeyboardButton(text="⏳ Дней", callback_data="a:plus_days")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:subs")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.in_({"a:plus_price", "a:plus_days"}))
async def a_plus_edit(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    field = callback.data.split(":")[1]
    mapping = {
        "plus_price": ("plus_price", "💰 Новое значение цены:"),
        "plus_days": ("plus_days", "⏳ Новое число дней:"),
    }
    key, prompt = mapping[field]
    await state.update_data(set_key=key)
    await callback.message.answer(prompt)
    await state.set_state(AdminStates.plus_price)
    await callback.answer()


@dp.message(AdminStates.plus_price)
async def a_plus_value(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    data = await state.get_data()
    set_setting(data.get("set_key"), message.text)
    await message.answer("✅ Сохранено", reply_markup=admin_kb())
    await state.clear()


@dp.callback_query(F.data == "a:allow")
async def a_allow(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    text = (f"🔓 <b>ALLOW настройки</b>\n\n"
            f"💰 Цена: <b>{get_allow_price()}✈️</b>\n"
            f"⏳ Дней: <b>{get_allow_days()}</b>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Цена", callback_data="a:allow_price"),
         InlineKeyboardButton(text="⏳ Дней", callback_data="a:allow_days")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:subs")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.in_({"a:allow_price", "a:allow_days"}))
async def a_allow_edit(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    field = callback.data.split(":")[1]
    mapping = {
        "allow_price": ("allow_price", "💰 Новое значение цены:"),
        "allow_days": ("allow_days", "⏳ Новое число дней:"),
    }
    key, prompt = mapping[field]
    await state.update_data(set_key=key)
    await callback.message.answer(prompt)
    await state.set_state(AdminStates.allow_price)
    await callback.answer()


@dp.message(AdminStates.allow_price)
async def a_allow_value(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    data = await state.get_data()
    set_setting(data.get("set_key"), message.text)
    await message.answer("✅ Сохранено", reply_markup=admin_kb())
    await state.clear()


# =========================================================
# ПРОМОКОДЫ (админ)
# =========================================================
@dp.callback_query(F.data == "adm:promo")
async def adm_promo(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="a:promos")],
        [InlineKeyboardButton(text="💸 Бонусы", callback_data="a:bonuses")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="a:broadcast_menu")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:back")],
    ])
    try:
        await callback.message.edit_text("🎁 <b>Акции</b>",
                                          parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:promos")
async def a_promos(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    active = get_active_promos()
    active_priv = get_active_promo_privs()
    arch = get_archived_promos()
    text = "🎟 <b>Промокоды</b>\n\n"
    text += f"<b>Активные (баланс) ({len(active)}):</b>\n"
    if active:
        for p in active:
            pid, code, reward, mu, uses, exp = p
            text += f"• <code>{code}</code> — +{reward}✈️ ({uses}/{mu or '∞'})\n"
    else:
        text += "<i>нет</i>\n"
    text += f"\n<b>Активные (подписки) ({len(active_priv)}):</b>\n"
    if active_priv:
        for p in active_priv:
            pid, code, ptype, days, mu, uses, exp = p
            text += f"• <code>{code}</code> — {ptype.upper()} +{days}д ({uses}/{mu or '∞'})\n"
    else:
        text += "<i>нет</i>\n"
    text += f"\n<b>Архив:</b> {len(arch)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать (баланс)", callback_data="a:promo_create")],
        [InlineKeyboardButton(text="💎 Создать (PLUS/ALLOW)", callback_data="a:promo_priv_create")],
        [InlineKeyboardButton(text="📢 Объявить", callback_data="a:promo_announce_list")],
        [InlineKeyboardButton(text="📦 Архив", callback_data="a:promo_archive")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="a:promo_del_list")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:promo")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:promo_create")
async def a_promo_create(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("🎫 Введи <b>код</b>:")
    await state.set_state(AdminStates.promo_code)
    await callback.answer()


@dp.message(AdminStates.promo_code)
async def promo_code(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(promo_code=message.text.strip())
    await message.answer("💰 Награда в ✈️:")
    await state.set_state(AdminStates.promo_reward)


@dp.message(AdminStates.promo_reward)
async def promo_reward(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    await state.update_data(promo_reward=int(message.text))
    await message.answer("👥 Макс. использований (0 = ∞):")
    await state.set_state(AdminStates.promo_uses)


@dp.message(AdminStates.promo_uses)
async def promo_uses(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    await state.update_data(promo_uses=int(message.text))
    await message.answer("⏳ Через сколько часов истечёт (0 = ∞):")
    await state.set_state(AdminStates.promo_hours)


@dp.message(AdminStates.promo_hours)
async def promo_hours(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    hours = int(message.text)
    data = await state.get_data()
    ok = create_promo(data.get("promo_code"), data.get("promo_reward"),
                       data.get("promo_uses"), hours)
    if ok:
        await message.answer("✅ Создан", reply_markup=admin_kb())
    else:
        await message.answer("⚠️ Дубликат", reply_markup=admin_kb())
    await state.clear()


@dp.callback_query(F.data == "a:promo_priv_create")
async def a_promo_priv_create(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("🎫 Введи <b>код</b>:")
    await state.set_state(AdminStates.promo_priv_code)
    await callback.answer()


@dp.message(AdminStates.promo_priv_code)
async def promo_priv_code(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(pp_code=message.text.strip())
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 PLUS", callback_data="a:pp_type:plus")],
        [InlineKeyboardButton(text="🔓 ALLOW", callback_data="a:pp_type:allow")],
    ])
    await message.answer("Выбери <b>тип подписки</b>:", parse_mode="HTML",
                         reply_markup=kb)
    await state.set_state(AdminStates.promo_priv_type)


@dp.callback_query(F.data.startswith("a:pp_type:"))
async def a_pp_type(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    ptype = callback.data.split(":")[2]
    if ptype not in ("plus", "allow"):
        await callback.answer("⚠️", show_alert=True)
        return
    await state.update_data(pp_type=ptype)
    label = "PLUS" if ptype == "plus" else "ALLOW"
    try:
        await callback.message.edit_text(f"Тип: <b>{label}</b>\n\n"
                                          f"На сколько <b>дней</b>?")
    except Exception:
        await callback.message.answer(f"Тип: <b>{label}</b>\n\nНа сколько <b>дней</b>?")
    await state.set_state(AdminStates.promo_priv_days)
    await callback.answer()


@dp.message(AdminStates.promo_priv_days)
async def promo_priv_days(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    await state.update_data(pp_days=int(message.text))
    await message.answer("👥 Макс. использований (0 = ∞):")
    await state.set_state(AdminStates.promo_priv_uses)


@dp.message(AdminStates.promo_priv_uses)
async def promo_priv_uses(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    await state.update_data(pp_uses=int(message.text))
    await message.answer("⏳ Через сколько часов истечёт (0 = ∞):")
    await state.set_state(AdminStates.promo_priv_hours)


@dp.message(AdminStates.promo_priv_hours)
async def promo_priv_hours(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    hours = int(message.text)
    data = await state.get_data()
    ok = create_promo_priv(
        data.get("pp_code"), data.get("pp_type", "plus"),
        data.get("pp_days", 7), data.get("pp_uses", 0), hours)
    if ok:
        await message.answer("✅ Создан", reply_markup=admin_kb())
    else:
        await message.answer("⚠️ Дубликат", reply_markup=admin_kb())
    await state.clear()


@dp.callback_query(F.data == "a:promo_announce_list")
async def a_promo_announce_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    active = get_active_promos()
    active_priv = get_active_promo_privs()
    if not active and not active_priv:
        await callback.answer("Нет активных промокодов", show_alert=True)
        return
    text = "📢 <b>Объявить промокод</b>\n\nВыбери какой рассылать:\n\n"
    b = InlineKeyboardBuilder()
    for p in active:
        pid, code, reward, mu, uses, exp = p
        b.button(text=f"📢 {code} — +{reward}✈️ (баланс)",
                 callback_data=f"a:promo_announce:{pid}")
    for p in active_priv:
        pid, code, ptype, days, mu, uses, exp = p
        b.button(text=f"📢 {code} — {ptype.upper()} +{days}д",
                 callback_data=f"a:promo_priv_announce:{pid}")
    b.button(text="🏠 Назад", callback_data="a:promos")
    b.adjust(1)
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                          reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


# ИЗМЕНЕНО: пауза 0.2 + TelegramRetryAfter
@dp.callback_query(F.data.startswith("a:promo_announce:"))
async def a_promo_announce(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    pid = int(callback.data.split(":")[2])
    promos = get_active_promos()
    target = next((p for p in promos if p[0] == pid), None)
    if not target:
        await callback.answer("Промокод не найден или неактивен", show_alert=True)
        return
    _, code, reward, mu, uses, exp = target
    mu_val = mu or 0
    text = (
        f"🎁 <b>Промокод!</b>\n\n"
        f"🎫 Код: <code>{code}</code>\n"
        f"💰 Награда: <b>+{reward}✈️</b>\n"
        f"👥 Активаций: <b>{uses}/{mu_val if mu_val > 0 else '∞'}</b>\n\n"
        f"Активировать: /promo {code}"
    )
    user_ids = get_all_user_ids()
    await callback.answer(f"📤 Рассылка на {len(user_ids)}...", show_alert=True)
    ok, fail = 0, 0
    flood_wait = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            ok += 1
        except TelegramRetryAfter as e:
            wait = e.retry_after + 1
            log.warning(f"⏳ Flood: ждём {wait} сек (uid={uid})")
            await asyncio.sleep(wait)
            try:
                await bot.send_message(uid, text, parse_mode="HTML")
                ok += 1
            except Exception:
                fail += 1
            flood_wait += wait
        except Exception:
            fail += 1
        await asyncio.sleep(0.2)
    info_lines = [
        f"🎫 <b>Промокод:</b> <code>{code}</code>",
        f"💰 Награда: <b>+{reward}✈️</b>",
        f"👥 Активаций: <b>{uses}/{mu_val if mu_val > 0 else '∞'}</b>",
    ]
    if exp:
        try:
            exp_str = datetime.fromisoformat(exp).strftime('%d.%m.%Y %H:%M')
            info_lines.append(f"⏳ До: <b>{exp_str}</b>")
        except Exception:
            pass
    try:
        await callback.message.answer(
            f"✅ <b>Промокод объявлен!</b>\n\n" + "\n".join(info_lines) +
            f"\n\n✅ Доставлено: <b>{ok}</b>\n❌ Ошибок: <b>{fail}</b>\n"
            f"⏳ Пауз из-за flood: <b>{flood_wait} сек</b>",
            parse_mode="HTML", reply_markup=admin_kb())
    except Exception:
        pass


# ИЗМЕНЕНО: пауза 0.2 + TelegramRetryAfter
@dp.callback_query(F.data.startswith("a:promo_priv_announce:"))
async def a_promo_priv_announce(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    pid = int(callback.data.split(":")[2])
    promos = get_active_promo_privs()
    target = next((p for p in promos if p[0] == pid), None)
    if not target:
        await callback.answer("Промокод не найден или неактивен", show_alert=True)
        return
    _, code, ptype, days, mu, uses, exp = target
    mu_val = mu or 0
    text = (
        f"🎁 <b>Промокод!</b>\n\n"
        f"🎫 Код: <code>{code}</code>\n"
        f"💎 Награда: <b>{ptype.upper()} +{days}д</b>\n"
        f"👥 Активаций: <b>{uses}/{mu_val if mu_val > 0 else '∞'}</b>\n\n"
        f"Активировать: /promo {code}"
    )
    user_ids = get_all_user_ids()
    await callback.answer(f"📤 Рассылка на {len(user_ids)}...", show_alert=True)
    ok, fail = 0, 0
    flood_wait = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            ok += 1
        except TelegramRetryAfter as e:
            wait = e.retry_after + 1
            log.warning(f"⏳ Flood: ждём {wait} сек (uid={uid})")
            await asyncio.sleep(wait)
            try:
                await bot.send_message(uid, text, parse_mode="HTML")
                ok += 1
            except Exception:
                fail += 1
            flood_wait += wait
        except Exception:
            fail += 1
        await asyncio.sleep(0.2)
    info_lines = [
        f"🎫 <b>Промокод:</b> <code>{code}</code>",
        f"💎 Награда: <b>{ptype.upper()} +{days}д</b>",
        f"👥 Активаций: <b>{uses}/{mu_val if mu_val > 0 else '∞'}</b>",
    ]
    if exp:
        try:
            exp_str = datetime.fromisoformat(exp).strftime('%d.%m.%Y %H:%M')
            info_lines.append(f"⏳ До: <b>{exp_str}</b>")
        except Exception:
            pass
    try:
        await callback.message.answer(
            f"✅ <b>Промокод объявлен!</b>\n\n" + "\n".join(info_lines) +
            f"\n\n✅ Доставлено: <b>{ok}</b>\n❌ Ошибок: <b>{fail}</b>\n"
            f"⏳ Пауз из-за flood: <b>{flood_wait} сек</b>",
            parse_mode="HTML", reply_markup=admin_kb())
    except Exception:
        pass


@dp.callback_query(F.data == "a:promo_archive")
async def a_promo_archive(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    arch = get_archived_promos()
    text = f"📦 <b>Архив промокодов</b> ({len(arch)})\n\n"
    if not arch:
        text += "<i>Пусто</i>"
    else:
        for p in arch[:50]:
            pid, code, reward, mu, uses, exp = p
            uses_str = f"{uses}/{mu or '∞'}"
            exp_str = ""
            if exp:
                try:
                    exp_str = f" · до {datetime.fromisoformat(exp).strftime('%d.%m %H:%M')}"
                except Exception:
                    pass
            text += f"• <code>{code}</code> — +{reward}✈️ ({uses_str}){exp_str}\n"
        if len(arch) > 50:
            text += f"\n<i>...и ещё {len(arch) - 50}</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧹 Очистить архив", callback_data="a:promo_clear_archive")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:promos")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:promo_clear_archive")
async def a_promo_clear_archive(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    n = clear_archived_promos()
    await callback.answer(f"🧹 Очищено: {n}")
    await a_promo_archive(callback)


@dp.callback_query(F.data == "a:promo_del_list")
async def a_promo_del_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    promos = get_all_promos()
    privs = get_all_promo_privs()
    if not promos and not privs:
        await callback.answer("Пусто", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for p in promos:
        pid, code, reward, mu, uses, exp = p
        b.button(text=f"🗑 {code} (баланс)", callback_data=f"a:promodel:{pid}")
    for p in privs:
        pid, code, ptype, days, mu, uses, exp = p
        b.button(text=f"🗑 {code} ({ptype.upper()})",
                 callback_data=f"a:promoprivdel:{pid}")
    b.button(text="🏠 Назад", callback_data="a:promos")
    b.adjust(1)
    try:
        await callback.message.edit_text("Удалить:", reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("a:promodel:"))
async def a_promodel(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    delete_promo(int(callback.data.split(":")[2]))
    await callback.answer("Удалён")
    await a_promos(callback)


@dp.callback_query(F.data.startswith("a:promoprivdel:"))
async def a_promoprivdel(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    delete_promo_priv(int(callback.data.split(":")[2]))
    await callback.answer("Удалён")
    await a_promos(callback)


# =========================================================
# РАССЫЛКА
# =========================================================
@dp.callback_query(F.data == "a:broadcast_menu")
async def a_broadcast_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    sig_on = get_broadcast_signature_enabled()
    sig_status = "✅ ВКЛ" if sig_on else "❌ ВЫКЛ"
    text = (
        f"📢 <b>Рассылка</b>\n\n"
        f"Подпись: <b>{sig_status}</b>\n\n"
        f"<b>Текущая подпись:</b>\n"
        f"<code>{get_broadcast_signature_text()}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Отправить рассылку",
                              callback_data="a:broadcast")],
        [InlineKeyboardButton(text=f"✍️ Подпись: {sig_status}",
                              callback_data="a:bc_sig_toggle")],
        [InlineKeyboardButton(text="📝 Изменить текст подписи",
                              callback_data="a:bc_sig_text")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:back")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:bc_sig_toggle")
async def a_bc_sig_toggle(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    new_val = not get_broadcast_signature_enabled()
    set_broadcast_signature_enabled(new_val)
    await callback.answer("✅ ВКЛ" if new_val else "❌ ВЫКЛ")
    await a_broadcast_menu(callback)


@dp.callback_query(F.data == "a:bc_sig_text")
async def a_bc_sig_text(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer(
        f"📝 <b>Текущая подпись:</b>\n"
        f"<code>{get_broadcast_signature_text()}</code>\n\n"
        f"Отправь <b>новый текст</b> подписи.\n"
        f"<i>Можно с HTML-тегами.</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.bcast_sig_text)
    await callback.answer()


@dp.message(AdminStates.bcast_sig_text)
async def a_bc_sig_text_do(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("⚠️ Пусто.")
        return
    set_setting("broadcast_signature_text", text)
    await message.answer(
        f"✅ <b>Подпись сохранена:</b>\n<code>{text}</code>",
        parse_mode="HTML", reply_markup=admin_kb())
    await state.clear()


@dp.callback_query(F.data == "a:broadcast")
async def a_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("📢 Отправь текст рассылки:")
    await state.set_state(AdminStates.waiting_broadcast)
    await callback.answer()


# ИЗМЕНЕНО: пауза 0.2 + TelegramRetryAfter
@dp.message(AdminStates.waiting_broadcast)
async def a_broadcast_send(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("⚠️ Пусто.")
        return
    sig = ""
    if get_broadcast_signature_enabled():
        sig = get_broadcast_signature_text()
    full_text = text + sig
    ids = get_all_user_ids()
    ok, fail = 0, 0
    flood_wait = 0
    await message.answer(f"📤 Рассылка на {len(ids)} юзеров (пауза 0.2с)...")
    for uid in ids:
        try:
            await bot.send_message(uid, full_text, parse_mode="HTML")
            ok += 1
        except TelegramRetryAfter as e:
            wait = e.retry_after + 1
            log.warning(f"⏳ Flood: ждём {wait} сек (uid={uid})")
            await asyncio.sleep(wait)
            try:
                await bot.send_message(uid, full_text, parse_mode="HTML")
                ok += 1
            except Exception:
                fail += 1
            flood_wait += wait
        except Exception:
            fail += 1
        await asyncio.sleep(0.2)
    await message.answer(
        f"✅ OK: {ok} | ❌ Fail: {fail}\n"
        f"⏳ Пауз из-за flood: {flood_wait} сек",
        reply_markup=admin_kb())
    await state.clear()


# =========================================================
# СИСТЕМА
# =========================================================
@dp.callback_query(F.data == "adm:system")
async def adm_system(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    stopped = is_bot_stopped()
    status = "🔴 СТОП" if stopped else "🟢 ОК"
    ad_enabled = is_auto_delete_enabled()
    ad_status = "✅" if ad_enabled else "❌"
    ad_min = get_auto_delete_delay() // 60
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔧 Стоп/Рестоп ({status})",
                              callback_data="a:stop_menu")],
        [InlineKeyboardButton(text=f"🗑 Автоудаление ({ad_status} · {ad_min}м)",
                              callback_data="a:autodel_menu")],
        [InlineKeyboardButton(text="💾 Бэкап БД", callback_data="a:backup")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="a:back")],
    ])
    try:
        await callback.message.edit_text(
            f"🛠 <b>Система</b>\n\nСтатус: {status}",
            parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:stop_menu")
async def a_stop_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    stopped = is_bot_stopped()
    status = "🔴 ОСТАНОВЛЕН" if stopped else "🟢 РАБОТАЕТ"
    text = f"🔧 <b>Управление ботом</b>\n\nСтатус: <b>{status}</b>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="▶️ Запустить" if stopped else "⏸ Остановить",
            callback_data="a:stop_toggle")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:system")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:stop_toggle")
async def a_stop_toggle(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    if is_bot_stopped():
        set_bot_stopped(False)
        await callback.answer("✅ Запущен")
        await send_log("✅ <b>Бот снят с тех. перерыва</b>")
    else:
        set_bot_stopped(True)
        await callback.answer("⏸ Остановлен")
        await send_log("🔧 <b>Бот поставлен на тех. перерыв</b>")
    await a_stop_menu(callback)


@dp.callback_query(F.data == "a:autodel_menu")
async def a_autodel_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    enabled = is_auto_delete_enabled()
    mins = get_auto_delete_delay() // 60
    status = "✅ ВКЛ" if enabled else "❌ ВЫКЛ"
    text = (f"🗑 <b>Автоудаление</b>\n\n"
            f"Статус: {status}\nЗадержка: <b>{mins} мин</b>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'❌ Выключить' if enabled else '✅ Включить'}",
            callback_data="a:autodel_toggle")],
        [InlineKeyboardButton(text="⏱ Задержка",
                              callback_data="a:autodel_delay")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="adm:system")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "a:autodel_toggle")
async def a_autodel_toggle(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    new_val = not is_auto_delete_enabled()
    set_auto_delete_enabled(new_val)
    await callback.answer("✅" if new_val else "❌")
    await a_autodel_menu(callback)


@dp.callback_query(F.data == "a:autodel_delay")
async def a_autodel_delay(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    current = get_auto_delete_delay() // 60
    await callback.message.answer(
        f"Текущая: <b>{current} мин</b>\n\nВведи новое кол-во <b>минут</b>:",
        parse_mode="HTML")
    await state.set_state(AdminStates.autodel_delay)
    await callback.answer()


@dp.message(AdminStates.autodel_delay)
async def autodel_delay_value(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID or not message.text.isdigit():
        return
    mins = int(message.text)
    if mins <= 0 or mins > 10080:
        await message.answer("⚠️ От 1 до 10080 минут.")
        return
    set_setting("auto_delete_delay", mins * 60)
    await message.answer(f"✅ {mins} мин", reply_markup=admin_kb())
    await state.clear()


@dp.callback_query(F.data == "a:backup")
async def a_backup(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_ID:
        return
    try:
        await bot.send_document(ADMIN_ID, FSInputFile(
            DB_PATH,
            filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"))
        await callback.answer("✅")
    except Exception as e:
        await callback.answer(f"❌ {e}", show_alert=True)


# =========================================================
# FALLBACK
# =========================================================
@dp.message()
async def fallback_handler(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if uid == ADMIN_ID:
        return
    current = await state.get_state()
    if current is not None:
        return
    if HAS_DARKBOOST and db_needs_check(uid):
        await show_db_sponsors(message, uid,
                                message.from_user.username or "", "")
        return
    await message.answer(
        "🤖 Я не понимаю. Используй кнопки внизу или /start.",
        reply_markup=main_kb(uid))


# =========================================================
# ФОНОВЫЕ ЗАДАЧИ
# =========================================================
async def crypto_polling_task():
    while True:
        if not cp:
            return
        try:
            log.info("💎 CryptoPay: polling начат")
            await cp.start_polling()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"💎 CryptoPay polling: {e}", exc_info=True)
            await asyncio.sleep(10)


# ИЗМЕНЕНО: пауза 0.3 между юзерами
async def low_balance_push_task():
    await asyncio.sleep(60)
    while True:
        try:
            conn = db()
            c = conn.cursor()
            c.execute("""SELECT u.user_id, u.joined_at, u.balance
                         FROM users u
                         WHERE u.activated=1 AND u.banned=0
                           AND u.balance < 3
                           AND u.user_id NOT IN (SELECT user_id FROM blocked_users)
                           AND u.user_id NOT IN (SELECT user_id FROM low_balance_notified)""")
            rows = c.fetchall()
            conn.close()
            for uid, joined_at, bal in rows:
                try:
                    try:
                        joined = datetime.fromisoformat(joined_at)
                    except Exception:
                        continue
                    age = (datetime.now() - joined).total_seconds()
                    is_new = age < 86400
                    is_old = age >= 7 * 86400
                    if not (is_new or is_old):
                        continue
                    if user_ever_claimed_bonus(uid):
                        continue
                    if not has_available_earn(uid):
                        continue
                    if was_low_balance_notified(uid):
                        continue
                    if is_old:
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="🎁 Забрать всё",
                                                  callback_data="earn:claim_all")],
                            [InlineKeyboardButton(text="💰 Заработать",
                                                  callback_data="goto_earn")],
                        ])
                        text = (
                            "💸 <b>У тебя почти нет валюты!</b>\n\n"
                            f"💰 Баланс: <b>{bal}✈️</b>\n\n"
                            "🎁 В «Заработать» тебя ждут бонусы — забери всё одним нажатием!"
                        )
                    else:
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="💰 Заработать",
                                                  callback_data="goto_earn")],
                        ])
                        text = (
                            "💸 <b>У тебя почти нет валюты!</b>\n\n"
                            f"💰 Баланс: <b>{bal}✈️</b>\n\n"
                            "🎁 В разделе «💰 Заработать» тебя ждут бонусы — забери их!"
                        )
                    ok = await safe_send(bot, uid, text, parse_mode="HTML",
                                          reply_markup=kb)
                    if ok:
                        mark_low_balance_notified(uid)
                        log.info(f"💸 low balance push → {uid}")
                    await asyncio.sleep(0.3)
                except Exception as e:
                    log.warning(f"low_balance_push {uid}: {e}")
        except Exception as e:
            log.error(f"low_balance_push_task: {e}", exc_info=True)
        await asyncio.sleep(1800)


# ИЗМЕНЕНО: пауза 0.3 между юзерами
async def low_earn_push_task():
    await asyncio.sleep(90)
    while True:
        try:
            conn = db()
            c = conn.cursor()
            c.execute("""SELECT user_id FROM users
                         WHERE activated=1 AND banned=0 AND balance < 3
                           AND user_id NOT IN (SELECT user_id FROM blocked_users)""")
            ids = [r[0] for r in c.fetchall()]
            conn.close()
            for uid in ids:
                try:
                    if not should_push_low_earn(uid):
                        continue
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💰 Заработать",
                                              callback_data="goto_earn")],
                    ])
                    ok = await safe_send(
                        bot, uid,
                        "💸 <b>Мало ✈️?</b>\n\n"
                        "Загляни в «Заработать» — там бонусы, стрик и квесты!\n"
                        "Жми кнопку 👇",
                        parse_mode="HTML", reply_markup=kb)
                    if ok:
                        mark_low_earn_push(uid)
                    await asyncio.sleep(0.3)
                except Exception as e:
                    log.warning(f"low_earn_push {uid}: {e}")
        except Exception as e:
            log.error(f"low_earn_push_task: {e}", exc_info=True)
        await asyncio.sleep(600)


if cp:
    @cp.invoice_paid()
    async def on_crypto_invoice_paid(invoice):
        try:
            invoice_id = str(invoice.invoice_id)
            topup = get_crypto_topup(invoice_id)
            if not topup:
                return
            _, uid, amount_pt, status = topup
            if status == "paid":
                return
            if not mark_crypto_topup_paid(invoice_id):
                return
            add_balance(uid, amount_pt, "🪙 Крипта")
            new_bal = get_balance(uid)
            try:
                await bot.send_message(
                    uid,
                    f"✅ <b>Оплата крипты получена!</b>\n\n"
                    f"💰 +{amount_pt}✈️\n"
                    f"💳 Баланс: <b>{new_bal}✈️</b>",
                    parse_mode="HTML")
            except Exception:
                pass
            await send_log(
                f"💰 Crypto АВТО\n👤 <code>{uid}</code>\n💵 +{amount_pt}✈️")
        except Exception as e:
            log.error(f"on_crypto_invoice_paid: {e}")


# =========================================================
# ОШИБКИ И ЗАПУСК
# =========================================================
# ИЗМЕНЕНО: логируем все BadRequest, кроме message is not modified
@dp.errors()
async def errors_handler(event: types.ErrorEvent):
    exc = event.exception
    if isinstance(exc, TelegramBadRequest):
        err = str(exc).lower()
        if "message is not modified" in err:
            return True
        log.error(f"TelegramBadRequest: {exc}")
        return True
    log.error(f"Error: {exc}")
    return True


async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="promo", description="🎟 Промокод"),
        BotCommand(command="cancel", description="❌ Отменить"),
        BotCommand(command="stop", description="⏸ Остановить"),
        BotCommand(command="restop", description="▶️ Запустить"),
    ])


async def main():
    log.info(f"🤖 Запущен. ADMIN_ID={ADMIN_ID}")
    log.info(f"📁 DB_PATH={DB_PATH}")
    log.info(f"💎 CryptoPay: {'✅' if cp else '❌'}")
    log.info(f"🛡 DarkBoost: {'✅' if HAS_DARKBOOST else '❌'}")
    log.info(f"⭐ Stars receiver: @{STARS_RECEIVER}")
    log.info(f"🗑 Видео автоудаление: {AUTO_DELETE_SECONDS // 60} мин")
    log.info(f"🗑 Текст автоудаление: {BOT_MSG_DELETE_SECONDS // 3600} ч")
    log.info(f"🛡 DarkBoost повтор: {REMIND_INTERVAL // 3600} ч")
    await set_commands()
    try:
        me = await bot.get_me()
        log.info(f"🤖 Основной бот: @{me.username} (id={me.id})")
        log.info(f"👑 ADMIN_ID: {ADMIN_ID}")
        try:
            chat = await bot.get_chat(ADMIN_ID)
            log.info(f"✅ ADMIN chat OK: {chat.type} {chat.id}")
        except Exception as e:
            log.error(f"❌ ADMIN chat ERROR: {e}")
            log.error("👉 НАПИШИ /start БОТУ ИЛИ ПРОВЕРЬ ADMIN_ID!")
    except Exception as e:
        log.error(f"getMe: {e}")
    if cp:
        asyncio.create_task(crypto_polling_task())
    asyncio.create_task(low_balance_push_task())
    asyncio.create_task(low_earn_push_task())
    try:
        await dp.start_polling(bot)
    finally:
        if _http_session and not _http_session.closed:
            try:
                await _http_session.close()
            except Exception:
                pass
        await bot.session.close()
        log.info("🛑 Остановлен")


if __name__ == "__main__":
    asyncio.run(main())