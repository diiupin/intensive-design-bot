import os
import logging
from decimal import Decimal
from uuid import uuid4

import requests
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    Update
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
YOOKASSA_SHOP_ID = os.environ["YOOKASSA_SHOP_ID"]
YOOKASSA_SECRET_KEY = os.environ["YOOKASSA_SECRET_KEY"]
CHANNEL_ID = os.environ["CHANNEL_ID"]  # e.g. -1001234567890 or @channelusername
PRICE_RUB = Decimal(os.getenv("PRICE_RUB", "1990.00"))
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Интенсив Di Sign")
BOT_USERNAME = os.getenv("BOT_USERNAME", "intensive_design_bot")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
app = FastAPI(title="Intensive Di Sign Bot")


def money(value: Decimal) -> str:
    return f"{value:.2f}"


def create_payment(telegram_id: int) -> dict:
    """Create a YooKassa redirect payment with Telegram ID in metadata."""
    payload = {
        "amount": {"value": money(PRICE_RUB), "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": f"https://t.me/{BOT_USERNAME}",
        },
        "description": PRODUCT_NAME,
        "metadata": {
            "telegram_id": str(telegram_id),
            "product": PRODUCT_NAME,
        },
    }

    # YooKassa requires a unique Idempotence-Key per payment creation attempt.
    r = requests.post(
        "https://api.yookassa.ru/v3/payments",
        auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        headers={
            "Content-Type": "application/json",
            "Idempotence-Key": str(uuid4()),
        },
        json=payload,
        timeout=20,
    )
    if r.status_code >= 400:
        log.error("YooKassa error %s: %s", r.status_code, r.text)
        raise RuntimeError(f"YooKassa error {r.status_code}: {r.text}")
    return r.json()


async def send_access(telegram_id: int):
    """Create a one-person invite link and send it to the buyer."""
    # If the user is already in the channel, don't create/send another link.
    try:
        member = await bot.get_chat_member(CHANNEL_ID, telegram_id)
        if member.status in {"member", "administrator", "creator"}:
            await bot.send_message(
                telegram_id,
                "Ты уже в канале 💅\nЕсли потеряла ссылку — она тебе не нужна, доступ уже есть."
            )
            return
    except Exception:
        # A user who isn't in the channel can produce an error here; that's fine.
        pass

    invite = await bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        name=f"purchase-{telegram_id}",
        member_limit=1,
    )

    await bot.send_message(
        telegram_id,
        "🎉 Оплата прошла!\n\n"
        f"Добро пожаловать в {PRODUCT_NAME}.\n"
        "Нажми кнопку ниже — ссылка рассчитана на один вход.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🎓 Войти в канал", url=invite.invite_link)]
            ]
        ),
    )


@dp.message(CommandStart())
async def start(message: Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"💳 Купить доступ — {money(PRICE_RUB)} ₽",
                callback_data="buy"
            )],
        ]
    )
    await message.answer(
        f"🎓 <b>{PRODUCT_NAME}</b>\n\n"
        "Единоразовый доступ к закрытому каналу.\n"
        f"Стоимость: <b>{money(PRICE_RUB)} ₽</b>\n\n"
        "После успешной оплаты бот автоматически пришлёт персональную ссылку для входа.",
        parse_mode="HTML",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "buy")
async def buy(callback: CallbackQuery):
    await callback.answer()
    try:
        payment = create_payment(callback.from_user.id)
        url = payment["confirmation"]["confirmation_url"]
        await callback.message.answer(
            f"💳 <b>{PRODUCT_NAME}</b>\n\n"
            f"Сумма: <b>{money(PRICE_RUB)} ₽</b>\n\n"
            "Нажми кнопку ниже и оплати на странице ЮKassa.\n"
            "После успешной оплаты доступ придёт автоматически.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Оплатить", url=url)]
                ]
            ),
        )
    except Exception:
        log.exception("Could not create payment")
        await callback.message.answer(
            "Не удалось создать платёж. Напиши мне, и я проверю оплату."
        )


@app.get("/")
async def health():
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}


@app.post("/yookassa/webhook")
async def yookassa_webhook(request: Request):
    # YooKassa sends a notification when a payment changes status.
    data = await request.json()

    event = data.get("event")
    obj = data.get("object", {})
    if event != "payment.succeeded":
        return {"ok": True}

    metadata = obj.get("metadata") or {}
    telegram_id = metadata.get("telegram_id")
    if not telegram_id:
        log.error("No telegram_id in payment metadata: %s", data)
        return {"ok": True}

    # Trust only a payment that YooKassa reports as paid/succeeded.
    if obj.get("status") != "succeeded" or not obj.get("paid"):
        return {"ok": True}

    await send_access(int(telegram_id))
    return {"ok": True}


@app.on_event("shutdown")
async def shutdown():
    await bot.session.close()
