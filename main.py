import os, json, logging, csv, io
import psycopg2
import psycopg2.extras

from flask import Flask
from threading import Thread
from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes


TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = 1353106724
BOT_USERNAME = "LEGENDARYwrx_bot"

WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/api/telegram")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
print(f"[BOT] WEBAPP_URL={WEBAPP_URL} PORT={PORT} WEBHOOK_URL={WEBHOOK_URL}")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- БАЗА ДАННЫХ ----------

def get_db():
    return psycopg2.connect(os.environ['DATABASE_URL'])
def db_create_order(user_id, fio, phone):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO orders (user_id, fio, phone, status) VALUES (%s, %s, %s, %s) RETURNING id",
                (user_id, fio, phone, 'в модерации')
            )
            order_id = cur.fetchone()[0]
            conn.commit()
            return order_id
    finally:
        conn.close()

def db_get_order_by_user(user_id):
    """Возвращает заявку пользователя строго по его Telegram user_id.
    Сначала ищет незакрытую заявку без чека, затем любую последнюю для отображения статуса.
    НЕ использует fallback на заявки с user_id=0 — это приводило к прикреплению
    чеков чужих пользователей к чужим заказам."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 1. Заявка этого пользователя без чека (ждёт оплаты)
            cur.execute(
                "SELECT * FROM orders WHERE user_id = %s AND photo_id IS NULL AND status = 'в модерации' ORDER BY created_at DESC LIMIT 1",
                (user_id,)
            )
            row = cur.fetchone()
            if row:
                return row
            # 2. Любая последняя заявка этого пользователя (для отображения статуса)
            cur.execute(
                "SELECT * FROM orders WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
                (user_id,)
            )
            return cur.fetchone()
    finally:
        conn.close()

def db_get_order_by_id(order_id):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
            return cur.fetchone()
    finally:
        conn.close()

def db_count_confirmed_referrals(referrer_order_id):
    """Считает подтверждённые заказы, привлечённые данным заказом."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM orders WHERE referred_by = %s AND status = 'заказ принят' AND is_bonus = FALSE",
                (referrer_order_id,)
            )
            return cur.fetchone()[0]
    finally:
        conn.close()

def db_create_bonus_order(fio, phone, referred_by_order_id):
    """Создаёт бонусный заказ (бесплатный постер) для пригласившего."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO orders (user_id, fio, phone, status, is_bonus) VALUES (0, %s, %s, 'заказ принят', TRUE) RETURNING id",
                (f"🎁 БОНУС: {fio}", phone)
            )
            bonus_id = cur.fetchone()[0]
            conn.commit()
            return bonus_id
    finally:
        conn.close()

def db_reset_order_photo(order_id):
    """Сбрасывает неверно прикреплённый чек и возвращает заказ в статус 'в модерации'."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET photo_id = NULL, status = 'в модерации' WHERE id = %s",
                (order_id,)
            )
            conn.commit()
    finally:
        conn.close()

def db_update_order(order_id, **fields):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            set_clause = ", ".join(f"{k} = %s" for k in fields)
            values = list(fields.values()) + [order_id]
            cur.execute(f"UPDATE orders SET {set_clause} WHERE id = %s", values)
            conn.commit()
    finally:
        conn.close()

def db_get_stats():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'в модерации')  AS pending,
                    COUNT(*) FILTER (WHERE status = 'заказ принят') AS accepted,
                    COUNT(*) FILTER (WHERE status = 'отклонён')     AS rejected,
                    COUNT(*) FILTER (WHERE photo_id IS NULL)        AS no_photo,
                    DATE(MIN(created_at)) AS first_order_date,
                    DATE(MAX(created_at)) AS last_order_date
                FROM orders
            """)
            return cur.fetchone()
    finally:
        conn.close()

def db_get_order_by_phone(phone, sender_user_id=None):
    """Находит заявку без чека по номеру телефона.
    Ищет только анонимные заявки (user_id=0) или заявки самого отправителя.
    Это предотвращает прикрепление чека к чужому заказу."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Нормализуем телефон: убираем всё кроме цифр для сравнения
            phone_digits = ''.join(c for c in phone if c.isdigit())
            if sender_user_id and sender_user_id != 0:
                # Ищем только анонимный заказ или заказ самого пользователя
                cur.execute(
                    """SELECT * FROM orders
                       WHERE (user_id = '0' OR user_id = %s)
                         AND photo_id IS NULL
                         AND status = 'в модерации'
                         AND regexp_replace(phone, '[^0-9]', '', 'g') LIKE %s
                       ORDER BY created_at DESC LIMIT 1""",
                    (str(sender_user_id), f'%{phone_digits[-10:]}%')
                )
            else:
                cur.execute(
                    """SELECT * FROM orders
                       WHERE user_id = '0'
                         AND photo_id IS NULL
                         AND status = 'в модерации'
                         AND regexp_replace(phone, '[^0-9]', '', 'g') LIKE %s
                       ORDER BY created_at DESC LIMIT 1""",
                    (f'%{phone_digits[-10:]}%',)
                )
            return cur.fetchone()
    finally:
        conn.close()

def db_search_orders(query):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            like = f"%{query}%"
            cur.execute("""
                SELECT * FROM orders
                WHERE fio ILIKE %s OR phone ILIKE %s
                ORDER BY created_at DESC
            """, (like, like))
            return cur.fetchall()
    finally:
        conn.close()

def db_get_all_orders(status_filter=None):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if status_filter:
                cur.execute(
                    "SELECT * FROM orders WHERE status = %s ORDER BY created_at DESC",
                    (status_filter,)
                )
            else:
                cur.execute("SELECT * FROM orders ORDER BY created_at DESC")
            return cur.fetchall()
    finally:
        conn.close()

# ---------- ГЕНЕРАЦИЯ БИЛЕТА ----------

BG_PATH = os.path.join(os.path.dirname(__file__), 'public', 'legenda_bg.png')
FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'

