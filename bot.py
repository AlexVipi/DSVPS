import requests
import os
import re
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
import subprocess
import json
import asyncio
from telegram import LabeledPrice
from telegram.ext import PreCheckoutQueryHandler
from telegram.ext import MessageHandler, filters
import time
import config as cfg

# Запускаем API
subprocess.Popen(["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TXID_RE = re.compile(r'^([0-9a-fA-F]{64}|[A-Za-z0-9+/_\-]{43,44}=?)$')
_pending_polls: dict = {}

DB_PATH = "/opt/vpn-service/db.json"
_EXTEND_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Продлить", callback_data="extend")]])

TOKEN = "8649755881:AAF0xu_TCORH71JchmDSg2AezaQmuNWbP2s"
API_URL = "http://127.0.0.1:8000"
ADMIN_IDS = [1585155165, 456738861]


def is_admin(user_id):
    return user_id in ADMIN_IDS



async def _check_subscriptions(bot):
    try:
        with open(DB_PATH) as f:
            db = json.load(f)
    except Exception as e:
        logger.error("notify: failed to load db: %s", e)
        return

    now = int(time.time())
    changed = False

    for user_id, data in db.items():
        if not isinstance(data, dict) or "expires" not in data:
            continue

        expires = data["expires"]
        remaining = expires - now
        is_trial = data.get("is_trial", False)
        notified = data.setdefault("notified", {})

        # (ключ, порог в секундах, текст)
        checks = []
        if not is_trial:
            checks.append(("3d", 3 * 86400,
                "⚠️ *Подписка заканчивается через 3 дня.*\nПродли VPN, чтобы не потерять доступ."))
            checks.append(("1d", 86400,
                "⚠️ *Подписка заканчивается через 1 день.*\nПродли VPN, чтобы не потерять доступ."))
        checks.append(("3h", 3 * 3600,
            "⏰ *Подписка заканчивается через 3 часа.*\nПродли VPN прямо сейчас."))
        checks.append(("1h", 3600,
            "🔴 *Подписка заканчивается через 1 час!*\nСкоро потеряешь доступ к VPN."))

        for key, threshold, msg in checks:
            if not notified.get(key) and 0 < remaining <= threshold:
                try:
                    await bot.send_message(int(user_id), msg, parse_mode="Markdown", reply_markup=_EXTEND_KB)
                    notified[key] = True
                    changed = True
                    logger.info("notify: sent %s to user %s (remaining=%ds)", key, user_id, remaining)
                except Exception as e:
                    logger.warning("notify: failed %s to user %s: %s", key, user_id, e)

        if not notified.get("expired") and remaining <= 0:
            msg = "❌ *Ваша подписка закончилась.*\nПродлите VPN, чтобы восстановить доступ."
            try:
                await bot.send_message(int(user_id), msg, parse_mode="Markdown", reply_markup=_EXTEND_KB)
                notified["expired"] = True
                changed = True
                logger.info("notify: sent expired to user %s", user_id)
            except Exception as e:
                logger.warning("notify: failed expired to user %s: %s", user_id, e)

    if changed:
        try:
            with open(DB_PATH, "w") as f:
                json.dump(db, f)
        except Exception as e:
            logger.error("notify: failed to save db: %s", e)


async def _notification_loop(bot):
    await asyncio.sleep(15)  # дать боту время полностью запуститься
    while True:
        try:
            await _check_subscriptions(bot)
        except Exception as e:
            logger.error("notification loop error: %s", e)
        await asyncio.sleep(60)


async def set_commands(app):
    commands = [
        BotCommand("start", "🚀 Запуск"),
    ]
    await app.bot.set_my_commands(commands)
    asyncio.create_task(_notification_loop(app.bot))


async def _show_period_selection(update, context, action):
    """Показывает кнопки выбора срока подписки (1, 2, 3 месяца)."""
    query = update.callback_query
    message = query.message
    s = cfg.STARS_PER_USD
    keyboard = [
        [InlineKeyboardButton(f"1 месяц — $1 / {s} ⭐",     callback_data=f"{action}_1m")],
        [InlineKeyboardButton(f"2 месяца — $2 / {s * 2} ⭐", callback_data=f"{action}_2m")],
        [InlineKeyboardButton(f"3 месяца — $3 / {s * 3} ⭐", callback_data=f"{action}_3m")],
    ]
    await message.reply_text("🗓 Выберите срок подписки:", reply_markup=InlineKeyboardMarkup(keyboard))


async def _show_network_selection(update, context, action, months):
    """Показывает кнопки выбора способа оплаты (TRC-20, TON или Telegram Stars)."""
    query = update.callback_query
    message = query.message
    stars = cfg.STARS_PER_USD * months
    usd = months  # $1 за месяц
    keyboard = [
        [
            InlineKeyboardButton(f"USDT TRC-20 (${usd})", callback_data=f"{action}_{months}m_tron"),
            InlineKeyboardButton(f"USDT TON (${usd})",    callback_data=f"{action}_{months}m_ton"),
        ],
        [
            InlineKeyboardButton(f"⭐ Telegram Stars ({stars} ⭐)", callback_data=f"{action}_{months}m_stars"),
        ],
    ]
    await message.reply_text("💳 Выберите способ оплаты:", reply_markup=InlineKeyboardMarkup(keyboard))


async def extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _show_period_selection(update, context, 'extend')


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _show_period_selection(update, context, 'buy')


async def _show_stars_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, months: int = 1):
    """Отправляет инвойс Telegram Stars для оплаты VPN."""
    stars = cfg.STARS_PER_USD * months
    payload = f"{action}:{months}"
    title = "VPN подписка"
    description = f"VPN на {months} мес. — {stars} ⭐"
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=title,
        description=description,
        payload=payload,
        provider_token="",  # пустой токен = Telegram Stars
        currency="XTR",
        prices=[LabeledPrice(f"VPN {months} мес.", stars)],
    )


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    payload = update.message.successful_payment.invoice_payload
    try:
        action, months_str = payload.split(":")
        months = int(months_str)
    except Exception:
        action, months = "buy", 1

    if action == "extend":
        res = requests.post(f"{API_URL}/extend/{user_id}", params={"months": months}).json()
    else:
        res = requests.post(f"{API_URL}/buy/{user_id}", params={"months": months}).json()

    if "error" in res:
        return await update.message.reply_text("❌ Ошибка при выдаче VPN")
    await update.message.reply_text(f"✅ Оплата прошла! VPN активирован на {months} мес. 🎉")
    await update.message.reply_document(open(res["config_file"], "rb"))
    await update.message.reply_photo(open(res["qr_file"], "rb"))


