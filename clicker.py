import asyncio
import os
import random
import logging
import time
from datetime import datetime
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramRetryAfter,
    TelegramForbiddenError,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

BOT_TOKEN = os.getenv("CLICKER_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8621228825"))
MIN_WITHDRAW = 100.0
BOT_USERNAME = "@ZOVVOZZOVVOXbot"

CLICK_COOLDOWN = 0.3
ADMIN_GET_COOLDOWN = 60.0
ADMIN_GET_AMOUNT = 1000.0
UPGRADE_PRICE = 50.0
UPGRADE_MULTIPLIER = 2.0
UPGRADE_MAX_LEVEL = 2

STARS_COST = 300.0
STARS_AMOUNT = 15
CRYPTO_COST = 500.0
CRYPTO_AMOUNT = 1.0

DARKBOOST_API_KEY = os.getenv("DARKBOOST_API_KEY", "")
DARKBOOST_BASE = "https://darkboosts.com"
CHECK_COOLDOWN = 3.0
SESSION_TTL = 3600

SUBSCRIPTION_INTERVAL = 3 * 3600
SUBSCRIPTION_SPONSORS = 3

TASK_SPONSORS_COUNT = 10
TASK_REWARD = 50.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

user_balances = {}
user_upgrades = {}
user_banned = set()
user_clicks = {}
last_click_time = {}
last_admin_get = {"t": 0.0}
withdraw_requests = {}
request_counter = {"next_id": 1}
all_user_ids = set()

_http_session: Optional[aiohttp.ClientSession] = None
_last_darkboost_check: dict[int, float] = {}
_last_sponsors_request: dict[int, float] = {}

last_subscription_check: dict[int, float] = {}
subscription_pending: dict[int, dict] = {}

task_completed: set[int] = set()
task_pending: dict[int, dict] = {}


class BotStates(StatesGroup):
    waiting_balance = State()
    waiting_broadcast = State()
    waiting_ban = State()
    waiting_unban = State()
    bot_withdraw_amount = State()
    stars_username = State()
    crypto_receipt = State()


class RetryAfterMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        for attempt in range(3):
            try:
                return await handler(event, data)
            except TelegramRetryAfter as e:
                logger.warning(f"RetryAfter {e.retry_after} сек. (попытка {attempt + 1}/3)")
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                logger.warning("Пользователь заблокировал бота — пропускаем.")
                return None
        return None


def ensure_user(uid: int):
    user_balances.setdefault(uid, 0.0)
    user_upgrades.setdefault(uid, 0)
    user_clicks.setdefault(uid, 0)
    all_user_ids.add(uid)


def upgrade_level(uid: int) -> int:
    return user_upgrades.get(uid, 0)


def click_multiplier(uid: int) -> float:
    lvl = min(upgrade_level(uid), UPGRADE_MAX_LEVEL)
    return UPGRADE_MULTIPLIER ** lvl


def next_upgrade_price(uid: int) -> float:
    return UPGRADE_PRICE * (UPGRADE_MULTIPLIER ** upgrade_level(uid))


def can_upgrade(uid: int) -> bool:
    return upgrade_level(uid) < UPGRADE_MAX_LEVEL


def click_reward(uid: int) -> float:
    return round(random.uniform(0.3, 0.9), 2)


def create_withdraw_request(uid: int, username: str, amount: float,
                            wtype: str, details: str = "") -> int:
    rid = request_counter["next_id"]
    request_counter["next_id"] += 1
    withdraw_requests[rid] = {
        "user_id": uid,
        "username": username,
        "amount": amount,
        "type": wtype,
        "details": details,
        "status": "pending",
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    user_balances[uid] = max(0.0, user_balances.get(uid, 0.0) - amount)
    return rid


def get_main_keyboard(uid: int) -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="✈️ Кликер")
    b.button(text="👤 Профиль")
    b.button(text="💸 Вывод")
    b.button(text="📜 История")
    b.button(text="🎯 Задания")
    if uid == ADMIN_ID:
        b.button(text="🛠 Админ-панель")
        b.adjust(2, 2, 2)
    else:
        b.adjust(2, 2, 1)
    return b.as_markup(resize_keyboard=True)


def get_profile_inline(uid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    mult = click_multiplier(uid)
    if can_upgrade(uid):
        price = next_upgrade_price(uid)
        b.button(text=f"⚡ Прокачать клик (x{mult:.0f}) — {price:.0f} ✈️",
                 callback_data="upgrade_click")
    else:
        b.button(text=f"✅ Клик прокачан максимально (x{mult:.0f})", callback_data="noop")
    b.adjust(1)
    return b.as_markup()


def get_admin_keyboard() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="💯 Получить")
    b.button(text="📋 Заявки на вывод")
    b.button(text="📊 Статистика")
    b.button(text="👥 Пользователи")
    b.button(text="💰 Выдать/забрать")
    b.button(text="🚫 Бан/разбан")
    b.button(text="📢 Рассылка")
    b.button(text="📜 Все заявки")
    b.button(text="⬅️ Назад")
    b.adjust(2, 2, 2, 2, 1)
    return b.as_markup(resize_keyboard=True)