def generate_ticket(order_id: int, fio: str) -> io.BytesIO:
    img = Image.open(BG_PATH).convert('RGBA')
    W, H = img.size

    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Тёмный баннер внизу
    banner_h = int(H * 0.38)
    draw.rectangle([(0, H - banner_h), (W, H)], fill=(0, 0, 0, 175))

    # Неоновая рамка
    lw = 4
    draw.rectangle([(lw, H - banner_h + lw), (W - lw, H - lw)],
                   outline=(0, 255, 255, 220), width=lw)

    img = Image.alpha_composite(img, overlay).convert('RGB')
    draw = ImageDraw.Draw(img)

    # "УЧАСТНИК №"
    try:
        fnt_label = ImageFont.truetype(FONT_BOLD, size=int(H * 0.07))
        fnt_number = ImageFont.truetype(FONT_BOLD, size=int(H * 0.20))
        fnt_fio = ImageFont.truetype(FONT_BOLD, size=int(H * 0.055))
    except Exception:
        fnt_label = fnt_number = fnt_fio = ImageFont.load_default()

    label = "УЧАСТНИК №"
    number = str(order_id)
    # Обрезаем ФИО до 30 символов
    fio_short = fio[:30] if len(fio) > 30 else fio

    def center_x(text, font):
        bb = draw.textbbox((0, 0), text, font=font)
        return (W - (bb[2] - bb[0])) // 2

    y_label = H - banner_h + int(H * 0.025)
    y_num   = y_label + int(H * 0.07)
    y_fio   = y_num + int(H * 0.21)

    # Тень
    shadow = (0, 80, 80)
    draw.text((center_x(label, fnt_label) + 2, y_label + 2), label, font=fnt_label, fill=shadow)
    draw.text((center_x(number, fnt_number) + 3, y_num + 3), number, font=fnt_number, fill=shadow)
    draw.text((center_x(fio_short, fnt_fio) + 2, y_fio + 2), fio_short, font=fnt_fio, fill=shadow)

    # Основной текст
    draw.text((center_x(label, fnt_label), y_label), label, font=fnt_label, fill=(180, 255, 255))
    draw.text((center_x(number, fnt_number), y_num), number, font=fnt_number, fill=(0, 255, 255))
    draw.text((center_x(fio_short, fnt_fio), y_fio), fio_short, font=fnt_fio, fill=(220, 255, 255))

    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=92)
    buf.seek(0)
    return buf

# ---------- ОБРАБОТЧИКИ БОТА ----------

