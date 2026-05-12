# LEGENDA BOT — ПОЛНЫЙ АРХИВ
=================================

## СТРУКТУРА АРХИВА

main.py                        — Главный файл Telegram-бота (вся логика)
mini_app/index.html            — Mini App (форма заказа в браузере Telegram)
mini_app/legenda_bg.png        — Фоновое изображение магазина
mini_app/qr_payment.png        — QR-код для оплаты
scripts/start-production.sh    — Скрипт запуска Python-бота
## СЕКРЕТЫ (в переменных окружения / секретах)

- BOT_TOKEN            — токен бота от @BotFather
- TELEGRAM_BOT_TOKEN   — дублирует BOT_TOKEN
- SESSION_SECRET       — секрет сессии Express
- DATABASE_URL         — строка подключения PostgreSQL (автоматически)

## ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ

- TELEGRAM_BOT_TOKEN = токен бота от @BotFather
- DATABASE_URL = строка подключения PostgreSQL
- WEBAPP_URL = https://your-domain.com/api/shop/ (если есть веб-версия магазина)
- PORT = Railway задаёт автоматически
- WEBHOOK_PATH = /api/telegram
- WEBHOOK_URL = https://<your-railway-app>.up.railway.app/api/telegram (для webhook режима)

## КЛЮЧЕВЫЕ НАСТРОЙКИ В main.py

- ADMIN_CHAT_ID = 1353106724   (Telegram ID администратора)
- WEBHOOK_PORT  = 8443
- Ссылка реферала: https://t.me/LEGENDARYwrx_bot?start=ref_{order_id}
- Бонусный постер: каждые 10 подтверждённых рефералов

## БАЗА ДАННЫХ (PostgreSQL)

Таблица orders:
  id          SERIAL PRIMARY KEY
  user_id     TEXT
  fio         TEXT
  phone       TEXT
  status      TEXT  (в модерации / заказ принят / отклонён)
  photo_id    TEXT  (file_id чека из Telegram)
  created_at  TIMESTAMP DEFAULT NOW()
  referred_by INTEGER
  is_bonus    BOOLEAN DEFAULT FALSE

## РАЗВЁРТЫВАНИЕ НА RAILWAY

1. В проекте Railway создайте новый сервис и подключите этот репозиторий.
2. Создайте переменные окружения:
   - TELEGRAM_BOT_TOKEN
   - DATABASE_URL
   - WEBAPP_URL (по желанию)
   - WEBHOOK_URL (по желанию, если хотите webhook режим)
3. Убедитесь, что в проекте есть `requirements.txt` и `Procfile`.
4. Railway автоматически установит зависимости и запустит `web: python3 main.py`.

Если `WEBHOOK_URL` задан, бот запустится в webhook режиме. Если нет — бот будет работать через polling.
