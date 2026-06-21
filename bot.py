"""
StarCases backend — приём оплаты Telegram Stars + хранение баланса пользователей.

Что делает этот файл:
1. Запускает Telegram-бота (long polling, aiogram 3.x).
2. Поднимает HTTP API (aiohttp) с тремя эндпоинтами для фронтенда (cases_twa.html):
     GET  /api/balance?user_id=...           -> {"balance": 123}
     POST /api/invoice   {"user_id":..,"amount":..}   -> {"invoice_url": "..."}
     POST /api/withdraw  {"user_id":..,"item_name":..,"item_value":..} -> {"ok": true}
3. Слушает успешные платежи (successful_payment) и начисляет звёзды НА СЕРВЕРЕ —
   фронтенд никогда сам не увеличивает баланс, поэтому это не подделать через консоль браузера.

Установка зависимостей:
    pip install aiogram aiohttp aiohttp-cors

Переменные окружения:
    BOT_TOKEN   — токен бота от @BotFather
    ADMIN_CHAT_ID — твой Telegram user_id, туда будут приходить заявки на вывод подарков

Запуск:
    BOT_TOKEN=123456:ABC... ADMIN_CHAT_ID=123456789 python bot.py
"""

import asyncio
import os
import sqlite3
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery
from aiogram.filters import CommandStart
from aiohttp import web
import aiohttp_cors

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))
DB_PATH = "starcases.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------------------------------------------------------------------------
# БАЗА ДАННЫХ (sqlite, баланс пользователей)
# ---------------------------------------------------------------------------

def db_init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS balances (user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    conn.close()


def get_balance(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT balance FROM balances WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else 0


def add_balance(user_id: int, amount: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO balances(user_id, balance) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?",
        (user_id, amount, amount),
    )
    conn.commit()
    new_balance = get_balance(user_id)
    conn.close()
    return new_balance


# ---------------------------------------------------------------------------
# TELEGRAM BOT: команды и оплата Stars
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "Привет! Открой StarCases через кнопку меню, чтобы открывать кейсы и играть.\n"
        f"Твой текущий баланс: ⭐ {get_balance(message.from_user.id)}"
    )


@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    # Подтверждаем платёж — без этого Stars-инвойс не пройдёт
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment = message.successful_payment
    # payload в формате "topup:<amount>" — см. create_invoice_link ниже
    try:
        amount = int(payment.invoice_payload.split(":")[1])
    except Exception:
        amount = payment.total_amount  # total_amount в Stars приходит как есть

    new_balance = add_balance(message.from_user.id, amount)
    await message.answer(f"Зачислено ⭐ {amount}. Текущий баланс: ⭐ {new_balance}")


# ---------------------------------------------------------------------------
# HTTP API для фронтенда (cases_twa.html)
# ---------------------------------------------------------------------------

async def api_balance(request: web.Request):
    user_id = int(request.query.get("user_id", "0"))
    return web.json_response({"balance": get_balance(user_id)})


async def api_invoice(request: web.Request):
    data = await request.json()
    user_id = int(data["user_id"])
    amount = int(data["amount"])  # сумма в Stars

    # Создаём реальную invoice-ссылку через Bot API.
    # currency="XTR" — это и есть Telegram Stars.
    invoice_url = await bot.create_invoice_link(
        title=f"Пополнение баланса StarCases",
        description=f"Пополнение на {amount} звёзд",
        payload=f"topup:{amount}",
        provider_token="",  # для Stars provider_token не нужен
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount} Stars", amount=amount)],
    )
    return web.json_response({"invoice_url": invoice_url})


async def api_withdraw(request: web.Request):
    data = await request.json()
    user_id = data.get("user_id")
    item_name = data.get("item_name")
    item_value = data.get("item_value")

    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"📦 Новая заявка на вывод\n"
                f"User ID: {user_id}\n"
                f"Подарок: {item_name} (⭐ {item_value})",
            )
        except Exception as e:
            logging.warning("Не удалось уведомить админа: %s", e)

    return web.json_response({"ok": True})


def build_app() -> web.Application:
    app = web.Application()
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*",
        )
    })
    routes = [
        web.get("/api/balance", api_balance),
        web.post("/api/invoice", api_invoice),
        web.post("/api/withdraw", api_withdraw),
    ]
    for route in routes:
        cors.add(app.router.add_route(route.method, route.path, route.handler))
    return app


async def main():
    db_init()

    http_app = build_app()
    runner = web.AppRunner(http_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "8080")))
    await site.start()
    logging.info("HTTP API запущен на порту %s", os.environ.get("PORT", "8080"))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