STATUS_EMOJI = {
    'в модерации': '⏳',
    'заказ принят': '✅',
    'отклонён':     '❌',
}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    ref_order_id = None
    if args and args[0].startswith('ref_'):
        try:
            ref_order_id = int(args[0].split('_')[1])
        except (IndexError, ValueError):
            pass

    webapp_url = WEBAPP_URL
    if ref_order_id:
        webapp_url = f"{WEBAPP_URL}?ref={ref_order_id}"

    if webapp_url:
        inline_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛍 Открыть LEGENDARY SHOP", web_app=WebAppInfo(url=webapp_url))
        ]])
        reply_kb = ReplyKeyboardMarkup(
            [[KeyboardButton("🛍 LEGENDARY SHOP", web_app=WebAppInfo(url=webapp_url))]],
            resize_keyboard=True,
            one_time_keyboard=False,
            input_field_placeholder="Нажми кнопку магазина ниже"
        )
        caption = (
            f"👋 <b>Тебя пригласил участник #{ref_order_id}!</b>\n\n"
            f"Оформи заказ и получи именной постер LEGENDA 🔥\n\n"
            f"Нажми кнопку ниже 👇"
            if ref_order_id else
            "🔥 <b>LEGENDA POSTER SHOP</b>\n\n"
            "Именные постеры для участников клуба LEGENDA.\n\n"
            "Нажми кнопку ниже чтобы открыть магазин 👇"
        )
        poster_url = f"{WEBAPP_URL}legenda_bg.png"
        try:
            await update.message.reply_photo(
                photo=poster_url,
                caption=caption,
                parse_mode='HTML',
                reply_markup=reply_kb
            )
        except Exception as e:
            logger.warning(f"start: не удалось отправить фото: {e}")
            await update.message.reply_text(caption, parse_mode='HTML', reply_markup=reply_kb)
    else:
        await update.message.reply_text(
            "Привет! Магазин временно недоступен. Попробуй позже."
        )
        logger.warning("WEBAPP_URL не настроен — кнопка WebApp не показана")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin = update.effective_chat.id == ADMIN_CHAT_ID
    if is_admin:
        keyboard = [
            [InlineKeyboardButton("📊 Статистика",                  callback_data="adm_stats")],
            [InlineKeyboardButton("📋 Все заказы",                  callback_data="adm_orders_all")],
            [InlineKeyboardButton("⏳ В модерации",                 callback_data="adm_orders_pending")],
            [InlineKeyboardButton("✅ Принятые заказы",             callback_data="adm_orders_accepted")],
            [InlineKeyboardButton("❌ Отклонённые заказы",          callback_data="adm_orders_rejected")],
            [InlineKeyboardButton("📤 Экспорт CSV",                 callback_data="adm_export")],
            [InlineKeyboardButton("🔍 Поиск по заказам",            callback_data="adm_find_prompt")],
            [InlineKeyboardButton("🔁 Уведомить по заказу",         callback_data="adm_renotify_prompt")],
            [InlineKeyboardButton("🎟 Отправить постер вручную",    callback_data="adm_poster_prompt")],
            [InlineKeyboardButton("📨 Разослать реф-ссылки всем",   callback_data="adm_sendreflinks")],
            [InlineKeyboardButton("🗑 Удалить неоплаченные заказы", callback_data="adm_clearunpaid")],
            [InlineKeyboardButton("🗑 Удалить отклонённые заказы",  callback_data="adm_clearrejected")],
        ]
        await update.message.reply_text(
            "🛠 <b>Панель администратора</b>\n━━━━━━━━━━━━━━━\nВыбери действие:",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    else:
        text = (
            "📋 <b>Как оформить заказ</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "1️⃣ Заполни форму через кнопку «Отправить чек»\n"
            "2️⃣ Отправь фото чека в этот чат\n"
            "3️⃣ Дождись подтверждения администратора\n\n"
            "/start — начать сначала\n"
            "/help — это сообщение"
        )
    await update.message.reply_text(text, parse_mode='HTML')

async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список всех заказов. Только для администратора."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text("У тебя нет доступа к этой команде.")
        return

    # Парсим аргументы: /orders, /orders pending, /orders accepted, /orders rejected
    args = context.args
    status_map = {
        'pending':  'в модерации',
        'accepted': 'заказ принят',
        'rejected': 'отклонён',
    }
    status_filter = status_map.get(args[0].lower()) if args else None

    all_orders = db_get_all_orders(status_filter=status_filter)

    if not all_orders:
        label = f'со статусом «{status_filter}»' if status_filter else ''
        await update.message.reply_text(f"Заказов {label} пока нет.")
        return

    lines = []
    for o in all_orders:
        emoji = STATUS_EMOJI.get(o['status'], '❓')
        date = o['created_at'].strftime('%d.%m.%Y %H:%M') if o['created_at'] else '—'
        lines.append(
            f"{emoji} <b>Заказ #{o['id']}</b> — {date}\n"
            f"   👤 {o['fio']}  📞 {o['phone']}\n"
            f"   Статус: {o['status']}"
        )

    header = f"📋 <b>Все заказы</b> ({len(all_orders)} шт.)"
    if status_filter:
        header = f"📋 <b>Заказы: {status_filter}</b> ({len(all_orders)} шт.)"

    # Разбиваем на части по 10 заказов чтобы не превысить лимит Telegram
    chunk_size = 10
    for i in range(0, len(lines), chunk_size):
        chunk = lines[i:i + chunk_size]
        text = (header if i == 0 else "📋 <b>...продолжение</b>") + "\n\n" + "\n\n".join(chunk)
        await update.message.reply_text(text, parse_mode='HTML')

    keyboard = [[
        InlineKeyboardButton("⏳ В модерации", callback_data="filter_pending"),
        InlineKeyboardButton("✅ Принятые",    callback_data="filter_accepted"),
        InlineKeyboardButton("❌ Отклонённые", callback_data="filter_rejected"),
    ]]
    await update.message.reply_text(
        "Фильтр по статусу:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def order_detail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает детали конкретного заказа. Только для администратора."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text("У тебя нет доступа к этой команде.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Укажи номер заказа. Пример: /order 5")
        return

    order_id = int(context.args[0])
    order = db_get_order_by_id(order_id)

    if not order:
        await update.message.reply_text(f"Заказ #{order_id} не найден.")
        return

    emoji = STATUS_EMOJI.get(order['status'], '❓')
    date = order['created_at'].strftime('%d.%m.%Y %H:%M') if order['created_at'] else '—'

    text = (
        f"📦 <b>Заказ #{order['id']}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 ФИО: {order['fio']}\n"
        f"📞 Телефон: {order['phone']}\n"
        f"🕐 Дата: {date}\n"
        f"📌 Статус: {emoji} {order['status']}\n"
        f"🖼 Чек: {'прикреплён' if order['photo_id'] else 'не прикреплён'}"
    )

    # Если чек есть — отправляем фото, иначе просто текст
    if order['photo_id']:
        keyboard = []
        if order['status'] == 'в модерации':
            keyboard = [[
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_{order['id']}"),
                InlineKeyboardButton("❌ Отклонить",   callback_data=f"reject_{order['id']}")
            ]]
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=order['photo_id'],
            caption=text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
        )
    else:
        await update.message.reply_text(text, parse_mode='HTML')

async def resetorders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полная очистка всех заказов. Только для администратора."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text("У тебя нет доступа к этой команде.")
        return
    conn = get_db()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE orders RESTART IDENTITY CASCADE")
        await update.message.reply_text("✅ Все заказы удалены. Счётчик сброшен до #1.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
    finally:
        conn.close()

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику заказов. Только для администратора."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text("У тебя нет доступа к этой команде.")
        return

    s = db_get_stats()
    total = int(s['total'])

    if total == 0:
        await update.message.reply_text("Заказов пока нет.")
        return

    accepted  = int(s['accepted'])
    rejected  = int(s['rejected'])
    pending   = int(s['pending'])
    no_photo  = int(s['no_photo'])

    accept_pct  = round(accepted / total * 100) if total else 0
    reject_pct  = round(rejected / total * 100) if total else 0
    pending_pct = round(pending  / total * 100) if total else 0

    # Мини-гистограмма
    def bar(pct, length=10):
        filled = round(pct / 100 * length)
        return '█' * filled + '░' * (length - filled)

    first = s['first_order_date'].strftime('%d.%m.%Y') if s['first_order_date'] else '—'
    last  = s['last_order_date'].strftime('%d.%m.%Y')  if s['last_order_date']  else '—'

    text = (
        f"📊 <b>Статистика заказов</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📦 Всего заказов: <b>{total}</b>\n"
        f"📅 Первый: {first}  |  Последний: {last}\n\n"
        f"✅ Принято:    <b>{accepted}</b> ({accept_pct}%)\n"
        f"   {bar(accept_pct)}\n\n"
        f"⏳ В модерации: <b>{pending}</b> ({pending_pct}%)\n"
        f"   {bar(pending_pct)}\n\n"
        f"❌ Отклонено:  <b>{rejected}</b> ({reject_pct}%)\n"
        f"   {bar(reject_pct)}\n\n"
        f"🖼 Без чека:   <b>{no_photo}</b>"
    )

    keyboard = [[
        InlineKeyboardButton("⏳ В модерации", callback_data="filter_pending"),
        InlineKeyboardButton("✅ Принятые",    callback_data="filter_accepted"),
        InlineKeyboardButton("❌ Отклонённые", callback_data="filter_rejected"),
    ]]
    await update.message.reply_text(
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экспортирует все заказы в CSV. Только для администратора."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text("У тебя нет доступа к этой команде.")
        return

    await update.message.reply_text("⏳ Формирую файл...")

    # Опциональный фильтр: /export pending | accepted | rejected
    args = context.args
    status_map = {
        'pending':  'в модерации',
        'accepted': 'заказ принят',
        'rejected': 'отклонён',
    }
    status_filter = status_map.get(args[0].lower()) if args else None
    all_orders = db_get_all_orders(status_filter=status_filter)

    if not all_orders:
        label = f' со статусом «{status_filter}»' if status_filter else ''
        await update.message.reply_text(f"Нет заказов{label} для экспорта.")
        return

    # Создаём CSV в памяти (без записи на диск)
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')

    # Заголовок
    writer.writerow(['№ заказа', 'ФИО', 'Телефон', 'Статус', 'Чек', 'Дата создания'])

    for o in all_orders:
        date_str = o['created_at'].strftime('%d.%m.%Y %H:%M') if o['created_at'] else '—'
        writer.writerow([
            o['id'],
            o['fio'],
            o['phone'],
            o['status'],
            'прикреплён' if o['photo_id'] else 'нет',
            date_str,
        ])

    # Кодируем в bytes с BOM для корректного открытия в Excel
    csv_bytes = output.getvalue().encode('utf-8-sig')
    output.close()

    from datetime import datetime
    filename = f"orders_{datetime.now().strftime('%Y%m%d_%H%M')}"
    if status_filter:
        filename += f"_{args[0]}"
    filename += ".csv"

    label = f' ({status_filter})' if status_filter else ''
    caption = (
        f"📂 <b>Экспорт заказов{label}</b>\n"
        f"Всего записей: {len(all_orders)}\n"
        f"Формат: CSV (разделитель «;», кодировка UTF-8)\n"
        f"Открывай в Excel или Google Sheets."
    )

    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=io.BytesIO(csv_bytes),
        filename=filename,
        caption=caption,
        parse_mode='HTML'
    )

async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ищет заказы по ФИО или телефону. Только для администратора."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text("У тебя нет доступа к этой команде.")
        return

    if not context.args:
        await update.message.reply_text(
            "Укажи текст для поиска.\n"
            "Примеры:\n"
            "/find Иванов\n"
            "/find +79991234567\n"
            "/find 9991"
        )
        return

    query = " ".join(context.args)
    results = db_search_orders(query)

    if not results:
        await update.message.reply_text(f"По запросу «{query}» ничего не найдено.")
        return

    lines = []
    for o in results:
        emoji = STATUS_EMOJI.get(o['status'], '❓')
        date = o['created_at'].strftime('%d.%m.%Y %H:%M') if o['created_at'] else '—'
        lines.append(
            f"{emoji} <b>Заказ #{o['id']}</b> — {date}\n"
            f"   👤 {o['fio']}  📞 {o['phone']}\n"
            f"   Статус: {o['status']}"
        )

    header = f"🔍 <b>Результаты поиска «{query}»</b> ({len(results)} шт.)\n\n"

    # Показываем детальную кнопку для каждого результата
    keyboard = [
        [InlineKeyboardButton(f"🔍 Заказ #{o['id']} — {o['fio']}", callback_data=f"detail_{o['id']}")]
        for o in results
    ]

    await update.message.reply_text(
        header + "\n\n".join(lines),
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.web_app_data:
        return
    data = update.message.web_app_data.data
    try:
        payload = json.loads(data)
        if payload.get('action') == 'new_order':
            user_id = update.effective_chat.id
            fio = payload['fio']
            phone = payload['phone']
            order_id = db_create_order(user_id, fio, phone)

            # Уведомляем пользователя с номером заказа и кнопкой статуса
            await update.message.reply_text(
                f"🎉 <b>Заявка #{order_id} оформлена!</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"👤 ФИО: {fio}\n"
                f"📞 Телефон: {phone}\n"
                f"📌 Статус: ⏳ Ожидает чек\n\n"
                f"📸 <b>Последний шаг — пришли сюда скриншот оплаты!</b>\n"
                f"Просто сделай скриншот подтверждения из банковского приложения и отправь его прямо в этот чат.\n\n"
                f"Без чека заявка не будет рассмотрена.",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 Статус заказа", callback_data=f"mystatus_{order_id}")
                ]])
            )

            # Уведомляем администратора сразу при создании заявки
            username = update.effective_user.username
            user_link = f"@{username}" if username else f"ID: {user_id}"
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"🔔 <b>Новая заявка #{order_id}</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"👤 ФИО: {fio}\n"
                    f"📞 Телефон: {phone}\n"
                    f"💬 Пользователь: {user_link}\n"
                    f"📌 Статус: ⏳ ожидает чек\n\n"
                    f"Чек ещё не прикреплён. Ожидай фото."
                ),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"🔍 Детали заказа #{order_id}", callback_data=f"detail_{order_id}")
                ]])
            )
            logger.info(f"WebApp order created: {order_id} fio={fio} phone={phone}")
    except Exception as e:
        logger.error(f"Ошибка обработки webapp данных: {e}")
        await update.message.reply_text("Ошибка обработки данных.")

