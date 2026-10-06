import os
import time
import uuid
import asyncio
import logging
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.error import TelegramError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
YOOKASSA_SHOP_ID = os.environ["YOOKASSA_SHOP_ID"]
YOOKASSA_SECRET_KEY = os.environ["YOOKASSA_SECRET_KEY"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
PRICE_RUB = os.environ.get("PRICE_RUB", "4414.00")
PRODUCT_NAME = os.environ.get("PRODUCT_NAME", "Интенсив Di Sign")
BASE_URL = os.environ.get("BASE_URL", "https://intensive-design-bot.onrender.com").rstrip("/")

TELEGRAM_WEBHOOK_PATH = "/telegram/webhook"
YOOKASSA_WEBHOOK_PATH = "/yookassa/webhook"

app = FastAPI()
tg_app = Application.builder().token(BOT_TOKEN).build()
bot = tg_app.bot

# Guards duplicate webhook deliveries while this instance is running.
processed_payments: set[str] = set()
processing_payments: set[str] = set()
payment_lock = asyncio.Lock()


def expected_amount() -> Decimal:
    try:
        return Decimal(PRICE_RUB).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise RuntimeError("PRICE_RUB must be a valid number, e.g. 4414.00") from exc


async def yookassa_request(method: str, path: str, **kwargs) -> dict:
    auth = (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(
            method,
            f"https://api.yookassa.ru/v3{path}",
            auth=auth,
            **kwargs,
        )
    if response.status_code >= 400:
        logger.error("YooKassa API error %s: %s", response.status_code, response.text)
        raise RuntimeError(f"YooKassa API returned HTTP {response.status_code}")
    return response.json()


async def create_yookassa_payment(user_id: int) -> str:
    amount = expected_amount()
    payload = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": f"{BASE_URL}/payment/return",
        },
        "description": f"{PRODUCT_NAME} — доступ к закрытому каналу",
        "metadata": {
            "telegram_user_id": str(user_id),
            "product": PRODUCT_NAME,
        },
    }
    data = await yookassa_request(
        "POST",
        "/payments",
        headers={
            "Idempotence-Key": str(uuid.uuid4()),
            "Content-Type": "application/json",
        },
        json=payload,
    )
    confirmation_url = (data.get("confirmation") or {}).get("confirmation_url")
    if not confirmation_url or not data.get("id"):
        logger.error("Unexpected YooKassa response: %s", data)
        raise RuntimeError("YooKassa did not return a payment URL")
    return confirmation_url


async def get_yookassa_payment(payment_id: str) -> dict:
    return await yookassa_request("GET", f"/payments/{payment_id}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    await update.message.reply_text(
        f"🎓 {PRODUCT_NAME}\n\n"
        "Единоразовый доступ к закрытому каналу.\n"
        f"Стоимость: {PRICE_RUB} ₽\n\n"
        "После успешной оплаты бот автоматически пришлёт "
        "персональную одноразовую ссылку для входа.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💳 Купить доступ — {PRICE_RUB} ₽", callback_data="buy")]
        ]),
    )


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if query.data != "buy" or not query.from_user:
        return

    try:
        payment_url = await create_yookassa_payment(query.from_user.id)
    except Exception:
        logger.exception("Failed to create YooKassa payment")
        await query.message.reply_text("Не удалось создать платёж. Попробуй ещё раз через минуту.")
        return

    await query.message.reply_text(
        "Платёж создан. Нажми кнопку ниже и оплати.\n\n"
        "После успешной оплаты вернись в Telegram — бот автоматически "
        "пришлёт персональную ссылку на закрытый канал.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Перейти к оплате", url=payment_url)]
        ]),
    )


async def process_successful_payment(event_payment: dict):
    payment_id = event_payment.get("id")
    if not payment_id:
        raise RuntimeError("Webhook payment has no id")

    async with payment_lock:
        if payment_id in processed_payments or payment_id in processing_payments:
            return
        processing_payments.add(payment_id)

    try:
        # Never trust the webhook body alone. Re-read the payment from YooKassa
        # using the merchant credentials and verify the amount/metadata.
        payment = await get_yookassa_payment(payment_id)

        if payment.get("status") != "succeeded" or payment.get("paid") is not True:
            logger.info("Payment %s is not successfully paid yet", payment_id)
            return

        amount = (payment.get("amount") or {}).get("value")
        currency = (payment.get("amount") or {}).get("currency")
        if currency != "RUB" or Decimal(str(amount)) != expected_amount():
            raise RuntimeError(f"Unexpected payment amount/currency for {payment_id}: {amount} {currency}")

        metadata = payment.get("metadata") or {}
        user_id_raw = metadata.get("telegram_user_id")
        if not user_id_raw:
            raise RuntimeError(f"Payment {payment_id} has no telegram_user_id metadata")
        user_id = int(user_id_raw)

        # If the buyer already joined, do not create another invite.
        try:
            member = await bot.get_chat_member(CHANNEL_ID, user_id)
            if member.status in {"member", "administrator", "creator"}:
                await bot.send_message(user_id, "✅ Оплата прошла. Ты уже состоишь в закрытом канале.")
                processed_payments.add(payment_id)
                return
        except TelegramError:
            pass

        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            name=f"payment-{payment_id[:12]}",
            expire_date=int(time.time()) + 24 * 60 * 60,
            member_limit=1,
            creates_join_request=False,
        )

        await bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 Оплата прошла успешно!\n\n"
                "Твоя персональная ссылка для входа в закрытый канал:\n\n"
                f"👉 {invite.invite_link}\n\n"
                "Ссылка одноразовая: по ней сможет войти только один человек. "
"Она действует 24 часа. Не передавай её другим.\n\n"
"Если возникнут проблемы с оплатой или входом — напиши Диане: @diuuupin 💌"
            ),
            disable_web_page_preview=True,
        )
        processed_payments.add(payment_id)
        logger.info("Invite sent to Telegram user %s for payment %s", user_id, payment_id)
    finally:
        processing_payments.discard(payment_id)


tg_app.add_handler(CommandHandler("start", start_command))
tg_app.add_handler(CallbackQueryHandler(buy_callback))


@app.on_event("startup")
async def startup():
    await tg_app.initialize()
    await tg_app.start()
    webhook_url = f"{BASE_URL}{TELEGRAM_WEBHOOK_PATH}"
    await bot.set_webhook(url=webhook_url, allowed_updates=["message", "callback_query"])
    logger.info("Telegram webhook set to %s", webhook_url)


@app.on_event("shutdown")
async def shutdown():
    await tg_app.stop()
    await tg_app.shutdown()


@app.post(TELEGRAM_WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    update = Update.de_json(await request.json(), bot)
    await tg_app.process_update(update)
    return JSONResponse({"ok": True})


@app.post(YOOKASSA_WEBHOOK_PATH)
async def yookassa_webhook(request: Request):
    data = await request.json()
    if data.get("event") == "payment.succeeded":
        await process_successful_payment(data.get("object") or {})
    return JSONResponse({"ok": True})


@app.get("/payment/return", response_class=HTMLResponse)
async def payment_return():
    return HTMLResponse(
        """<!doctype html><html lang='ru'><meta name='viewport' content='width=device-width,initial-scale=1'>
        <title>Оплата</title><body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;padding:40px 24px;text-align:center'>
        <h2>Оплата завершена</h2><p>Вернись в Telegram — бот автоматически пришлёт персональную ссылку для входа.</p>
        </body></html>"""
    )


@app.get("/")
async def root():
    return {"ok": True, "service": "intensive-design-bot"}