async def _show_payment_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, network: str, months: int = 1):
    """
    Создаёт платёжную сессию и показывает пользователю:
      - адрес кошелька (TRC-20 или TON)
      - точную сумму к оплате
      - инструкцию по отправке хэша транзакции
    """
    user_id = str(update.effective_user.id)
    message = update.callback_query.message

    try:
        res = requests.post(
            f"{API_URL}/payment/create/{user_id}",
            params={"action": action, "network": network, "months": months},
        ).json()
    except Exception as e:
        logger.error('payment/create error: %s', e)
        return await message.reply_text("❌ Ошибка сервера. Попробуйте позже.")

    if "error" in res:
        return await message.reply_text(f"❌ {res['error']}")

    session = res["session"]
    wallet = res["wallet"]
    amount = res["amount"]
    expires_at = res["expires_at"]
    minutes = int((expires_at - time.time()) / 60)

    if network == 'ton':
        network_label = 'TON'
        txid_hint = 'хэш транзакции (Transaction Hash) из обозревателя блокчейна'
    else:
        network_label = 'TRC-20'
        txid_hint = 'TXID транзакции'

    months_label = f"{months} мес." if months > 1 else "30 дней"
    text = (
        f"💳 *Оплата VPN ({months_label}) через USDT {network_label}*\n\n"
        f"Отправьте ровно:\n```\n{amount} USDT\n```\n"
        f"На адрес (сеть {network_label}):\n`{wallet}`\n\n"
        f"⏳ Сессия действует *{minutes} минут*\n\n"
        f"После оплаты отправьте {txid_hint} прямо в этот чат."
    )

    context.user_data["payment_id"] = session
    context.user_data["awaiting_txid"] = True
    context.user_data["payment_action"] = action
    context.user_data["payment_network"] = network
    logger.info('Payment invoice shown: user=%s action=%s network=%s pid=%s', user_id, action, network, session)

    await message.reply_text(text, parse_mode="Markdown")