async def _send_receipt_to_admin(context, update, order, file_id, is_pdf=False):
    """Прикрепляет чек к заявке и уведомляет администратора."""
    user_id = update.effective_chat.id
    db_update_order(order['id'], photo_id=file_id, user_id=user_id)
    username = update.effective_user.username
    user_link = f"@{username}" if username else f"ID: {user_id}"
    icon = "📄" if is_pdf else "📸"
    kind = "PDF-чек" if is_pdf else "Чек"
    text = (
        f"{icon} <b>{kind} к заказу #{order['id']}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 Покупатель: {order['fio']}\n"
        f"📞 Телефон: {order['phone']}\n"
        f"💬 Пользователь: {user_link}\n"
        f"📌 Статус: ⏳ В модерации"
    )
    keyboard = [[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_{order['id']}"),
        InlineKeyboardButton("❌ Отклонить",   callback_data=f"reject_{order['id']}")
    ]]
    if is_pdf:
        await context.bot.send_document(chat_id=ADMIN_CHAT_ID, document=file_id,
                                        caption=text, parse_mode='HTML',
                                        reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await context.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=file_id,
                                     caption=text, parse_mode='HTML',
                                     reply_markup=InlineKeyboardMarkup(keyboard))
    await update.message.reply_text(
        f"✅ <b>Чек по заказу #{order['id']} получен!</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Администратор проверит оплату и подтвердит заказ.\n"
        f"Ты получишь уведомление с результатом.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 Статус заказа", callback_data=f"mystatus_{order['id']}")
        ]])
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    order = db_get_order_by_user(user_id)

    if not order:
        # Заявка не найдена по Telegram ID (Android-пользователи или первый визит)
        # Сохраняем чек и ищем заявку по номеру телефона
        photo_file = update.message.photo[-1]
        context.user_data['pending_receipt'] = {'file_id': photo_file.file_id, 'is_pdf': False}
        await update.message.reply_text(
            "📞 <b>Введи номер телефона из заявки</b>\n\n"
            "Укажи тот же номер, что вводил в форме заказа — так мы найдём твою заявку и прикрепим чек.\n\n"
            "<i>Пример: +79241234567 или 89241234567</i>",
            parse_mode='HTML'
        )
        return

    # Если заказ подтверждён — повторный чек не нужен
    if order['photo_id'] and order['status'] == 'заказ принят':
        await update.message.reply_text(
            f"✅ Заявка <b>#{order['id']}</b> уже подтверждена!\n"
            f"Статус: {STATUS_EMOJI.get(order['status'], '❓')} {order['status']}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Статус заказа", callback_data=f"mystatus_{order['id']}")
            ]])
        )
        return

    # Если заявка в модерации с чеком — не дублируем
    if order['photo_id'] and order['status'] == 'в модерации':
        await update.message.reply_text(
            f"⏳ По заявке <b>#{order['id']}</b> чек уже отправлен — ожидай решения администратора.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Статус заказа", callback_data=f"mystatus_{order['id']}")
            ]])
        )
        return

    # Если заявка была отклонена — разрешаем повторно отправить чек
    photo_file = update.message.photo[-1]
    await _send_receipt_to_admin(context, update, order, photo_file.file_id, is_pdf=False)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает PDF-чек напрямую из банковского приложения."""
    user_id = update.effective_chat.id
    doc = update.message.document

    # Принимаем только PDF
    if not doc or doc.mime_type != 'application/pdf':
        return

    order = db_get_order_by_user(user_id)

    if not order:
        # Заявка не найдена по Telegram ID (Android-пользователи или первый визит)
        context.user_data['pending_receipt'] = {'file_id': doc.file_id, 'is_pdf': True}
        await update.message.reply_text(
            "📞 <b>Введи номер телефона из заявки</b>\n\n"
            "Укажи тот же номер, что вводил в форме заказа — так мы найдём твою заявку и прикрепим чек.\n\n"
            "<i>Пример: +79241234567 или 89241234567</i>",
            parse_mode='HTML'
        )
        return

    if order['photo_id'] and order['status'] == 'заказ принят':
        await update.message.reply_text(
            f"✅ Заявка <b>#{order['id']}</b> уже подтверждена!\n"
            f"Статус: {STATUS_EMOJI.get(order['status'], '❓')} {order['status']}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Статус заказа", callback_data=f"mystatus_{order['id']}")
            ]])
        )
        return

    if order['photo_id'] and order['status'] == 'в модерации':
        await update.message.reply_text(
            f"⏳ По заявке <b>#{order['id']}</b> чек уже отправлен — ожидай решения администратора.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Статус заказа", callback_data=f"mystatus_{order['id']}")
            ]])
        )
        return

    # Если заявка была отклонена — разрешаем повторно отправить чек
    await _send_receipt_to_admin(context, update, order, doc.file_id, is_pdf=True)


async def handle_phone_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ищет заявку по номеру телефона когда pending_receipt ждёт привязки."""
    pending = context.user_data.get('pending_receipt')
    if not pending:
        return  # Не наш случай — другие хендлеры обработают

    sender_user_id = update.effective_chat.id
    phone = update.message.text.strip()
    order = db_get_order_by_phone(phone, sender_user_id=sender_user_id)

    if not order:
        await update.message.reply_text(
            f"❌ Заявка с номером <b>{phone}</b> не найдена или уже принадлежит другому пользователю.\n\n"
            "Проверь номер и попробуй снова — введи телефон ещё раз.",
            parse_mode='HTML'
        )
        return

    # Нашли заявку — прописываем Telegram ID чтобы следующий раз нашлось сразу
    if not order.get('user_id') or str(order['user_id']) == '0':
        db_update_order(order['id'], user_id=sender_user_id)

    context.user_data.pop('pending_receipt', None)
    await _send_receipt_to_admin(context, update, order, pending['file_id'], is_pdf=pending['is_pdf'])

