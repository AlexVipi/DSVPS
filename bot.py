import requests
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
import subprocess
import json
import asyncio
from telegram import ReplyKeyboardMarkup
from telegram import LabeledPrice
from telegram.ext import PreCheckoutQueryHandler
from telegram.ext import MessageHandler, filters
import time

# запускаем API
subprocess.Popen(["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"])


TOKEN = "8649755881:AAF0xu_TCORH71JchmDSg2AezaQmuNWbP2s"
API_URL = "http://127.0.0.1:8000"


ADMIN_IDS = [1585155165, 456738861]


def is_admin(user_id):
    return user_id in ADMIN_IDS

async def set_commands(app):
    commands = [
        BotCommand("start", "🚀 Запуск"),
    ]
    await app.bot.set_my_commands(commands)





async def extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.message.reply_text("💳 Скоро будет оплата 😄")


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    prices = [LabeledPrice("VPN 30 дней", 1  * 1)]  # 100 ⭐

    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title="VPN подписка",
        description="Доступ на 30 дней",
        payload="vpn_30",
        provider_token="",  # ОБЯЗАТЕЛЬНО пустой для Stars
        currency="XTR",  # Stars
        prices=prices,
    )



async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)



async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # вызываем API (СДЕЛАЙ /buy endpoint!)
    res = requests.post(f"{API_URL}/buy/{user_id}").json()

    if "error" in res:
        return await update.message.reply_text("❌ Ошибка при выдаче VPN")

    await update.message.reply_text("✅ Оплата прошла! VPN выдан")

    # отправляем конфиг
    await update.message.reply_document(open(res["config_file"], "rb"))
    await update.message.reply_photo(open(res["qr_file"], "rb"))



async def myvpn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)

    try:
        res = requests.get(f"{API_URL}/status/{user_id}").json()
    except:
        return await query.message.reply_text("❌ Ошибка сервера")

    if not res.get("active"):
        return await query.message.reply_text("❌ У тебя нет активной подписки")

    expires = res["expires"]
    now = int(time.time())
    remaining = expires - now

    hours = remaining // 3600
    minutes = (remaining % 3600) // 60

    text = (
        f"📊 *Моя подписка*\n\n"
        f"Статус: ✅ Активна\n"
        f"IP: {res['ip']}\n"
        f"Осталось: {hours}ч {minutes}м"
    )

    keyboard = [
        InlineKeyboardButton("🔄 Продлить", callback_data="extend"),
        InlineKeyboardButton("❌ Удалить", callback_data="delete_me")
    ]

    await query.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ===== команда /start =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if is_admin(user_id):
        keyboard = [
            [
                InlineKeyboardButton("🚀 Trial", callback_data="trial"),
                InlineKeyboardButton("💳 Купить", callback_data="buy"),
                InlineKeyboardButton("📥 Скачать конфиг", callback_data="get_conf")    
            ],
            [
                InlineKeyboardButton("👥 Пользователи", callback_data="users"),
                InlineKeyboardButton("❌ Удалить", callback_data="delete"),
		InlineKeyboardButton("📊 Моя подписка", callback_data="myvpn")
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton("🚀 Получить VPN", callback_data="trial"),
                InlineKeyboardButton("💳 Купить", callback_data="buy"),
		InlineKeyboardButton("📊 Моя подписка", callback_data="myvpn"),
                InlineKeyboardButton("📥 Скачать конфиг", callback_data="get_conf")
            ]
        ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Выбери действие:",
        reply_markup=reply_markup
    )
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
        # отправляем файл
        await message.reply_document(open(conf_path, "rb"))

        # отправляем QR
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

    text = "👥 Пользователи:\n\n"

    for uid, data in db.items():
        text += f"{uid} → {data['ip']}\n"

    await query.message.reply_text(text)

async def delete_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        return await query.message.reply_text("❌ Нет доступа")

    context.user_data["awaiting_delete"] = True

    await query.message.reply_text("Введи user_id для удаления")

# ===== обработка текста (после кнопки delete) =====
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_delete"):
        user_id = update.message.text

        res = requests.delete(f"{API_URL}/delete/{user_id}").json()

        if "error" in res:
            await update.message.reply_text("❌ Не найден")
        else:
            await update.message.reply_text("✅ Пользователь удалён")

        context.user_data["awaiting_delete"] = False

# ===== обработчик кнопок =====
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "trial":
        await trial(update, context)

    elif data == "users":
        await users(update, context)

    elif data == "delete":
        await delete_prompt(update, context)
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





# ===== запуск =====
app = ApplicationBuilder().token(TOKEN).post_init(set_commands).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(CommandHandler("trial", trial))  # fallback
app.add_handler(CommandHandler("users", users))  # fallback
app.add_handler(CommandHandler("delete", delete_prompt))  # fallback
app.add_handler(CommandHandler("start", start))
app.add_handler(PreCheckoutQueryHandler(pre_checkout))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))


from telegram.ext import MessageHandler, filters
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

print("Bot started...")
app.run_polling()