async def _deliver_vpn(bot, chat_id, res):
    """Отправляет пользователю сообщение об успехе и файлы конфига."""
    await bot.send_message(chat_id, "✅ Оплата подтверждена! VPN активирован на 30 дней 🎉")
    try:
        await bot.send_document(chat_id, open(res["config_file"], "rb"))
        await bot.send_photo(chat_id, open(res["qr_file"], "rb"))
    except Exception as e:
        logger.error('Error sending config files: %s', e)
        await bot.send_message(chat_id, "⚠️ Не удалось отправить файл конфига. Используй кнопку «📥 Скачать конфиг».")


async def _poll_txid(bot, chat_id, user_id, txid, expires_at):
    """Фоновая задача: проверяет транзакцию каждые 15 секунд до подтверждения или истечения сессии."""
    logger.info('[POLL] Started polling txid=%s user=%s expires_at=%s', txid, user_id, expires_at)
    while True:
        await asyncio.sleep(15)
        if int(time.time()) >= expires_at:
            logger.info('[POLL] Session expired for user=%s txid=%s', user_id, txid)
            await bot.send_message(chat_id, "⌛ Время ожидания оплаты истекло. Транзакция так и не подтвердилась.\nНажми «💳 Купить» чтобы создать новый счёт.")
            break
        try:
            res = requests.post(f"{API_URL}/payment/verify/{user_id}/{txid}").json()
        except Exception as e:
            logger.warning('[POLL] verify error user=%s: %s', user_id, e)
            continue

        if res.get("pending"):
            logger.info('[POLL] Still pending txid=%s', txid)
            continue

        if "error" in res:
            reason = res.get("reason", res["error"])
            logger.warning('[POLL] Permanent error user=%s txid=%s: %s', user_id, txid, reason)
            await bot.send_message(chat_id, f"❌ {reason}")
            break

        logger.info('[POLL] Confirmed txid=%s user=%s', txid, user_id)
        await _deliver_vpn(bot, chat_id, res)
        break

    _pending_polls.pop(user_id, None)