async def callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # ── Кнопки панели администратора (/help) ──────────────────────────────
    if query.data.startswith('adm_'):
        if query.from_user.id != ADMIN_CHAT_ID:
            await query.message.reply_text("Нет доступа.")
            return

        action = query.data

        if action == 'adm_stats':
            s = db_get_stats()
            text = (
                "📊 <b>Статистика заказов</b>\n"
                "━━━━━━━━━━━━━━━\n"
                f"📦 Всего заказов: <b>{s['total']}</b>\n"
                f"⏳ В модерации: <b>{s['pending']}</b>\n"
                f"✅ Принято: <b>{s['accepted']}</b>\n"
                f"❌ Отклонено: <b>{s['rejected']}</b>\n"
                f"📷 Без чека: <b>{s['no_photo']}</b>\n"
                f"🗓 Первый заказ: {s['first_order_date'] or '—'}\n"
                f"🗓 Последний заказ: {s['last_order_date'] or '—'}"
            )
            await query.message.reply_text(text, parse_mode='HTML')
            return

        if action in ('adm_orders_all', 'adm_orders_pending', 'adm_orders_accepted', 'adm_orders_rejected'):
            status_map = {
                'adm_orders_all':      None,
                'adm_orders_pending':  'в модерации',
                'adm_orders_accepted': 'заказ принят',
                'adm_orders_rejected': 'отклонён',
            }
            status_filter = status_map[action]
            all_orders = db_get_all_orders(status_filter=status_filter)
            if not all_orders:
                label = f'со статусом «{status_filter}»' if status_filter else ''
                await query.message.reply_text(f"Заказов {label} пока нет.")
                return
            lines = []
            for o in all_orders:
                emoji = STATUS_EMOJI.get(o['status'], '❓')
                date = o['created_at'].strftime('%d.%m.%Y %H:%M') if o['created_at'] else '—'
                lines.append(
                    f"{emoji} <b>Заказ #{o['id']}</b> — {date}\n"
                    f"   👤 {o['fio']}  📞 {o['phone']}\n"
                    f"   Статус: {o['status']}"
                )
            label_map = {
                'adm_orders_all':      'Все заказы',
                'adm_orders_pending':  '⏳ В модерации',
                'adm_orders_accepted': '✅ Принятые',
                'adm_orders_rejected': '❌ Отклонённые',
            }
            header = f"📋 <b>{label_map[action]}</b> ({len(all_orders)} шт.)"
            # Разбиваем на части если много заказов
            chunk, chunks = [], []
            for line in lines:
                if sum(len(l) for l in chunk) + len(line) > 3500:
                    chunks.append(chunk)
                    chunk = []
                chunk.append(line)
            if chunk:
                chunks.append(chunk)
            await query.message.reply_text(header, parse_mode='HTML')
            for ch in chunks:
                await query.message.reply_text("\n\n".join(ch), parse_mode='HTML')
            return

        if action == 'adm_export':
            all_orders = db_get_all_orders()
            if not all_orders:
                await query.message.reply_text("Нет заказов для экспорта.")
                return
            output = io.StringIO()
            writer = csv.writer(output, delimiter=';')
            writer.writerow(['№ заказа', 'ФИО', 'Телефон', 'Статус', 'Чек', 'Дата создания'])
            for o in all_orders:
                date_str = o['created_at'].strftime('%d.%m.%Y %H:%M') if o['created_at'] else '—'
                writer.writerow([o['id'], o['fio'], o['phone'], o['status'],
                                 'прикреплён' if o['photo_id'] else 'нет', date_str])
            csv_bytes = output.getvalue().encode('utf-8-sig')
            output.close()
            from datetime import datetime as _dt
            filename = f"orders_{_dt.now().strftime('%Y%m%d_%H%M')}.csv"
            await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=io.BytesIO(csv_bytes),
                filename=filename,
                caption=f"📂 <b>Экспорт заказов</b> — {len(all_orders)} записей",
                parse_mode='HTML'
            )
            return

        if action == 'adm_find_prompt':
            await query.message.reply_text(
                "🔍 <b>Поиск заказов</b>\n\nВведи команду:\n"
                "<code>/find Иванов</code> — по ФИО\n"
                "<code>/find 79991234567</code> — по телефону",
                parse_mode='HTML'
            )
            return

        if action == 'adm_renotify_prompt':
            await query.message.reply_text(
                "🔁 <b>Повторное уведомление</b>\n\nВведи команду:\n"
                "<code>/renotify13</code> — отправить уведомление по заказу #13",
                parse_mode='HTML'
            )
            return

        if action == 'adm_poster_prompt':
            await query.message.reply_text(
                "🎟 <b>Отправить постер</b>\n\nВведи команду:\n"
                "<code>/sendposter13</code> — сгенерировать постер заказа #13",
                parse_mode='HTML'
            )
            return

        if action == 'adm_sendreflinks':
            all_orders = db_get_all_orders(status_filter='заказ принят')
            eligible = [o for o in all_orders if o.get('user_id') and int(o['user_id']) > 0 and not o.get('is_bonus')]
            if not eligible:
                await query.message.reply_text("Нет подтверждённых участников с известным Telegram ID.")
                return
            await query.message.reply_text(
                f"⏳ Начинаю рассылку реферальных ссылок {len(eligible)} участникам..."
            )
            import asyncio as _asyncio
            sent, failed = 0, 0
            for o in eligible:
                ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{o['id']}"
                try:
                    await context.bot.send_message(
                        chat_id=int(o['user_id']),
                        text=(
                            f"🔗 <b>Твоя реферальная ссылка:</b>\n"
                            f"━━━━━━━━━━━━━━━\n"
                            f"{ref_link}\n\n"
                            f"Приглашай друзей! За каждые <b>10 оплативших</b> друзей ты получаешь "
                            f"<b>бесплатный постер 🎁</b>\n\n"
                            f"Прогресс виден прямо в магазине — нажми кнопку ЛЕГЕНДА."
                        ),
                        parse_mode='HTML',
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                "📤 Поделиться ссылкой",
                                url=f"https://t.me/share/url?url={ref_link}&text=Оформи+постер+LEGENDA!"
                            )
                        ]])
                    )
                    sent += 1
                except Exception as e:
                    logger.warning(f"adm_sendreflinks: заказ #{o['id']} user {o['user_id']}: {e}")
                    failed += 1
                await _asyncio.sleep(0.1)
            await query.message.reply_text(
                f"✅ Рассылка завершена\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📨 Отправлено: <b>{sent}</b>\n"
                f"⚠️ Не удалось: <b>{failed}</b> (бот заблокирован или user_id=0)",
                parse_mode='HTML'
            )
            return

        if action == 'adm_clearunpaid':
            conn = get_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM orders WHERE photo_id IS NULL AND status = 'в модерации'"
                    )
                    count = cur.fetchone()[0]
                    if count == 0:
                        await query.message.reply_text("✅ Нет неоплаченных заявок — таблица чистая.")
                        return
                    cur.execute(
                        "DELETE FROM orders WHERE photo_id IS NULL AND status = 'в модерации'"
                    )
                    conn.commit()
            finally:
                conn.close()
            await query.message.reply_text(
                f"🗑 <b>Удалено {count} неоплаченных заявок</b>\n"
                f"Удалены все заявки без чека. Подтверждённые и отклонённые — не тронуты.",
                parse_mode='HTML'
            )
            return

        if action == 'adm_clearrejected':
            conn = get_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM orders WHERE status = 'отклонён'"
                    )
                    count = cur.fetchone()[0]
                    if count == 0:
                        await query.message.reply_text("✅ Нет отклонённых заявок.")
                        return
                    cur.execute(
                        "DELETE FROM orders WHERE status = 'отклонён'"
                    )
                    conn.commit()
            finally:
                conn.close()
            await query.message.reply_text(
                f"🗑 <b>Удалено {count} отклонённых заявок</b>\n"
                f"Подтверждённые заказы — не тронуты.",
                parse_mode='HTML'
            )
            return

        return  # неизвестное adm_ действие

    # Кнопка "Статус заказа" для клиента (не администратора)
    if query.data.startswith('mystatus_'):
        order_id = int(query.data.split('_')[1])
        order = db_get_order_by_id(order_id)
        if not order:
            await query.message.reply_text(f"Заказ #{order_id} не найден.")
            return
        emoji = STATUS_EMOJI.get(order['status'], '❓')
        date = order['created_at'].strftime('%d.%m.%Y %H:%M') if order['created_at'] else '—'
        photo_status = '✅ Прикреплён' if order['photo_id'] else '❌ Не отправлен — отправь фото чека в чат'
        await query.message.reply_text(
            f"📦 <b>Ваш заказ #{order['id']}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 ФИО: {order['fio']}\n"
            f"📞 Телефон: {order['phone']}\n"
            f"🕐 Дата: {date}\n"
            f"📌 Статус: {emoji} {order['status']}\n"
            f"🖼 Чек: {photo_status}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Обновить статус", callback_data=f"mystatus_{order['id']}")
            ]])
        )
        return

    # Кнопка "Детали заказа" из уведомления о новой заявке
    if query.data.startswith('detail_'):
        order_id = int(query.data.split('_')[1])
        order = db_get_order_by_id(order_id)
        if not order:
            await query.message.reply_text(f"Заказ #{order_id} не найден.")
            return
        emoji = STATUS_EMOJI.get(order['status'], '❓')
        date = order['created_at'].strftime('%d.%m.%Y %H:%M') if order['created_at'] else '—'
        text = (
            f"📦 <b>Заказ #{order['id']}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 ФИО: {order['fio']}\n"
            f"📞 Телефон: {order['phone']}\n"
            f"🕐 Дата: {date}\n"
            f"📌 Статус: {emoji} {order['status']}\n"
            f"🖼 Чек: {'прикреплён' if order['photo_id'] else 'не прикреплён — ожидай фото'}"
        )
        if order['photo_id']:
            keyboard = []
            if order['status'] == 'в модерации':
                keyboard = [[
                    InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_{order['id']}"),
                    InlineKeyboardButton("❌ Отклонить",   callback_data=f"reject_{order['id']}")
                ]]
            # Кнопка сброса всегда доступна если есть чек (для исправления ошибочных привязок)
            keyboard.append([
                InlineKeyboardButton("🔄 Сбросить чек (неверная привязка)", callback_data=f"resetphoto_{order['id']}")
            ])
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=order['photo_id'],
                caption=text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await query.message.reply_text(text, parse_mode='HTML')
        return

    # Обработка фильтров из /orders
    if query.data.startswith('filter_'):
        filter_key = query.data.replace('filter_', '')
        status_map = {
            'pending':  'в модерации',
            'accepted': 'заказ принят',
            'rejected': 'отклонён',
        }
        status_filter = status_map.get(filter_key)
        filtered = db_get_all_orders(status_filter=status_filter)
        if not filtered:
            await query.message.reply_text(f"Заказов со статусом «{status_filter}» нет.")
            return
        lines = []
        for o in filtered:
            emoji = STATUS_EMOJI.get(o['status'], '❓')
            date = o['created_at'].strftime('%d.%m.%Y %H:%M') if o['created_at'] else '—'
            lines.append(
                f"{emoji} <b>Заказ #{o['id']}</b> — {date}\n"
                f"   👤 {o['fio']}  📞 {o['phone']}\n"
                f"   Статус: {o['status']}"
            )
        header = f"📋 <b>Заказы: {status_filter}</b> ({len(filtered)} шт.)"
        await query.message.reply_text(header + "\n\n" + "\n\n".join(lines), parse_mode='HTML')
        return

    # ── Сброс неверно прикреплённого чека (только администратор) ──────────
    if query.data.startswith('resetphoto_'):
        if query.from_user.id != ADMIN_CHAT_ID:
            await query.message.reply_text("Нет доступа.")
            return
        order_id = int(query.data.split('_')[1])
        order = db_get_order_by_id(order_id)
        if not order:
            await query.message.reply_text(f"Заказ #{order_id} не найден.")
            return
        db_reset_order_photo(order_id)
        await query.message.reply_text(
            f"🔄 <b>Чек сброшен для заказа #{order_id}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 ФИО: {order['fio']}\n"
            f"📞 Телефон: {order['phone']}\n\n"
            f"Статус возвращён в «в модерации». "
            f"Теперь владелец заявки может прислать правильный чек.",
            parse_mode='HTML'
        )
        return

    action, order_id_str = query.data.split('_')
    order_id = int(order_id_str)

    order = db_get_order_by_id(order_id)
    if not order:
        await query.edit_message_caption(caption="Заказ не найден.")
        return

    # Безопасное получение user_id — всегда int, 0 означает неизвестен
    try:
        user_id = int(order['user_id']) if order['user_id'] else 0
    except (TypeError, ValueError):
        user_id = 0

    if action == 'approve':
        db_update_order(order_id, status='заказ принят')
        new_caption = query.message.caption + "\n\n✅ Подтверждён"
        await query.edit_message_caption(caption=new_caption, reply_markup=None)

        # Если user_id неизвестен — постер отправить невозможно, предупреждаем
        if user_id == 0:
            await query.message.reply_text(
                f"⚠️ Заказ #{order_id} подтверждён, но Telegram ID покупателя неизвестен.\n"
                f"👤 {order['fio']} | 📞 {order['phone']}\n\n"
                f"Используй /sendposter {order_id} когда узнаешь его аккаунт.",
                parse_mode='HTML'
            )
            return

        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{order_id}"
        # Генерируем именной билет с номером заказа
        try:
            ticket_buf = generate_ticket(order_id, order['fio'])
            await context.bot.send_photo(
                chat_id=user_id,
                photo=ticket_buf,
                caption=(
                    f"🎉 <b>Поздравляем! Заказ #{order_id} подтверждён!</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"👤 {order['fio']}\n"
                    f"🔢 Твой номер участника: <b>#{order_id}</b>\n\n"
                    f"Сохрани этот билет — он подтверждает твоё участие!"
                ),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 Статус заказа", callback_data=f"mystatus_{order_id}")
                ]])
            )
        except Exception as e:
            logger.error(f"Ошибка генерации билета для заказа #{order_id}: {e}")
            # Fallback: текстовое уведомление клиенту
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🎉 <b>Заказ #{order_id} подтверждён!</b>\n"
                        f"👤 {order['fio']}\n"
                        f"Ожидай постер — он скоро будет готов!"
                    ),
                    parse_mode='HTML'
                )
            except Exception as e2:
                logger.error(f"Не удалось уведомить клиента заказа #{order_id}: {e2}")
                await query.message.reply_text(
                    f"⚠️ Заказ #{order_id} подтверждён, но уведомление клиенту не доставлено.\n"
                    f"User ID: {user_id} | {order['fio']}"
                )
        # Отправляем реферальную ссылку
        if user_id:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"🔗 <b>Твоя реферальная ссылка:</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"{ref_link}\n\n"
                    f"Приглашай друзей! За каждые <b>10 оплативших</b> друзей ты получаешь <b>бесплатный постер 🎁</b>\n\n"
                    f"Прогресс можно отслеживать прямо в магазине."
                ),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📤 Поделиться ссылкой", url=f"https://t.me/share/url?url={ref_link}&text=Оформи+постер+LEGENDA!")
                ]])
            )
        # Проверяем реферальную цепочку — уведомляем пригласившего
        referrer_order_id = order.get('referred_by')
        if referrer_order_id:
            referrer = db_get_order_by_id(referrer_order_id)
            if referrer and referrer['user_id']:
                count = db_count_confirmed_referrals(referrer_order_id)
                needed = 10
                if count > 0 and count % needed == 0:
                    # Достигли рубежа — выдаём бонусный постер
                    bonus_id = db_create_bonus_order(referrer['fio'], referrer['phone'], referrer_order_id)
                    try:
                        bonus_buf = generate_ticket(bonus_id, referrer['fio'])
                        await context.bot.send_photo(
                            chat_id=referrer['user_id'],
                            photo=bonus_buf,
                            caption=(
                                f"🎁 <b>БОНУСНЫЙ ПОСТЕР!</b>\n"
                                f"━━━━━━━━━━━━━━━\n"
                                f"Ты пригласил {count} оплативших друзей!\n"
                                f"Держи бесплатный постер — заслужил 🏆\n\n"
                                f"👤 {referrer['fio']}\n"
                                f"🔢 Номер бонусного заказа: <b>#{bonus_id}</b>"
                            ),
                            parse_mode='HTML'
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки бонусного постера: {e}")
                        await context.bot.send_message(
                            chat_id=referrer['user_id'],
                            text=f"🎁 <b>Поздравляем!</b> Ты пригласил {count} оплативших друзей и получаешь бесплатный постер #{bonus_id}!",
                            parse_mode='HTML'
                        )
                else:
                    remaining_for_bonus = needed - (count % needed)
                    await context.bot.send_message(
                        chat_id=referrer['user_id'],
                        text=(
                            f"👥 <b>По твоей ссылке оплатил новый участник!</b>\n"
                            f"━━━━━━━━━━━━━━━\n"
                            f"Приглашено оплативших: <b>{count} / 10</b>\n"
                            f"До бесплатного постера: ещё <b>{remaining_for_bonus}</b> 🎁"
                        ),
                        parse_mode='HTML'
                    )
    elif action == 'reject':
        db_update_order(order_id, status='отклонён')
        new_caption = query.message.caption + "\n\n❌ Отклонён"
        await query.edit_message_caption(caption=new_caption, reply_markup=None)
        if user_id == 0:
            await query.message.reply_text(
                f"⚠️ Заказ #{order_id} отклонён, но Telegram ID покупателя неизвестен — уведомление не доставлено.\n"
                f"👤 {order['fio']} | 📞 {order['phone']}"
            )
            return
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"❌ <b>Заказ #{order_id} отклонён.</b>\n\n"
                    f"Если считаешь это ошибкой — отправь чек снова прямо в этот чат."
                ),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 Статус заказа", callback_data=f"mystatus_{order_id}")
                ]])
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить клиента об отклонении заказа #{order_id}: {e}")
            await query.message.reply_text(
                f"⚠️ Заказ #{order_id} отклонён, но уведомление не доставлено (user_id={user_id})."
            )

