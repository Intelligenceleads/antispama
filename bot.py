import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Deque, Dict, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties          # <-- НОВЫЙ ИМПОРТ
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = int(os.getenv("CHAT_ID", "-1003976779838"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "7624953181"))
DEFAULT_MODE = os.getenv("KICK_MODE", "kick").lower()  # kick | ban
DEFAULT_THRESHOLD = int(os.getenv("SPAM_THRESHOLD", "5"))
SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", "settings.json"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в .env или переменных окружения")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("pozor_mgn_antispam")

dp = Dispatcher()

history: Dict[int, Deque[Tuple[float, str]]] = defaultdict(lambda: deque(maxlen=8))

URL_RE = re.compile(r"https?://\S+|www\.\S+|t\.me/\S+", re.IGNORECASE)
USERNAME_MENTION_RE = re.compile(r"@[a-zA-Z0-9_]{5,}")
REPEAT_CHAR_RE = re.compile(r"(.)\1{7,}", re.IGNORECASE)

SPAM_WORDS = {
    "казино", "ставки", "беттинг", "букмекер", "раскрутка", "заработок",
    "инвестиции", "инвестор", "удвоим", "вывод денег", "легкий заработок",
    "крипта", "airdrop", "розыгрыш usdt", "giveaway", "free usdt",
    "накрутка", "продам аккаунт", "куплю аккаунт", "слив базы", "пробив",
}


def load_settings() -> dict:
    settings = {
        "enabled": True,
        "mode": DEFAULT_MODE if DEFAULT_MODE in {"kick", "ban"} else "kick",
        "threshold": max(1, DEFAULT_THRESHOLD),
    }
    try:
        if SETTINGS_FILE.exists():
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            settings.update({k: saved[k] for k in settings if k in saved})
    except Exception as exc:
        logger.warning("Не удалось прочитать settings.json: %s", exc)
    return settings


settings = load_settings()


def save_settings() -> None:
    try:
        SETTINGS_FILE.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("Не удалось сохранить настройки: %s", exc)


def admin_only(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id == ADMIN_ID and message.chat.type == "private"


def admin_keyboard() -> InlineKeyboardMarkup:
    state = "🟢 ВКЛ" if settings["enabled"] else "🔴 ВЫКЛ"
    mode = "Удалять + кик" if settings["mode"] == "kick" else "Удалять + бан"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Антиспам: {state}", callback_data="toggle")],
            [
                InlineKeyboardButton(text="⚡ Кик", callback_data="mode:kick"),
                InlineKeyboardButton(text="🔨 Бан", callback_data="mode:ban"),
            ],
            [
                InlineKeyboardButton(text="Порог −", callback_data="threshold:minus"),
                InlineKeyboardButton(text=f"Порог: {settings['threshold']}", callback_data="noop"),
                InlineKeyboardButton(text="Порог +", callback_data="threshold:plus"),
            ],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh")],
        ]
    )


def admin_text() -> str:
    state = "🟢 включён" if settings["enabled"] else "🔴 выключен"
    mode = "кик (удалить из чата)" if settings["mode"] == "kick" else "бан (постоянная блокировка)"
    return (
        "🛡 <b>ПОЗОР МГН АНТИСПАМ</b>\n\n"
        f"Чат: <code>{CHAT_ID}</code>\n"
        f"Статус: <b>{state}</b>\n"
        f"Режим: <b>{mode}</b>\n"
        f"Порог срабатывания: <b>{settings['threshold']}</b>\n\n"
        "Настройки применяются сразу."
    )


async def is_chat_admin(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHAT_ID, user_id)
        return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


def normalize_text(text: str) -> str:
    text = text.lower().strip()
    return re.sub(r"\s+", " ", text)