async def _process_txid(update: Update, context: ContextTypes.DEFAULT_TYPE, txid: str):
    """
    Проверяет TXID через API: валидность формата, уникальность, блокчейн.
    При успехе — отправляет конфиг-файл и QR-код.
    """
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id

    if not TXID_RE.match(txid):
        return await update.message.reply_text(
            "❌ Неверный формат хэша транзакции.\n"
            "Для TRC-20: 64 hex-символа (0–9, a–f).\n"
            "Для TON: хэш из обозревателя блокчейна (tonscan.org, tonviewer.com)."
        )

    network = context.user_data.get("payment_network", "tron")
    network_label = "TON" if network == "ton" else "TRON"

    await update.message.reply_text(f"⏳ Проверяю транзакцию в блокчейне {network_label}…")

    try:
        res = requests.post(f"{API_URL}/payment/verify/{user_id}/{txid}").json()
    except Exception as e:
        logger.error('payment/verify error: %s', e)
        return await update.message.reply_text("❌ Ошибка сервера. Попробуйте через минуту.")

    if res.get("pending"):
        expires_at = int(res.get("expires_at", time.time() + 1800))
        remaining = max(0, expires_at - int(time.time()))
        mins = remaining // 60

        old_task = _pending_polls.pop(user_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        task = asyncio.create_task(_poll_txid(context.application.bot, chat_id, user_id, txid, expires_at))
        _pending_polls[user_id] = task

        await update.message.reply_text(
            f"⏳ Транзакция найдена, но ещё не получила подтверждений сети.\n\n"
            f"Начата автоматическая проверка каждые 15 секунд.\n"
            f"Как только транзакция подтвердится — VPN активируется автоматически.\n"
            f"Осталось времени: ~{mins} мин."
        )
        return

    if "error" in res:
        reason = res.get("reason", res["error"])
        logger.warning('Payment failed user=%s txid=%s reason=%s', user_id, txid, reason)
        return await update.message.reply_text(f"❌ {reason}")

    context.user_data["awaiting_txid"] = False
    context.user_data["payment_action"] = None
    logger.info('Payment success user=%s txid=%s', user_id, txid)
    await _deliver_vpn(context.application.bot, chat_id, res)


async def myvpn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)

    try:
        res = requests.get(f"{API_URL}/status/{user_id}").json()
    except Exception:
        return await query.message.reply_text("❌ Ошибка сервера")

    if not res.get("active"):
        return await query.message.reply_text("❌ У тебя нет активной подписки")

    expires = res["expires"]
    now = int(time.time())
    remaining = expires - now

    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    minutes = (remaining % 3600) // 60

    current_loc = res.get("location", "Server1")
    loc_text = "🇸🇪 Швеция" if current_loc == "Server1" else "🇳🇱 Нидерланды"

    text = (
        f"📊 *Моя подписка*\n\n"
        f"Статус: ✅ Активна\n"
        f"Текущая страна: *{loc_text}*\n"
        f"Осталось: {days:02d} д {hours:02d} ч {minutes:02d} м"
    )

    keyboard = [
        [
            InlineKeyboardButton("🇸🇪 SE", callback_data="set_loc_Server1"),
            InlineKeyboardButton("🇳🇱 NL", callback_data="set_loc_Server2"),
        ],
        [
            InlineKeyboardButton("🔄 Продлить", callback_data="extend"),
        ]
    ]

    await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ===== команда /start =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if is_admin(user_id):
        keyboard = [
            [
                InlineKeyboardButton("🚀 Trial", callback_data="trial"),
                InlineKeyboardButton("💳 Купить", callback_data="buy"),
                InlineKeyboardButton("📥 Скачать конфиг", callback_data="get_conf"),
            ],
            [
                InlineKeyboardButton("👥 Пользователи", callback_data="users"),
                InlineKeyboardButton("❌ Удалить", callback_data="delete"),
                InlineKeyboardButton("📊 Моя подписка", callback_data="myvpn"),
            ],
            [
                InlineKeyboardButton("🎁 Выдать доступ", callback_data="grant"),
                InlineKeyboardButton("❓ Помощь", callback_data="help"),
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton("🚀 Получить VPN", callback_data="trial"),
                InlineKeyboardButton("💳 Купить", callback_data="buy"),
            ],
            [
                InlineKeyboardButton("📊 Моя подписка", callback_data="myvpn"),
                InlineKeyboardButton("📥 Скачать конфиг", callback_data="get_conf"),
            ],
            [
                InlineKeyboardButton("❓ Помощь", callback_data="help"),
            ]
        ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выбери действие:", reply_markup=reply_markup)


# ===== команда /trial =====
async def trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.callback_query.message if update.callback_query else update.message
    user_id = str(update.effective_user.id)

    try:
        res = requests.post(f"{API_URL}/trial/{user_id}").json()
    except Exception as e:
        await message.reply_text("❌ Ошибка подключения к серверу")
        print(e)
        return

    if "error" in res:
        await message.reply_text("❗ Ты уже использовал пробный VPN")
        return

    conf_path = res["config_file"]
    qr_path = res["qr_file"]

    try:
        await message.reply_document(open(conf_path, "rb"))
        await message.reply_photo(open(qr_path, "rb"))
        await message.reply_text("✅ VPN на 24 часа готов!")
    except Exception as e:
        await message.reply_text("❌ Ошибка при отправке файлов")
        print(e)


async def send_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    res = requests.get(f"{API_URL}/config/{user_id}").json()

    if "error" in res:
        return await query.message.reply_text("❌ У тебя нет VPN")

    try:
        await query.message.reply_document(open(res["config_file"], "rb"))
        await query.message.reply_photo(open(res["qr_file"], "rb"))
        await query.message.reply_text("✅ Вот твой конфиг")
    except Exception as e:
        await query.message.reply_text("❌ Ошибка отправки")
        print(e)


async def users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return await query.message.reply_text("❌ Нет доступа")

    db = json.load(open("/opt/vpn-service/db.json"))
    now = int(time.time())
    text = "👥 Пользователи:\n\n"

    for uid, data in db.items():
        if not isinstance(data, dict):
            continue
        remaining = data.get("expires", 0) - now
        if remaining > 0:
            days = remaining // 86400
            hours = (remaining % 86400) // 3600
            mins = (remaining % 3600) // 60
            time_str = f"{days:02d} д {hours:02d} ч {mins:02d} м"
        else:
            time_str = "истекла"
        trial_str = "trial использован" if data.get("trial_used") else "trial не использован"
        text += f"`{uid}` | {data.get('ip', '?')} | {time_str} | {trial_str}\n"

    await query.message.reply_text(text, parse_mode="Markdown")


async def delete_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        return await query.message.reply_text("❌ Нет доступа")

    keyboard = [
        [
            InlineKeyboardButton("✏️ Ввести ID", callback_data="delete_by_id"),
            InlineKeyboardButton("👥 Список пользователей", callback_data="delete_from_list"),
        ]
    ]
    await query.message.reply_text("Как хочешь удалить пользователя?", reply_markup=InlineKeyboardMarkup(keyboard))


async def delete_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        return

    context.user_data["awaiting_delete"] = True
    await query.message.reply_text("Введи user_id для удаления:")


async def delete_from_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        return

    db = json.load(open("/opt/vpn-service/db.json"))
    now = int(time.time())

    keyboard = []
    for uid, data in db.items():
        if not isinstance(data, dict):
            continue
        ip = data.get("ip", "?")
        remaining = data.get("expires", 0) - now
        if remaining > 0:
            days = remaining // 86400
            label = f"{uid} | {ip} | {days} д"
        else:
            label = f"{uid} | {ip} | истекла"
        keyboard.append([InlineKeyboardButton(f"❌ {label}", callback_data=f"del_user_{uid}")])

    if keyboard:
        await query.message.reply_text("Выбери пользователя для удаления:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await query.message.reply_text("Нет пользователей.")


async def delete_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    res = requests.delete(f"{API_URL}/delete/{user_id}").json()

    if "error" in res:
        await query.message.reply_text("❌ VPN не найден")
    else:
        await query.message.reply_text("✅ Подписка удалена")


def _parse_grant_duration(tokens):
    """Парсит токены вида ['1d', '2h', '30m'] в (days, hours, minutes).
    Чистое число без суффикса считается днями."""
    days = hours = minutes = 0
    for token in tokens:
        m = re.fullmatch(r'(\d+)([dhm])', token)
        if m:
            val = int(m.group(1))
            unit = m.group(2)
            if unit == 'd':
                days += val
            elif unit == 'h':
                hours += val
            elif unit == 'm':
                minutes += val
        elif token.isdigit():
            days += int(token)
        else:
            return None
    return days, hours, minutes


def _duration_label(days, hours, minutes):
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} м")
    return " ".join(parts) if parts else "0 м"


async def grant_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        return await query.message.reply_text("❌ Нет доступа")

    context.user_data["awaiting_grant"] = True
    await query.message.reply_text(
        "Введи *user\_id* и продолжительность. Суффиксы: `d` — дни, `h` — часы, `m` — минуты.\n\n"
        "Примеры:\n"
        "`123456789 30d` — 30 дней\n"
        "`123456789 12h` — 12 часов\n"
        "`123456789 90m` — 90 минут\n"
        "`123456789 1d 12h 30m` — 1 день 12 часов 30 минут",
        parse_mode="Markdown"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = str(update.effective_user.id)

    if context.user_data.get("awaiting_delete"):
        res = requests.delete(f"{API_URL}/delete/{text}").json()
        if "error" in res:
            await update.message.reply_text("❌ Не найден")
        else:
            await update.message.reply_text("✅ Пользователь удалён")
        context.user_data["awaiting_delete"] = False
        return

    if context.user_data.get("awaiting_grant"):
        parts = text.split()
        if len(parts) < 2:
            return await update.message.reply_text(
                "❌ Неверный формат. Пример: `123456789 1d 12h 30m`",
                parse_mode="Markdown"
            )
        target_id = parts[0]
        parsed = _parse_grant_duration(parts[1:])
        if parsed is None:
            return await update.message.reply_text(
                "❌ Неверный формат времени. Используй суффиксы `d`, `h`, `m`.\nПример: `123456789 1d 12h 30m`",
                parse_mode="Markdown"
            )
        days, hours, minutes = parsed
        total_seconds = days * 86400 + hours * 3600 + minutes * 60
        if total_seconds <= 0:
            return await update.message.reply_text("❌ Укажи хотя бы одну единицу времени больше нуля")
        if total_seconds > 3650 * 86400:
            return await update.message.reply_text("❌ Максимальный срок — 3650 дней")
        try:
            res = requests.post(
                f"{API_URL}/grant/{target_id}",
                params={"days": days, "hours": hours, "minutes": minutes}
            ).json()
        except Exception as e:
            logger.error('grant error: %s', e)
            return await update.message.reply_text("❌ Ошибка сервера")
        if "error" in res:
            return await update.message.reply_text(f"❌ {res['error']}")
        from datetime import datetime
        expires_str = datetime.fromtimestamp(res["expires"]).strftime('%d.%m.%Y %H:%M')
        label = _duration_label(days, hours, minutes)
        await update.message.reply_text(
            f"✅ Доступ выдан пользователю `{target_id}` на *{label}*.\nДействует до: {expires_str}",
            parse_mode="Markdown"
        )
        try:
            await update.message.reply_document(open(res["config_file"], "rb"))
            await update.message.reply_photo(open(res["qr_file"], "rb"))
        except Exception as e:
            logger.error('Error sending granted config to admin: %s', e)
        try:
            await context.bot.send_message(
                int(target_id),
                f"🎉 Вам выдан VPN-доступ на *{label}*!\nДействует до: {expires_str}\n\nНиже — ваш конфиг и QR-код для подключения.",
                parse_mode="Markdown"
            )
            await context.bot.send_document(int(target_id), open(res["config_file"], "rb"))
            await context.bot.send_photo(int(target_id), open(res["qr_file"], "rb"))
        except Exception as e:
            logger.error('Error sending granted config to user %s: %s', target_id, e)
            await update.message.reply_text(
                f"⚠️ Не удалось отправить конфиг пользователю `{target_id}`. Возможно, он ещё не запускал бота.",
                parse_mode="Markdown"
            )
        context.user_data["awaiting_grant"] = False
        return

    is_txid_format = bool(TXID_RE.match(text))
    if context.user_data.get("awaiting_txid") or is_txid_format:
        try:
            res = requests.get(f"{API_URL}/payment/active/{user_id}").json()
            active = bool(res.get("session"))
        except Exception:
            active = False
        if active:
            if is_txid_format and not context.user_data.get("awaiting_txid"):
                context.user_data["awaiting_txid"] = True
                context.user_data["payment_action"] = res.get("action", "buy")
                context.user_data["payment_network"] = res.get("network", "tron")
            await _process_txid(update, context, text)


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        "❓ *Инструкция: от покупки до подключения*\n\n"

        "*1. Покупка подписки*\n"
        "Нажми *«💳 Купить»*, выбери срок (1, 2 или 3 месяца) и способ оплаты:\n"
        "• USDT TRC-20 — переводишь USDT в сети Tron, вставляешь TXID транзакции\n"
        "• USDT TON — переводишь USDT в сети TON, вставляешь хэш транзакции\n"
        "• Telegram Stars — оплата прямо в Telegram одним нажатием\n\n"

        "*2. Установка приложения*\n"
        "Скачай WireGuard для своего устройства:\n"
        "• Android: [Google Play](https://play.google.com/store/apps/details?id=com.wireguard.android)\n"
        "• iOS: [App Store](https://apps.apple.com/app/wireguard/id1441195209)\n"
        "• Windows: [wireguard.com](https://www.wireguard.com/install/)\n"
        "• macOS: [App Store](https://apps.apple.com/app/wireguard/id1451685025)\n\n"

        "*3. Добавление конфига*\n"
        "После оплаты бот пришлёт .conf файл и QR-код.\n"
        "• *Телефон (QR):* открой WireGuard → нажми «+» → *Сканировать QR-код* → наведи камеру\n"
        "• *Телефон (файл):* открой WireGuard → нажми «+» → *Импорт из файла* → выбери .conf файл\n"
        "• *ПК:* открой WireGuard → *Импортировать туннель из файла* → выбери .conf файл\n\n"

        "*4. Подключение*\n"
        "В приложении WireGuard нажми переключатель рядом с туннелем. "
        "Готово — ты подключён к VPN!\n\n"

        "*5. Выбор сервера*\n"
        "В разделе *«📊 Моя подписка»* можно переключаться между серверами.\n\n"

        "*6. Скачать конфиг повторно*\n"
        "Нажми *«📥 Скачать конфиг»* — бот пришлёт актуальный файл и QR-код.\n\n"

        "*7. Продление*\n"
        "Бот уведомит за 3 дня, 1 день, 3 и 1 час до окончания подписки. "
        "Продлить можно в *«📊 Моя подписка»* → *«🔄 Продлить»*."
    )

    await query.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)