async def sendreflinks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рассылает реферальные ссылки всем оплатившим. Только для администратора."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text("У тебя нет доступа к этой команде.")
        return

    all_orders = db_get_all_orders(status_filter='заказ принят')
    eligible = [o for o in all_orders if o.get('user_id') and int(o['user_id']) > 0 and not o.get('is_bonus')]

    if not eligible:
        await update.message.reply_text("Нет подтверждённых участников с известным Telegram ID.")
        return

    await update.message.reply_text(
        f"⏳ Начинаю рассылку реферальных ссылок {len(eligible)} участникам..."
    )

    sent, failed = 0, 0
    import asyncio as _asyncio

    for o in eligible:
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{o['id']}"
        try:
            await context.bot.send_message(
                chat_id=int(o['user_id']),
                text=(
                    f"🔗 <b>Твоя реферальная ссылка:</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"{ref_link}\n\n"
                    f"Приглашай друзей! За каждые <b>10 оплативших</b> друзей ты получаешь "
                    f"<b>бесплатный постер 🎁</b>\n\n"
                    f"Прогресс виден прямо в магазине — нажми кнопку ЛЕГЕНДА."
                ),
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "📤 Поделиться ссылкой",
                        url=f"https://t.me/share/url?url={ref_link}&text=Оформи+постер+LEGENDA!"
                    )
                ]])
            )
            sent += 1
        except Exception as e:
            logger.warning(f"sendreflinks: не удалось отправить заказу #{o['id']} (user {o['user_id']}): {e}")
            failed += 1
        await _asyncio.sleep(0.1)   # Пауза чтобы не словить flood limit Telegram

    await update.message.reply_text(
        f"✅ Рассылка завершена\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📨 Отправлено: <b>{sent}</b>\n"
        f"⚠️ Не удалось: <b>{failed}</b> (бот заблокирован или user_id=0)",
        parse_mode='HTML'
    )