def spam_score(text: str, user_id: int) -> Tuple[int, list[str]]:
    text_n = normalize_text(text)
    score = 0
    reasons: list[str] = []

    url_count = len(URL_RE.findall(text_n))
    if url_count >= 2:
        score += 4
        reasons.append(f"ссылок: {url_count}")
    elif url_count == 1:
        score += 1
        reasons.append("ссылка")

    if any(word in text_n for word in SPAM_WORDS):
        score += 4
        reasons.append("рекламный/скам-текст")

    if REPEAT_CHAR_RE.search(text_n):
        score += 2
        reasons.append("повтор символов")

    if len(text_n) > 450 and URL_RE.search(text_n):
        score += 2
        reasons.append("длинный текст со ссылкой")

    mentions = len(USERNAME_MENTION_RE.findall(text_n))
    if mentions >= 5:
        score += 3
        reasons.append(f"много упоминаний: {mentions}")

    now = time.time()
    user_history = history[user_id]
    recent = [item for item in user_history if now - item[0] <= 15]
    if any(old_text == text_n and text_n for _, old_text in recent):
        score += 5
        reasons.append("повторное сообщение")

    user_history.append((now, text_n))
    return score, reasons


async def remove_and_kick(message: Message, reason: str) -> None:
    if not message.from_user:
        return

    user_id = message.from_user.id
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("Не удалось удалить сообщение %s: %s", message.message_id, exc)

    bot = message.bot
    try:
        await bot.ban_chat_member(chat_id=CHAT_ID, user_id=user_id, revoke_messages=True)
        if settings["mode"] == "kick":
            await bot.unban_chat_member(chat_id=CHAT_ID, user_id=user_id, only_if_banned=True)
        logger.info(
            "Модерация: user_id=%s username=%s reason=%s mode=%s",
            user_id, message.from_user.username, reason, settings["mode"],
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.error("Не удалось удалить/забанить user_id=%s: %s", user_id, exc)


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if admin_only(message):
        await message.answer(admin_text(), reply_markup=admin_keyboard())
        return
    if message.chat.id == CHAT_ID:
        await message.answer(
            "🛡 <b>ПОЗОР МГН АНТИСПАМ</b>\n\nАнтиспам активен."
        )


@dp.message(Command("settings", "admin", "panel"))
async def cmd_settings(message: Message) -> None:
    if admin_only(message):
        await message.answer(admin_text(), reply_markup=admin_keyboard())


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if admin_only(message):
        await message.answer(admin_text(), reply_markup=admin_keyboard())
    elif message.chat.id == CHAT_ID:
        await message.answer(
            f"🛡 <b>Антиспам активен</b>\nЧат: <code>{CHAT_ID}</code>\nРежим: <code>{settings['mode']}</code>"
        )


@dp.callback_query(F.from_user.id == ADMIN_ID)
async def admin_callbacks(callback: CallbackQuery) -> None:
    data = callback.data or ""
    if data == "toggle":
        settings["enabled"] = not settings["enabled"]
        save_settings()
    elif data == "mode:kick":
        settings["mode"] = "kick"
        save_settings()
    elif data == "mode:ban":
        settings["mode"] = "ban"
        save_settings()
    elif data == "threshold:minus":
        settings["threshold"] = max(1, settings["threshold"] - 1)
        save_settings()
    elif data == "threshold:plus":
        settings["threshold"] = min(15, settings["threshold"] + 1)
        save_settings()
    elif data in {"refresh", "noop"}:
        pass
    else:
        await callback.answer("Неизвестная команда", show_alert=False)
        return

    await callback.answer("Сохранено")
    if callback.message:
        await callback.message.edit_text(admin_text(), reply_markup=admin_keyboard())


@dp.message(F.chat.id == CHAT_ID)
async def moderate_message(message: Message) -> None:
    if not settings["enabled"] or not message.from_user:
        return

    if message.from_user.id == ADMIN_ID or await is_chat_admin(message.bot, message.from_user.id):
        return

    text = message.text or message.caption or ""
    if not text:
        return

    score, reasons = spam_score(text, message.from_user.id)
    if score >= settings["threshold"]:
        reason = ", ".join(reasons) or "подозрительное сообщение"
        await remove_and_kick(message, reason)


async def main() -> None:
    # ✅ ИСПРАВЛЕННАЯ ИНИЦИАЛИЗАЦИЯ БОТА
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    me = await bot.get_me()
    logger.info("Запущен @%s (%s)", me.username, me.id)
    logger.info("Чат антиспама: %s, админ: %s", CHAT_ID, ADMIN_ID)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())



   
  