def get_requests_inline() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    pending = [(rid, r) for rid, r in withdraw_requests.items() if r["status"] == "pending"]
    if not pending:
        b.button(text="Нет активных заявок", callback_data="noop")
        return b.as_markup()
    icons = {"bot": "🤖", "stars": "⭐", "crypto": "💵", "normal": "✈️"}
    for rid, r in pending:
        icon = icons.get(r["type"], "✈️")
        b.button(text=f"#{rid} {icon} {r['amount']:.2f} ✈️", callback_data=f"view_req:{rid}")
    b.adjust(1)
    return b.as_markup()


def get_withdraw_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"⭐ {STARS_AMOUNT} ⭐ за {STARS_COST:.0f} ✈️", callback_data="wtype:stars")
    b.button(text=f"💵 {CRYPTO_AMOUNT} $ (CryptoBot) за {CRYPTO_COST:.0f} ✈️",
             callback_data="wtype:crypto")
    b.button(text=f"🤖 Вывод в бота — от {MIN_WITHDRAW:.0f} ✈️", callback_data="wtype:bot")
    b.adjust(1)
    return b.as_markup()


async def safe_edit(message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _http_session


async def darkboost_get_sponsors(user_id: int, username: Optional[str] = None,
                                 max_sponsors: int = SUBSCRIPTION_SPONSORS) -> dict:
    payload = {"user_id": user_id, "chat_id": user_id,
               "username": username or "", "max_sponsors": max_sponsors}
    session = await get_http_session()
    async with session.post(
        f"{DARKBOOST_BASE}/api/v1/sponsors",
        headers={"Auth": DARKBOOST_API_KEY}, json=payload,
    ) as resp:
        data = await resp.json()
        logger.info("DarkBoost /sponsors → %s, status=%s", resp.status, data.get("status"))
        return data


async def darkboost_check(user_id: int, session_id: int) -> dict:
    payload = {"user_id": user_id, "chat_id": user_id, "session_id": session_id}
    session = await get_http_session()
    async with session.post(
        f"{DARKBOOST_BASE}/api/v1/check",
        headers={"Auth": DARKBOOST_API_KEY}, json=payload,
    ) as resp:
        data = await resp.json()
        logger.info("DarkBoost /check → %s, status=%s", resp.status, data.get("status"))
        return data


def _db_throttled(uid: int) -> bool:
    now = time.time()
    if now - _last_darkboost_check.get(uid, 0) < CHECK_COOLDOWN:
        return True
    _last_darkboost_check[uid] = now
    return False


def needs_subscription(uid: int) -> bool:
    return time.time() - last_subscription_check.get(uid, 0) >= SUBSCRIPTION_INTERVAL


async def require_subscription(message: Message, uid: int, username: Optional[str],
                               action: str) -> bool:
    if uid == ADMIN_ID:
        return False
    if not needs_subscription(uid):
        return False

    existing = subscription_pending.get(uid)
    if existing:
        sponsors = existing["sponsors"]
    else:
        now = time.time()
        if now - _last_sponsors_request.get(uid, 0) < 30:
            await message.answer("⏳ Подожди немного.")
            return True
        _last_sponsors_request[uid] = now

        try:
            data = await darkboost_get_sponsors(
                user_id=uid, username=username, max_sponsors=SUBSCRIPTION_SPONSORS
            )
        except Exception as e:
            logger.exception("DarkBoost /sponsors (sub): %s", e)
            await message.answer("😔 Не удалось получить спонсоров. Попробуй позже.")
            return True

        status = data.get("status")
        if status == "cooldown":
            reset = data.get("reset_seconds", 60)
            await message.answer(f"⏳ Подожди {reset} сек. — идёт проверка спонсоров.")
            return True
        if status != "ok":
            if status == "no_offers":
                await message.answer("📭 Нет предложений. Попробуй позже.")
            else:
                await message.answer(f"⚠️ Ошибка DarkBoost: {status}.")
            return True

        sponsors = data.get("sponsors", [])[:SUBSCRIPTION_SPONSORS]
        if not sponsors:
            await message.answer("📭 Спонсоры не найдены.")
            return True

        subscription_pending[uid] = {
            "session_id": data["session_id"],
            "sponsors": sponsors,
        }

    kb = InlineKeyboardBuilder()
    for s in sponsors:
        kb.button(text=s["title"], url=s["link"])
    kb.button(text="✅ Я подписался — Проверить", callback_data=f"sub_check:{action}")
    kb.adjust(1)

    if action == "withdraw":
        text = "Подпишитесь на наших спонсоров для вывода."
    else:
        text = (
            "🔐 <b>Обязательная подписка</b>\n\n"
            "Подпишись на спонсоров ниже и нажми «✅ Проверить».\n"
            "Проверка требуется каждые 3 часа."
        )

    await message.answer(text, reply_markup=kb.as_markup())
    return True


@dp.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    if uid in user_banned:
        await message.answer("⛔ Вы забанены в боте.")
        return
    await message.answer(
        f"Привет, {message.from_user.full_name}!\n\n"
        f"✈️ Кликай и зарабатывай от 0.3 до 0.9 ✈️ за клик.\n"
        f"⏱ Кулдаун: {CLICK_COOLDOWN:.1f} сек.\n"
        f"💸 Вывод от {MIN_WITHDRAW:.0f} ✈️.",
        reply_markup=get_main_keyboard(uid),
    )


@dp.message(F.text == "✈️ Кликер")
async def handle_clicker(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    if uid in user_banned:
        return

    if await require_subscription(message, uid, message.from_user.username, action="click"):
        return

    now = time.time()
    last = last_click_time.get(uid, 0)
    if now - last < CLICK_COOLDOWN:
        return
    last_click_time[uid] = now

    user_clicks[uid] = user_clicks.get(uid, 0) + 1
    earned = click_reward(uid)
    user_balances[uid] += earned

    await message.answer(
        f"✈️ +{earned:.2f}\nБаланс: <b>{user_balances[uid]:.2f} ✈️</b>"
    )


@dp.message(F.text == "👤 Профиль")
async def handle_profile(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    status = "🚫 забанен" if uid in user_banned else "✅ активен"
    lvl = upgrade_level(uid)
    mult = click_multiplier(uid)
    task_status = "✅ выполнено" if uid in task_completed else "❌ не выполнено"
    await message.answer(
        f"👤 <b>Профиль</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"Имя: {message.from_user.full_name}\n"
        f"Баланс: <b>{user_balances[uid]:.2f} ✈️</b>\n"
        f"Всего кликов: {user_clicks[uid]}\n"
        f"Уровень клика: {lvl} (x{mult:.0f})\n"
        f"🎯 Задание: {task_status}\n"
        f"Статус: {status}",
        reply_markup=get_profile_inline(uid),
    )


@dp.callback_query(F.data == "upgrade_click")
async def upgrade_click(call: CallbackQuery):
    uid = call.from_user.id
    ensure_user(uid)
    if uid in user_banned:
        await call.answer("⛔ Вы забанены.", show_alert=True)
        return
    if not can_upgrade(uid):
        await call.answer(f"✅ Уже максимальный уровень (x{click_multiplier(uid):.0f}).", show_alert=True)
        return
    price = next_upgrade_price(uid)
    if user_balances[uid] < price:
        await call.answer(
            f"Не хватает ✈️. Нужно {price:.0f}, у вас {user_balances[uid]:.2f}.",
            show_alert=True,
        )
        return
    user_balances[uid] -= price
    user_upgrades[uid] = upgrade_level(uid) + 1
    new_mult = click_multiplier(uid)
    await call.answer(f"✅ Прокачка куплена! Теперь x{new_mult:.0f}")
    status = "🚫 забанен" if uid in user_banned else "✅ активен"
    task_status = "✅ выполнено" if uid in task_completed else "❌ не выполнено"
    await call.message.edit_text(
        f"👤 <b>Профиль</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"Имя: {call.from_user.full_name}\n"
        f"Баланс: <b>{user_balances[uid]:.2f} ✈️</b>\n"
        f"Всего кликов: {user_clicks.get(uid, 0)}\n"
        f"Уровень клика: {upgrade_level(uid)} (x{new_mult:.0f})\n"
        f"🎯 Задание: {task_status}\n"
        f"Статус: {status}",
        reply_markup=get_profile_inline(uid),
    )


@dp.message(F.text == "💸 Вывод")
async def handle_withdraw(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    if uid in user_banned:
        return
    await message.answer(
        f"💸 <b>Куда вывести</b>\n\n"
        f"Ваш баланс: <b>{user_balances[uid]:.2f} ✈️</b>",
        reply_markup=get_withdraw_menu(),
    )


@dp.callback_query(F.data.startswith("wtype:"))
async def cb_withdraw_type(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    ensure_user(uid)
    if uid in user_banned:
        await call.answer("⛔ Вы забанены.", show_alert=True)
        return

    wtype = call.data.split(":", 1)[1]
    balance = user_balances[uid]

    if uid != ADMIN_ID:
        if await require_subscription(call.message, uid, call.from_user.username, action="withdraw"):
            await call.answer()
            return

    await call.answer()

    if wtype == "stars":
        if balance < STARS_COST:
            await call.answer(f"❌ Нужно {STARS_COST:.0f} ✈️. У вас {balance:.2f} ✈️.", show_alert=True)
            return
        await state.set_state(BotStates.stars_username)
        await call.message.answer(
            f"⭐ <b>Вывод {STARS_AMOUNT} ⭐</b>\n\n"
            f"Отправьте ваш <b>@username</b> в Telegram, куда отправить звёзды.\n"
            f"Пример: <code>@username</code>\n\n"
            f"Отмена — /cancel"
        )

    elif wtype == "crypto":
        if balance < CRYPTO_COST:
            await call.answer(f"❌ Нужно {CRYPTO_COST:.0f} ✈️. У вас {balance:.2f} ✈️.", show_alert=True)
            return
        await state.set_state(BotStates.crypto_receipt)
        await call.message.answer(
            f"💵 <b>Вывод {CRYPTO_AMOUNT} $ (CryptoBot)</b>\n\n"
            f"Отправьте <b>ссылку на чек оплаты</b> CryptoBot.\n"
            f"Пример: <code>https://t.me/CryptoBot?start=IV...</code>\n\n"
            f"Отмена — /cancel"
        )

    elif wtype == "bot":
        if balance < MIN_WITHDRAW:
            await call.answer(f"❌ Минимум {MIN_WITHDRAW:.0f} ✈️. У вас {balance:.2f} ✈️.", show_alert=True)
            return
        kb = InlineKeyboardBuilder()
        kb.button(text=f"💰 Вся сумма ({balance:.2f} ✈️)", callback_data="bw:all")
        kb.button(text="✏️ Своё значение", callback_data="bw:custom")
        kb.adjust(1)
        await call.message.answer(
            f"🤖 <b>Вывод в бота</b>\n\n"
            f"Баланс: <b>{balance:.2f} ✈️</b>\n"
            f"Минимум: {MIN_WITHDRAW:.0f} ✈️",
            reply_markup=kb.as_markup(),
        )


@dp.message(BotStates.stars_username)
async def stars_username_input(message: Message, state: FSMContext):
    uid = message.from_user.id
    username = (message.text or "").strip()
    if not username.startswith("@") or len(username) < 4:
        await message.answer("❌ Отправьте @username. Пример: <code>@username</code>")
        return
    if user_balances.get(uid, 0.0) < STARS_COST:
        await state.clear()
        await message.answer("❌ Баланс изменился — вывод отменён.")
        return
    rid = create_withdraw_request(uid, message.from_user.full_name, STARS_COST, "stars", username)
    await state.clear()
    await message.answer(
        f"✅ Заявка №{rid} на вывод <b>{STARS_AMOUNT} ⭐</b> создана.\n"
        f"Юзернейм: <code>{username}</code>\n"
        f"Списано: {STARS_COST:.0f} ✈️\n"
        f"Ожидайте обработки админом."
    )


@dp.message(BotStates.crypto_receipt)
async def crypto_receipt_input(message: Message, state: FSMContext):
    uid = message.from_user.id
    link = (message.text or "").strip()
    if not (link.startswith("http://") or link.startswith("https://")):
        await message.answer("❌ Отправьте ссылку на чек. Пример: <code>https://t.me/CryptoBot?start=...</code>")
        return
    if user_balances.get(uid, 0.0) < CRYPTO_COST:
        await state.clear()
        await message.answer("❌ Баланс изменился — вывод отменён.")
        return
    rid = create_withdraw_request(uid, message.from_user.full_name, CRYPTO_COST, "crypto", link)
    await state.clear()
    await message.answer(
        f"✅ Заявка №{rid} на вывод <b>{CRYPTO_AMOUNT} $</b> создана.\n"
        f"Чек: <code>{link}</code>\n"
        f"Списано: {CRYPTO_COST:.0f} ✈️\n"
        f"Ожидайте обработки админом."
    )


@dp.callback_query(F.data == "bw:all")
async def bw_all(call: CallbackQuery):
    uid = call.from_user.id
    ensure_user(uid)
    balance = user_balances[uid]
    if balance < MIN_WITHDRAW:
        await call.answer(f"❌ Минимум {MIN_WITHDRAW:.0f} ✈️.", show_alert=True)
        return
    rid = create_withdraw_request(uid, call.from_user.full_name, balance, "bot")
    await safe_edit(call.message,
        f"✅ Заявка №{rid} на вывод <b>{balance:.2f} ✈️</b> в бота создана.\n"
        f"Ожидайте обработки админом."
    )
    await call.answer("Заявка создана")


@dp.callback_query(F.data == "bw:custom")
async def bw_custom(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(BotStates.bot_withdraw_amount)
    await call.message.answer(
        f"✏️ Введите сумму вывода (минимум {MIN_WITHDRAW:.0f} ✈️):\n"
        f"Отмена — /cancel"
    )


@dp.message(BotStates.bot_withdraw_amount)
async def bw_custom_input(message: Message, state: FSMContext):
    uid = message.from_user.id
    ensure_user(uid)
    if uid in user_banned:
        await state.clear()
        return
    try:
        amount = float(message.text.replace(",", ".").strip())
    except ValueError:
        await message.answer("❌ Введите число.")
        return
    if amount < MIN_WITHDRAW:
        await message.answer(f"❌ Минимум {MIN_WITHDRAW:.0f} ✈️.")
        return
    if amount > user_balances.get(uid, 0.0):
        await message.answer(f"❌ Недостаточно ✈️. Баланс: {user_balances[uid]:.2f}.")
        return
    rid = create_withdraw_request(uid, message.from_user.full_name, amount, "bot")
    await state.clear()
    await message.answer(
        f"✅ Заявка №{rid} на вывод <b>{amount:.2f} ✈️</b> в бота создана.\n"
        f"Ожидайте обработки админом."
    )


@dp.message(F.text == "📜 История")
async def handle_history(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    mine = [(rid, r) for rid, r in withdraw_requests.items() if r["user_id"] == uid]
    if not mine:
        await message.answer("📭 У вас пока нет заявок на вывод.")
        return
    mine.sort(key=lambda x: x[0], reverse=True)
    status_map = {"pending": "⏳ ожидает", "approved": "✅ одобрено", "rejected": "❌ отклонено"}
    type_map = {"bot": "🤖 в бота", "stars": "⭐ Stars", "crypto": "💵 CryptoBot", "normal": "✈️"}
    lines = ["📜 <b>Ваши заявки</b>\n"]
    for rid, r in mine[:20]:
        t = type_map.get(r["type"], "")
        lines.append(
            f"№{rid} — {t} <b>{r['amount']:.2f} ✈️</b> — {status_map.get(r['status'], r['status'])}\n"
            f"   <i>{r['created']}</i>"
        )
        if r.get("details"):
            lines.append(f"   Реквизиты: <code>{r['details']}</code>")
    if len(mine) > 20:
        lines.append(f"\n…и ещё {len(mine) - 20}")
    await message.answer("\n".join(lines))


@dp.message(F.text == "🎯 Задания")
async def handle_tasks(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    if uid in user_banned:
        return
    if uid in task_completed:
        await message.answer("✅ Вы уже выполнили задание. Спасибо!")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Начать задание", callback_data="task_start")
    kb.adjust(1)
    await message.answer(
        f"🎯 <b>Задание</b>\n\n"
        f"📋 Подпишись на <b>{TASK_SPONSORS_COUNT}</b> спонсоров и получи "
        f"<b>{TASK_REWARD:.0f} ✈️</b> на баланс.\n\n"
        f"▶️ Нажми «Начать», чтобы получить список спонсоров.",
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(F.data == "task_start")
async def cb_task_start(call: CallbackQuery):
    uid = call.from_user.id
    ensure_user(uid)
    if uid in user_banned:
        await call.answer("⛔ Вы забанены.", show_alert=True)
        return
    if uid in task_completed:
        await call.answer("✅ Задание уже выполнено.", show_alert=True)
        return
    await call.answer("Загружаю спонсоров...")

    now = time.time()
    if now - _last_sponsors_request.get(uid, 0) < 30:
        wait = 30 - (now - _last_sponsors_request.get(uid, 0))
        await call.message.answer(f"⏳ Подожди {wait:.0f} сек.")
        return
    _last_sponsors_request[uid] = now

    try:
        data = await darkboost_get_sponsors(
            user_id=uid, username=call.from_user.username, max_sponsors=TASK_SPONSORS_COUNT
        )
    except Exception as e:
        logger.exception("DarkBoost /sponsors (task): %s", e)
        await call.message.answer("😔 Не удалось получить спонсоров. Попробуй позже.")
        return

    status = data.get("status")
    if status == "no_offers":
        await call.message.answer("📭 Сейчас нет предложений. Попробуй позже.")
        return
    if status == "cooldown":
        await call.message.answer(f"⏳ Подожди {data.get('reset_seconds', 60)} сек.")
        return
    if status != "ok":
        await call.message.answer(f"⚠️ Ошибка DarkBoost: {status}.")
        return

    sponsors = data.get("sponsors", [])[:TASK_SPONSORS_COUNT]
    if not sponsors:
        await call.message.answer("📭 Спонсоры не найдены.")
        return

    task_pending[uid] = {"session_id": data["session_id"], "sponsors": sponsors}

    kb = InlineKeyboardBuilder()
    for s in sponsors:
        kb.button(text=s["title"], url=s["link"])
    kb.button(text="✅ Я подписался — Проверить", callback_data="task_check")
    kb.button(text="🔙 Заново (новые спонсоры)", callback_data="task_start")
    kb.adjust(1)

    await call.message.edit_text(
        f"🎯 <b>Задание: подписка на {TASK_SPONSORS_COUNT} спонсоров</b>\n\n"
        f"Подпишись на всех ниже и нажми «✅ Проверить».\n"
        f"Награда: <b>{TASK_REWARD:.0f} ✈️</b>\n\n"
        f"📋 Спонсоров: <b>{len(sponsors)}</b>",
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(F.data == "task_check")
async def cb_task_check(call: CallbackQuery):
    uid = call.from_user.id
    ensure_user(uid)
    if uid in user_banned:
        await call.answer("⛔ Вы забанены.", show_alert=True)
        return
    if uid in task_completed:
        await call.answer("✅ Задание уже выполнено.", show_alert=True)
        return
    pending = task_pending.get(uid)
    if not pending:
        await call.answer("⚠️ Сессия не найдена. Нажми «Начать» заново.", show_alert=True)
        return
    if _db_throttled(uid):
        await call.answer("⏳ Слишком часто. Подожди пару секунд.", show_alert=True)
        return
    await call.answer()

    try:
        result = await darkboost_check(user_id=uid, session_id=pending["session_id"])
    except Exception as e:
        logger.exception("DarkBoost /check (task): %s", e)
        await call.message.answer("😔 Ошибка проверки. Попробуй ещё раз.")
        return

    status = result.get("status")

    if status == "ok":
        task_completed.add(uid)
        task_pending.pop(uid, None)
        user_balances[uid] += TASK_REWARD
        await safe_edit(
            call.message,
            f"✅ <b>Задание выполнено!</b>\n\n"
            f"Награда: <b>+{TASK_REWARD:.0f} ✈️</b>\n"
            f"Баланс: <b>{user_balances[uid]:.2f} ✈️</b>",
        )
        return

    if status == "not_ok":
        missing = result.get("missing", [])
        missing_ids = set()
        for m in missing:
            if isinstance(m, dict):
                missing_ids.add(m.get("offer_id") or m.get("id"))
            else:
                missing_ids.add(str(m))

        kb = InlineKeyboardBuilder()
        for s in pending["sponsors"]:
            if s.get("id") in missing_ids:
                kb.button(text=f"❌ {s['title']}", url=s["link"])
        kb.button(text="✅ Я подписался — Проверить", callback_data="task_check")
        kb.button(text="🔙 Заново (новые спонсоры)", callback_data="task_start")
        kb.adjust(1)

        await safe_edit(
            call.message,
            "⚠️ <b>Не все подписки выполнены.</b>\n\n"
            "Подпишись на оставшихся и нажми «Проверить» снова.",
            reply_markup=kb.as_markup(),
        )
        return

    if status == "busy":
        await call.message.answer(f"⏳ Подожди {result.get('retry_after_ms', 2000) / 1000:.1f} сек.")
        return
    if status == "cooldown":
        await call.message.answer(f"⏳ Попробуй через {result.get('reset_seconds', 60)} сек.")
        return
    if status == "expired":
        task_pending.pop(uid, None)
        await safe_edit(call.message, "🔄 Сессия истекла. Нажми «🎯 Задания» → «Начать» заново.")
        return
    await call.message.answer(f"⚠️ Неизвестный ответ: {status}")


@dp.callback_query(F.data.startswith("sub_check:"))
async def cb_sub_check(call: CallbackQuery):
    uid = call.from_user.id
    action = call.data.split(":", 1)[1]

    if _db_throttled(uid):
        await call.answer("⏳ Слишком часто.", show_alert=True)
        return
    await call.answer()

    pending = subscription_pending.get(uid)
    if not pending:
        await call.message.answer("⚠️ Сессия не найдена. Попробуй снова.")
        return

    try:
        result = await darkboost_check(user_id=uid, session_id=pending["session_id"])
    except Exception as e:
        logger.exception("DarkBoost /check (sub): %s", e)
        await call.message.answer("😔 Ошибка проверки. Попробуй ещё раз.")
        return

    status = result.get("status")

    if status == "ok":
        subscription_pending.pop(uid, None)
        last_subscription_check[uid] = time.time()
        await safe_edit(call.message, "✅ <b>Подписка подтверждена!</b>")
        if action == "click":
            user_clicks[uid] = user_clicks.get(uid, 0) + 1
            earned = click_reward(uid)
            user_balances[uid] += earned
            await call.message.answer(
                f"✈️ +{earned:.2f}\nБаланс: <b>{user_balances[uid]:.2f} ✈️</b>"
            )
        elif action == "withdraw":
            await call.message.answer(
                f"💸 <b>Куда вывести</b>\n\n"
                f"Ваш баланс: <b>{user_balances.get(uid, 0):.2f} ✈️</b>",
                reply_markup=get_withdraw_menu(),
            )
        return

    if status == "not_ok":
        missing = result.get("missing", [])
        missing_ids = set()
        for m in missing:
            if isinstance(m, dict):
                missing_ids.add(m.get("offer_id") or m.get("id"))
            else:
                missing_ids.add(str(m))

        kb = InlineKeyboardBuilder()
        for s in pending["sponsors"]:
            if s.get("id") in missing_ids:
                kb.button(text=f"❌ {s['title']}", url=s["link"])
        kb.button(text="✅ Я подписался — Проверить", callback_data=f"sub_check:{action}")
        kb.adjust(1)

        await safe_edit(
            call.message,
            "⚠️ <b>Не все подписки выполнены.</b>\n\n"
            "Подпишись на оставшихся и нажми «Проверить» снова.",
            reply_markup=kb.as_markup(),
        )
        return

    if status == "busy":
        await call.message.answer(f"⏳ Подожди {result.get('retry_after_ms', 2000) / 1000:.1f} сек.")
        return
    if status == "cooldown":
        await call.message.answer(f"⏳ Подожди {result.get('reset_seconds', 60)} сек.")
        return
    if status == "expired":
        subscription_pending.pop(uid, None)
        await safe_edit(call.message, "🔄 Сессия истекла. Попробуй снова.")
        return
    await call.message.answer(f"⚠️ Неизвестный ответ: {status}")


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=get_admin_keyboard())


@dp.message(F.text == "🛠 Админ-панель")
async def open_admin(message: Message):
    await cmd_admin(message)


@dp.message(F.text == "⬅️ Назад")
async def back_main(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Главное меню", reply_markup=get_main_keyboard(ADMIN_ID))


@dp.message(F.text == "💯 Получить")
async def admin_get(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    now = time.time()
    diff = now - last_admin_get["t"]
    if diff < ADMIN_GET_COOLDOWN:
        await message.answer(f"⏱ Подождите {ADMIN_GET_COOLDOWN - diff:.0f} сек.")
        return
    last_admin_get["t"] = now
    user_balances[ADMIN_ID] = user_balances.get(ADMIN_ID, 0.0) + ADMIN_GET_AMOUNT
    await message.answer(
        f"💯 +{ADMIN_GET_AMOUNT:.0f} ✈️\nБаланс: <b>{user_balances[ADMIN_ID]:.2f} ✈️</b>"
    )


@dp.message(F.text == "📋 Заявки на вывод")
async def admin_requests(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    pending = [(rid, r) for rid, r in withdraw_requests.items() if r["status"] == "pending"]
    if not pending:
        await message.answer("📭 Активных заявок нет.")
        return
    await message.answer(
        f"📋 <b>Активные заявки</b> ({len(pending)}):",
        reply_markup=get_requests_inline(),
    )


@dp.message(F.text == "📜 Все заявки")
async def admin_all_requests(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    if not withdraw_requests:
        await message.answer("📭 Заявок нет.")
        return
    status_map = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    type_map = {"bot": "🤖", "stars": "⭐", "crypto": "💵", "normal": "✈️"}
    lines = ["📜 <b>Все заявки</b>\n"]
    for rid, r in sorted(withdraw_requests.items(), key=lambda x: x[0], reverse=True)[:30]:
        t = type_map.get(r["type"], "")
        lines.append(
            f"{status_map.get(r['status'], '?')} {t} №{rid} — {r['amount']:.2f} ✈️ — "
            f"{r['username']} (<code>{r['user_id']}</code>)"
        )
    await message.answer("\n".join(lines))


@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    total_users = len(all_user_ids)
    total_balance = sum(user_balances.values())
    pending = sum(1 for r in withdraw_requests.values() if r["status"] == "pending")
    approved = sum(1 for r in withdraw_requests.values() if r["status"] == "approved")
    rejected = sum(1 for r in withdraw_requests.values() if r["status"] == "rejected")
    upgraded_count = sum(1 for lvl in user_upgrades.values() if lvl > 0)
    total_clicks = sum(user_clicks.values())
    tasks_done = len(task_completed)
    subs_now = sum(1 for t in last_subscription_check.values()
                   if time.time() - t < SUBSCRIPTION_INTERVAL)
    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"🚫 Забанено: <b>{len(user_banned)}</b>\n"
        f"⚡ Прокачали клик: <b>{upgraded_count}</b>\n"
        f"🖱 Всего кликов: <b>{total_clicks}</b>\n"
        f"🔐 Активных подписок: <b>{subs_now}</b>\n"
        f"🎯 Выполнили задание: <b>{tasks_done}</b>\n"
        f"💎 Суммарный баланс: <b>{total_balance:.2f} ✈️</b>\n\n"
        f"📋 Заявки:\n"
        f"  ⏳ ожидают: {pending}\n"
        f"  ✅ одобрено: {approved}\n"
        f"  ❌ отклонено: {rejected}"
    )


@dp.message(F.text == "👥 Пользователи")
async def admin_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    if not all_user_ids:
        await message.answer("Пользователей нет.")
        return
    sorted_users = sorted(all_user_ids, key=lambda u: user_balances.get(u, 0), reverse=True)
    lines = [f"👥 <b>Пользователи</b> (топ-20 из {len(sorted_users)}):\n"]
    for i, u in enumerate(sorted_users[:20], 1):
        ban = " 🚫" if u in user_banned else ""
        lvl = user_upgrades.get(u, 0)
        up = f" ⚡x{min(2 ** lvl, 2 ** UPGRADE_MAX_LEVEL)}" if lvl > 0 else ""
        done = " 🎯" if u in task_completed else ""
        lines.append(f"{i}. <code>{u}</code> — {user_balances.get(u, 0):.2f} ✈️{up}{done}{ban}")
    await message.answer("\n".join(lines))


@dp.message(F.text == "💰 Выдать/забрать")
async def admin_balance_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(BotStates.waiting_balance)
    await message.answer(
        "Введите: <code>ID сумма</code>\n"
        "Сумма может быть отрицательной, чтобы забрать.\n\n"
        "Пример: <code>123456789 50</code>"
    )


@dp.message(BotStates.waiting_balance)
async def admin_balance_apply(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.split()
        uid = int(parts[0])
        amount = float(parts[1].replace(",", "."))
    except (ValueError, IndexError):
        await message.answer("❌ Неверный формат. Пример: <code>123456789 50</code>")
        return
    ensure_user(uid)
    user_balances[uid] = max(0.0, user_balances[uid] + amount)
    await state.clear()
    await message.answer(
        f"✅ Пользователю <code>{uid}</code> изменён баланс на {amount:+.2f} ✈️\n"
        f"Новый баланс: <b>{user_balances[uid]:.2f} ✈️</b>",
        reply_markup=get_admin_keyboard(),
    )
    try:
        await bot.send_message(
            uid,
            f"💰 Ваш баланс изменён на {amount:+.2f} ✈️\n"
            f"Текущий баланс: <b>{user_balances[uid]:.2f} ✈️</b>",
        )
    except Exception:
        pass


@dp.message(F.text == "🚫 Бан/разбан")
async def admin_ban_menu(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🚫 Забанить", callback_data="ban_start")
    kb.button(text="✅ Разбанить", callback_data="unban_start")
    kb.adjust(2)
    await message.answer("Выберите действие:", reply_markup=kb.as_markup())


@dp.callback_query(F.data == "ban_start")
async def ban_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(BotStates.waiting_ban)
    await call.message.answer("Введите ID пользователя для бана:")
    await call.answer()


@dp.message(BotStates.waiting_ban)
async def ban_apply(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом.")
        return
    if uid == ADMIN_ID:
        await message.answer("❌ Нельзя забанить самого себя.")
        return
    user_banned.add(uid)
    await state.clear()
    await message.answer(f"🚫 Пользователь <code>{uid}</code> забанен.",
                         reply_markup=get_admin_keyboard())
    try:
        await bot.send_message(uid, "🚫 Вы были забанены в боте.")
    except Exception:
        pass


@dp.callback_query(F.data == "unban_start")
async def unban_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(BotStates.waiting_unban)
    await call.message.answer("Введите ID пользователя для разбана:")
    await call.answer()


@dp.message(BotStates.waiting_unban)
async def unban_apply(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом.")
        return
    user_banned.discard(uid)
    await state.clear()
    await message.answer(f"✅ Пользователь <code>{uid}</code> разбанен.",
                         reply_markup=get_admin_keyboard())
    try:
        await bot.send_message(uid, "✅ Вы были разбанены в боте.")
    except Exception:
        pass


@dp.message(F.text == "📢 Рассылка")
async def admin_broadcast_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(BotStates.waiting_broadcast)
    await message.answer(f"Введите текст рассылки (получателей: {len(all_user_ids)}).\nОтмена — /cancel")


@dp.message(Command("cancel"))
async def cancel_any(message: Message, state: FSMContext):
    await state.clear()
    kb = get_admin_keyboard() if message.from_user.id == ADMIN_ID else get_main_keyboard(message.from_user.id)
    await message.answer("Отменено.", reply_markup=kb)


@dp.message(BotStates.waiting_broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    text = message.text
    sent, failed = 0, 0
    for uid in list(all_user_ids):
        for attempt in range(3):
            try:
                await bot.send_message(uid, text)
                sent += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception:
                failed += 1
                break
        await asyncio.sleep(0.05)
    await message.answer(
        f"📢 Рассылка завершена.\n✅ Доставлено: {sent}\n❌ Ошибок: {failed}",
        reply_markup=get_admin_keyboard(),
    )


@dp.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer()


@dp.callback_query(F.data.startswith("view_req:"))
async def view_request(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    rid = int(call.data.split(":")[1])
    req = withdraw_requests.get(rid)
    if not req or req["status"] != "pending":
        await call.answer("Заявка уже обработана", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"approve:{rid}")
    kb.button(text="❌ Отклонить", callback_data=f"reject:{rid}")
    kb.adjust(2)

    type_map = {"bot": "🤖 Вывод в бота", "stars": "⭐ Stars", "crypto": "💵 CryptoBot"}
    details_line = ""
    if req.get("details"):
        details_line = f"\nРеквизиты: <code>{req['details']}</code>"

    await call.message.edit_text(
        f"📄 <b>Заявка №{rid}</b>\n\n"
        f"Тип: {type_map.get(req['type'], req['type'])}\n"
        f"Пользователь: {req['username']}\n"
        f"ID: <code>{req['user_id']}</code>\n"
        f"Сумма: <b>{req['amount']:.2f} ✈️</b>\n"
        f"Время: {req['created']}"
        f"{details_line}",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("approve:"))
async def approve_request(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    rid = int(call.data.split(":")[1])
    req = withdraw_requests.get(rid)
    if not req or req["status"] != "pending":
        await call.answer("Заявка уже обработана", show_alert=True)
        return
    req["status"] = "approved"
    await call.message.edit_text(call.message.html_text + "\n\n✅ <b>ОДОБРЕНО</b>")
    await call.answer("Одобрено")

    type_map = {"bot": "в бота", "stars": "в ⭐", "crypto": "в 💵"}
    try:
        await bot.send_message(
            req["user_id"],
            f"✅ Ваша заявка №{rid} на вывод {req['amount']:.2f} ✈️ {type_map.get(req['type'], '')} одобрена!\n"
            f"Ожидайте зачисления.",
        )
    except Exception as e:
        logger.error(e)


@dp.callback_query(F.data.startswith("reject:"))
async def reject_request(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    rid = int(call.data.split(":")[1])
    req = withdraw_requests.get(rid)
    if not req or req["status"] != "pending":
        await call.answer("Заявка уже обработана", show_alert=True)
        return
    req["status"] = "rejected"
    user_balances[req["user_id"]] = user_balances.get(req["user_id"], 0.0) + req["amount"]
    await call.message.edit_text(call.message.html_text + "\n\n❌ <b>ОТКЛОНЕНО</b> (средства возвращены)")
    await call.answer("Отклонено")
    try:
        await bot.send_message(
            req["user_id"],
            f"❌ Ваша заявка №{rid} на вывод <b>{req['amount']:.2f} ✈️</b> отклонена.\n"
            f"Средства возвращены на баланс.",
        )
    except Exception as e:
        logger.error(e)


@dp.message()
async def fallback(message: Message):
    await message.answer("🤖 Отправь /start, чтобы начать.")


async def on_shutdown(bot: Bot):
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        logger.info("aiohttp сессия закрыта")


async def main():
    if not BOT_TOKEN or not DARKBOOST_API_KEY:
        raise RuntimeError("BOT_TOKEN и DARKBOOST_API_KEY должны быть заданы")

    retry_mw = RetryAfterMiddleware()
    dp.message.middleware(retry_mw)
    dp.callback_query.middleware(retry_mw)

    dp.shutdown.register(on_shutdown)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Кликер запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())