# ===== обработчик кнопок =====
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = str(query.from_user.id)

    if data.startswith("del_user_"):
        if not is_admin(update.effective_user.id):
            await query.answer("❌ Нет доступа", show_alert=True)
            return
        target_id = data.replace("del_user_", "")
        try:
            res = requests.delete(f"{API_URL}/delete/{target_id}").json()
            if "error" in res:
                await query.answer("❌ Не найден", show_alert=True)
            else:
                await query.answer(f"✅ Пользователь {target_id} удалён", show_alert=True)
                # Убираем кнопку удалённого пользователя из сообщения
                old_markup = query.message.reply_markup
                if old_markup:
                    new_rows = [
                        row for row in old_markup.inline_keyboard
                        if not any(btn.callback_data == data for btn in row)
                    ]
                    new_markup = InlineKeyboardMarkup(new_rows) if new_rows else None
                    await query.message.edit_reply_markup(reply_markup=new_markup)
        except Exception as e:
            await query.answer("❌ Ошибка при удалении", show_alert=True)
            print(f"del_user_ error: {e}")
        return

    if data.startswith("set_loc_"):
        location = data.replace("set_loc_", "")
        try:
            requests.post(f"{API_URL}/set_location/{user_id}/{location}").json()
            loc_name = "Швеция 🇸🇪" if location == "Server1" else "Нидерланды 🇳🇱"
            await query.answer(f"Страна изменена на {loc_name}!", show_alert=True)
            return await myvpn(update, context)
        except Exception as e:
            print(f"Error changing location: {e}")
            await query.answer("❌ Ошибка при смене локации", show_alert=True)
            return

    await query.answer()

    if data == "trial":
        await trial(update, context)
    elif data == "users":
        await users(update, context)
    elif data == "delete":
        await delete_prompt(update, context)
    elif data == "delete_by_id":
        await delete_by_id(update, context)
    elif data == "delete_from_list":
        await delete_from_list(update, context)
    elif data == "grant":
        await grant_prompt(update, context)
    elif data == "buy":
        await buy(update, context)
    elif data == "myvpn":
        await myvpn(update, context)
    elif data == "delete_me":
        await delete_me(update, context)
    elif data == "extend":
        await extend(update, context)
    elif data == "get_conf":
        await send_config(update, context)
    elif data == "help":
        await help_handler(update, context)
    else:
        # Паттерн выбора периода: buy_1m, extend_2m, ...
        m = re.fullmatch(r'(buy|extend)_(\d+)m', data)
        if m:
            action, months = m.group(1), int(m.group(2))
            await _show_network_selection(update, context, action, months)
            return
        # Паттерн выбора сети: buy_1m_tron, extend_2m_stars, ...
        m = re.fullmatch(r'(buy|extend)_(\d+)m_(tron|ton|stars)', data)
        if m:
            action, months, network = m.group(1), int(m.group(2)), m.group(3)
            if network == "stars":
                await _show_stars_invoice(update, context, action=action, months=months)
            else:
                await _show_payment_invoice(update, context, action=action, network=network, months=months)


# ===== запуск =====
app = ApplicationBuilder().token(TOKEN).post_init(set_commands).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(CommandHandler("trial", trial))
app.add_handler(CommandHandler("users", users))
app.add_handler(CommandHandler("delete", delete_prompt))
app.add_handler(PreCheckoutQueryHandler(pre_checkout))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

print("Bot started...")
app.run_polling()
