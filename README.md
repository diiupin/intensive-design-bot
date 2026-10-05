# Интенсив Di Sign — Telegram bot + YooKassa

## Что делает
1. Пользователь нажимает /start.
2. Нажимает «Купить».
3. Бот создаёт отдельный платёж в YooKassa.
4. Telegram ID записывается в metadata платежа.
5. YooKassa сообщает о `payment.succeeded`.
6. Бот создаёт одноразовую invite-ссылку с `member_limit=1` и отправляет её покупателю.

## Переменные
- BOT_TOKEN — токен @BotFather.
- YOOKASSA_SHOP_ID — ID магазина из кабинета YooKassa.
- YOOKASSA_SECRET_KEY — секретный API-ключ YooKassa.
- CHANNEL_ID — ID закрытого канала (обычно вида -100...).
- PRICE_RUB — цена.
- BOT_USERNAME — username бота без @.

## Важно
Бот должен быть администратором закрытого канала с правом «Добавление подписчиков» / `can_invite_users`.

После деплоя на Render:
- Telegram webhook: `https://ВАШ-СЕРВИС.onrender.com/telegram/webhook`
- YooKassa webhook: `https://ВАШ-СЕРВИС.onrender.com/yookassa/webhook`

Для production лучше использовать постоянный платный инстанс или другой always-on хостинг; Render Free может засыпать после 15 минут без входящих запросов.