async def sendposter_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует и отправляет постер-билет по заказу. Только для администратора."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text("У тебя нет доступа к этой команде.")
        return

    order_id = None
    if context.args:
        try:
            order_id = int(context.args[0])
        except ValueError:
            pass

    if order_id is None:
        import re as _re
        m = _re.search(r'\d+', update.message.text or '')
        if m:
            order_id = int(m.group())

    if order_id is None:
        await update.message.reply_text("Использование: /sendposter13 или /sendposter 13")
        return

    order = db_get_order_by_id(order_id)
    if not order:
        await update.message.reply_text(f"Заказ #{order_id} не найден в базе.")
        return

    await update.message.reply_text(f"⏳ Генерирую постер для заказа #{order_id}...")

    try:
        ticket_buf = generate_ticket(order_id, order['fio'])
        await context.bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=ticket_buf,
            caption=(
                f"🎟 <b>Постер — Заказ #{order_id}</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"👤 {order['fio']}\n"
                f"📞 {order['phone']}\n"
                f"📌 Статус: {order['status']}"
            ),
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"sendposter ошибка: {e}")
        await update.message.reply_text(f"⚠️ Ошибка генерации постера: {e}")


async def renotify_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повторно отправляет уведомление по заказу. Только для администратора."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text("У тебя нет доступа к этой команде.")
        return

    order_id = None
    if context.args:
        try:
            order_id = int(context.args[0])
        except ValueError:
            pass

    if order_id is None:
        import re as _re
        m = _re.search(r'\d+', update.message.text or '')
        if m:
            order_id = int(m.group())

    if order_id is None:
        await update.message.reply_text("Использование: /renotify13 или /renotify 13")
        return

    order = db_get_order_by_id(order_id)
    if not order:
        await update.message.reply_text(f"Заказ #{order_id} не найден в базе.")
        return

    keyboard = [[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_{order_id}"),
        InlineKeyboardButton("❌ Отклонить",   callback_data=f"reject_{order_id}")
    ]]

    if order.get('photo_id'):
        is_pdf = str(order.get('photo_id', '')).startswith('BQ')  # PDF file_id обычно начинается с BQ
        text = (
            f"🔁 <b>Повторное уведомление — заказ #{order_id}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 Покупатель: {order['fio']}\n"
            f"📞 Телефон: {order['phone']}\n"
            f"📌 Статус: {order['status']}"
        )
        try:
            if is_pdf:
                await context.bot.send_document(
                    chat_id=ADMIN_CHAT_ID,
                    document=order['photo_id'],
                    caption=text,
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await context.bot.send_photo(
                    chat_id=ADMIN_CHAT_ID,
                    photo=order['photo_id'],
                    caption=text,
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            await update.message.reply_text(f"✅ Уведомление по заказу #{order_id} отправлено.")
        except Exception as e:
            logger.error(f"renotify ошибка отправки чека: {e}")
            await update.message.reply_text(f"⚠️ Не удалось отправить чек (ошибка: {e}). Попробуй отправить вручную.")
    else:
        text = (
            f"🔁 <b>Повторное уведомление — заказ #{order_id}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 Покупатель: {order['fio']}\n"
            f"📞 Телефон: {order['phone']}\n"
            f"📌 Статус: {order['status']}\n\n"
            f"⚠️ Чек не прикреплён к этому заказу."
        )
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        await update.message.reply_text(f"✅ Уведомление по заказу #{order_id} отправлено (без чека).")


async def clearunpaid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет все заявки без чека (пользователь не отправил оплату).
    Только для администратора. Использование: /clearunpaid
    """
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return

    conn = get_db()
    try:
        with conn.cursor() as cur:
            # Сначала считаем сколько будет удалено
            cur.execute(
                "SELECT COUNT(*) FROM orders WHERE photo_id IS NULL AND status = 'в модерации'"
            )
            count = cur.fetchone()[0]

            if count == 0:
                await update.message.reply_text("✅ Нет неисполненных заявок — таблица чистая.")
                return

            # Удаляем
            cur.execute(
                "DELETE FROM orders WHERE photo_id IS NULL AND status = 'в модерации'"
            )
            conn.commit()
    finally:
        conn.close()

    await update.message.reply_text(
        f"🗑 <b>Удалено {count} неисполненных заявок</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Удалены все заявки без чека (пользователь не отправил оплату).\n"
        f"Подтверждённые и отклонённые — не тронуты.",
        parse_mode='HTML'
    )


async def resetphoto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сброс чека и возврат заказа в «в модерации» — только для администратора.
    Использование: /resetphoto <номер_заказа>
    Пример: /resetphoto 23
    """
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Использование: /resetphoto <номер_заказа>\nПример: /resetphoto 23"
        )
        return

    order_id = int(args[0])
    order = db_get_order_by_id(order_id)
    if not order:
        await update.message.reply_text(f"❌ Заказ #{order_id} не найден.")
        return

    db_reset_order_photo(order_id)
    await update.message.reply_text(
        f"✅ <b>Чек сброшен — заказ #{order_id}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 ФИО: {order['fio']}\n"
        f"📞 Телефон: {order['phone']}\n"
        f"📌 Статус возвращён: <b>в модерации</b>\n\n"
        f"Теперь владелец заявки может прислать правильный чек.",
        parse_mode='HTML'
    )


# ---------- ЗАПУСК (webhook режим) ----------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "LEGSHOP WORKING"
def main():
    print("MAIN STARTED")
    if not TOKEN:
        print("ОШИБКА: не найден токен TELEGRAM_BOT_TOKEN в секретах")
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("orders", orders_command))
    app.add_handler(CommandHandler("order", order_detail_command))
    app.add_handler(CommandHandler("resetorders", resetorders_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CommandHandler("renotify", renotify_command))
    app.add_handler(MessageHandler(filters.Regex(r'^/renotify\d+'), renotify_command))
    app.add_handler(CommandHandler("sendposter", sendposter_command))
    app.add_handler(MessageHandler(filters.Regex(r'^/sendposter\d+'), sendposter_command))
    app.add_handler(CommandHandler("sendreflinks", sendreflinks_command))
    app.add_handler(CommandHandler("resetphoto", resetphoto_command))
    app.add_handler(CommandHandler("clearunpaid", clearunpaid_command))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^/start\\s+order_"), handle_webapp_data))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone_lookup))
    app.add_handler(CallbackQueryHandler(callback_query))
    
    
    if WEBHOOK_URL:
        print(f"[BOT] Webhook mode. URL={WEBHOOK_URL} port={PORT}")
        
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=WEBHOOK_URL,
            drop_pending_updates=False,
        )
    else:
         print("[WEB] Starting Flask...")
         Thread(
             target=lambda: flask_app.run(
                 host="0.0.0.0",
                 port=PORT,
                 debug=False,
                 use_reloader=False
             ),
             daemon=True
         ).start()
         print("[WEB] Flask started")
         app.run_polling(drop_pending_updates=True)
if __name__ == "__main__":
    main()
