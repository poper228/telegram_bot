import json
import logging
import os
from pathlib import Path

import gspread
import requests
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

from google_form_filler import start_filling, continue_filling, FILLING

# ====================== ЛОГИРОВАНИЕ ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ====================== КОНФИГ ИЗ ENV ======================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Создайте .env по образцу .env.example")

CHANNEL_ID = os.environ.get("CHANNEL_ID", "@idm_review")
LOG_CHAT_ID = os.environ.get("LOG_CHAT_ID", "@botanidm")
WEB_APP_URL = os.environ.get("WEB_APP_URL", "")

_manager_id = os.environ.get("MANAGER_CHAT_ID")
if not _manager_id:
    raise RuntimeError("MANAGER_CHAT_ID не задан. Добавьте его в .env")
MANAGER_CHAT_ID = int(_manager_id)

GOOGLE_SHEET_KEY = os.environ.get("GOOGLE_SHEET_KEY")
if not GOOGLE_SHEET_KEY:
    raise RuntimeError("GOOGLE_SHEET_KEY не задан. Добавьте его в .env")

# ====================== КОНСТАНТЫ ЦЕНООБРАЗОВАНИЯ ======================
YUAN_RATE = 12.5  # курс юаня к рублю
MARKUP = 1.1  # наценка 10%
SERVICE_FEE = 300  # комиссия в рублях

bot = telebot.TeleBot(BOT_TOKEN)

try:
    bot.set_my_commands([types.BotCommand("start", "Запустить бота")])
except ApiTelegramException as e:
    logger.warning("set_my_commands failed: %s", e)

# 🧠 Хранилище в памяти
PENDING_REPLIES = {}
USER_STATES = {}
REQUIRED_CHANNELS = ["@idm_str"]

# Тексты кнопок главного меню — нужны, чтобы выходить из диалогов по нажатию кнопки
MENU_BUTTONS = {
    "🚀 Открыть приложение",
    "🧮 Расчет стоимости",
    "📝 Оставить отзыв",
    "❓ FAQ",
    "🛒 Оформить заказ",
    "📞 Связаться с менеджером",
}


def redirect_if_menu(message):
    """Если внутри диалога пользователь нажал кнопку меню или ввёл команду —
    выходим из диалога и обрабатываем сообщение обычными хэндлерами.

    Возвращает True, если сообщение перенаправлено.
    """
    text = message.text or ""
    if text in MENU_BUTTONS or text.startswith("/"):
        bot.process_new_messages([message])
        return True
    return False

USERS_FILE = Path(__file__).with_name("users.json")


def load_users():
    try:
        if USERS_FILE.exists():
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.exception("load_users failed")
    return {}


def save_users():
    """Атомарная запись через временный файл."""
    try:
        tmp = USERS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(USERS, f, ensure_ascii=False)
        os.replace(tmp, USERS_FILE)
    except OSError:
        logger.exception("save_users failed")


USERS = load_users()


# ====================== Google Sheets с кэшем ======================
_gs_worksheet = None


def get_worksheet():
    global _gs_worksheet
    if _gs_worksheet is not None:
        return _gs_worksheet
    creds_path = str(Path(__file__).with_name("credentials.json"))
    logger.info("gspread auth, creds: %s", creds_path)
    gc = gspread.service_account(filename=creds_path)
    sh = gc.open_by_key(GOOGLE_SHEET_KEY)
    _gs_worksheet = sh.get_worksheet(0)
    return _gs_worksheet


def get_sheet_data():
    global _gs_worksheet
    try:
        data = get_worksheet().col_values(9)[1:]
    except gspread.exceptions.APIError:
        # Сессия могла протухнуть — пересоздаём подключение и пробуем ещё раз
        logger.warning("GS read failed, пересоздаю подключение и повторяю")
        _gs_worksheet = None
        data = get_worksheet().col_values(9)[1:]
    logger.info("GS read OK: %d значений", len(data))
    return data


# ====================== Подписка ======================
def check_subscription(user_id):
    try:
        for channel in REQUIRED_CHANNELS:
            chat_member = bot.get_chat_member(channel, user_id)
            if chat_member.status in ["left", "kicked"]:
                return False
        return True
    except ApiTelegramException:
        logger.exception("check_subscription failed")
        return False


def subscription_required(func):
    def wrapper(message, *args, **kwargs):
        if check_subscription(message.from_user.id):
            return func(message, *args, **kwargs)
        markup = types.InlineKeyboardMarkup()
        for channel in REQUIRED_CHANNELS:
            markup.add(
                types.InlineKeyboardButton(
                    "📢 Подписаться", url=f"https://t.me/{channel[1:]}"
                )
            )
        bot.send_message(
            message.chat.id,
            "⚠️ Чтобы пользоваться ботом, подпишитесь на канал:",
            reply_markup=markup,
        )

    return wrapper


# ====================== Логирование сообщений ======================
def log_user_message(message, note=""):
    try:
        if message.from_user.id == MANAGER_CHAT_ID:
            return

        user_id = message.from_user.id
        if message.from_user.username:
            key = message.from_user.username.lower()
            if USERS.get(key) != user_id:
                USERS[key] = user_id
                save_users()

        username = (
            f"@{message.from_user.username}"
            if message.from_user.username
            else message.from_user.first_name
        )
        text_or_media = message.text if message.text else "[Медиа]"
        caption = f"📩 {note}Сообщение от {username} ({user_id}):\n{text_or_media}"

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton(
                "Ответить пользователю", callback_data=f"reply_{user_id}"
            )
        )

        bot.forward_message(LOG_CHAT_ID, message.chat.id, message.message_id)
        bot.send_message(LOG_CHAT_ID, caption, reply_markup=markup)

    except ApiTelegramException:
        logger.exception("log_user_message failed")


# ====================== Главное меню ======================
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if WEB_APP_URL:
        markup.add(
            types.KeyboardButton(
                "🚀 Открыть приложение", web_app=types.WebAppInfo(url=WEB_APP_URL)
            )
        )
    markup.add("🧮 Расчет стоимости", "📝 Оставить отзыв")
    markup.add("❓ FAQ", "🛒 Оформить заказ")
    markup.add("📞 Связаться с менеджером")
    return markup


# ====================== /start ======================
@bot.message_handler(commands=["start"])
@subscription_required
def send_welcome(message):
    log_user_message(message, note="[START] ")
    bot.send_message(
        message.chat.id,
        "👋 Добро пожаловать в iDM store!\n"
        "Мы помогаем с покупками и доставкой товаров из китайских маркетплейсов 📦\n\n"
        "Выберите действие:",
        reply_markup=main_menu(),
    )


# ====================== Расчёт стоимости ======================
@bot.message_handler(func=lambda message: message.text == "🧮 Расчет стоимости")
@subscription_required
def calc_cost(message):
    log_user_message(message, note="[Кнопка меню] ")
    bot.send_message(message.chat.id, "Введите стоимость товара в юанях:")
    bot.register_next_step_handler(message, process_yuan_input)


def process_yuan_input(message):
    if redirect_if_menu(message):
        return
    log_user_message(message, note="[Расчет стоимости] ")
    try:
        yuan = float((message.text or "").replace(",", "."))
        rub = yuan * YUAN_RATE * MARKUP + SERVICE_FEE
        bot.send_message(message.chat.id, f"Итого к оплате: {rub:.2f} ₽")
    except ValueError:
        bot.send_message(message.chat.id, "Пожалуйста, введите число.")


# ====================== /addorder (только админ) ======================
@bot.message_handler(commands=["addorder"])
def addorder(message):
    if message.from_user.id != MANAGER_CHAT_ID:
        bot.reply_to(message, "❌ Неизвестная команда.")
        return

    start_filling(bot, message)
    bot.register_next_step_handler(message, fill_step)


def fill_step(message):
    if redirect_if_menu(message):
        FILLING.pop(message.from_user.id, None)
        return

    saved_values = continue_filling(bot, message)

    if message.from_user.id in FILLING:
        bot.register_next_step_handler(message, fill_step)
        return

    if saved_values:
        notify_customer_about_order(saved_values)


def notify_customer_about_order(values):
    """values = [Кто заказал, Вещь, Размер, Цена, Доставка, Срок, Платформа, order_number]"""
    try:
        who_raw, _item, _size, _price, _delivery, eta, _platform, order_no = values
    except ValueError:
        logger.error("notify_customer_about_order: неверный формат values: %r", values)
        return

    username = who_raw.lstrip("@").strip().lower() if who_raw else ""

    if not username or username not in USERS:
        try:
            bot.send_message(
                LOG_CHAT_ID,
                f"⚠️ Заказ {order_no} оформлен, но клиент «{who_raw}» "
                f"не писал боту — уведомление не отправлено.",
            )
        except ApiTelegramException:
            logger.exception("notify fallback failed")
        return

    user_id = USERS[username]
    text = (
        f"✅ Ваш заказ оформлен!\n\n"
        f"🔢 Номер заказа: {order_no}\n"
        f"⏱ Срок доставки: {eta}\n\n"
        f"Мы сообщим, когда посылка будет у нас. "
        f"Если есть вопросы — напишите нам 🙌"
    )
    try:
        bot.send_message(user_id, text)
        bot.send_message(
            LOG_CHAT_ID,
            f"📨 Уведомление о заказе {order_no} отправлено @{username} (id={user_id}).",
        )
    except ApiTelegramException as e:
        logger.warning("notify user %s failed: %s", user_id, e)
        try:
            bot.send_message(
                LOG_CHAT_ID,
                f"⚠️ Не удалось уведомить @{username} (id={user_id}) о заказе {order_no}: {e}",
            )
        except ApiTelegramException:
            logger.exception("notify error log failed")


# ====================== Отзыв ======================
@bot.message_handler(func=lambda message: message.text == "📝 Оставить отзыв")
@subscription_required
def request_order_number(message):
    log_user_message(message, note="[Кнопка меню] ")
    bot.send_message(message.chat.id, "Введите номер вашего заказа:")
    bot.register_next_step_handler(message, check_order_google)


def check_order_google(message):
    if redirect_if_menu(message):
        return
    log_user_message(message, note="[Проверка заказа] ")
    order_num = (message.text or "").strip().upper()
    try:
        valid_orders = get_sheet_data()
    except gspread.exceptions.APIError as e:
        logger.exception("gs check failed")
        bot.send_message(message.chat.id, f"⚠️ Ошибка при проверке заказа: {e}")
        return

    if order_num in valid_orders:
        bot.send_message(
            message.chat.id, "✅ Заказ найден! Напишите ваш отзыв с фото/видео."
        )
        bot.register_next_step_handler(message, collect_review, order_num)
    else:
        bot.send_message(message.chat.id, "❌ Данный заказ отсутствует в базе.")


def collect_review(message, order_num):
    if redirect_if_menu(message):
        return
    log_user_message(message, note="[Отзыв] ")
    try:
        bot.forward_message(CHANNEL_ID, message.chat.id, message.message_id)
        bot.send_message(
            message.chat.id,
            "✅ Спасибо за отзыв! Он опубликован в канале.",
            reply_markup=main_menu(),
        )
    except ApiTelegramException as e:
        logger.exception("forward review failed")
        bot.send_message(
            message.chat.id,
            f"⚠️ Не удалось отправить отзыв: {e}",
            reply_markup=main_menu(),
        )


# ====================== Оформить заказ ======================
@bot.message_handler(func=lambda message: message.text == "🛒 Оформить заказ")
@subscription_required
def order_process(message):
    log_user_message(message, note="[Кнопка меню] ")
    USER_STATES[message.from_user.id] = "ordering"
    bot.send_message(
        message.chat.id,
        "Отправьте ссылку на товар или фото/видео/документ, который хотите заказать.\n\n"
        "❗ Если передумали, просто нажмите другую кнопку меню.",
    )


def process_order_message(message):
    log_user_message(message, note="[Заказ] ")
    bot.send_message(
        message.chat.id,
        "✅ Ваш заказ получен! Менеджер свяжется с вами в течение часа.",
        reply_markup=main_menu(),
    )

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else message.from_user.first_name
    )
    caption = (
        f"🛒 Новый заказ от {username} ({message.from_user.id}):\n"
        f"{message.text if message.text else '[Прикреплено медиа]'}"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "Ответить пользователю", callback_data=f"reply_{message.from_user.id}"
        )
    )

    # 1) Лог-канал
    try:
        bot.forward_message(LOG_CHAT_ID, message.chat.id, message.message_id)
        bot.send_message(LOG_CHAT_ID, caption, reply_markup=markup)
    except ApiTelegramException:
        logger.exception("order to log chat failed")

    # 2) ЛИЧНО менеджеру — чтобы он действительно увидел и связался
    try:
        fwd = bot.forward_message(MANAGER_CHAT_ID, message.chat.id, message.message_id)
        PENDING_REPLIES[fwd.message_id] = message.chat.id
        bot.send_message(MANAGER_CHAT_ID, caption, reply_markup=markup)
    except ApiTelegramException:
        logger.exception("order to manager failed")


# ====================== FAQ ======================
FAQ_ITEMS = {
    "Время доставки": "Срок доставки: 18–30 дней.",
    "Какие маркетплейсы?": "Poizon,TaoBao,95,PinduDuo,Tmall,Shein,Asos",
    "Соцсети": "Мы в Telegram: @idm_str, Отзывы: @idm_review, TikTok: idm.cargo, YouTube: idm.store",
    "Процесс покупки до доставки": (
        "1️⃣ Скидываете ссылку.\n2️⃣ Обсуждаете с менеджером.\n3️⃣ Оплата и выкуп.\n"
        "4️⃣ Получаете трек-код.\n5️⃣ Ждёте доставку."
    ),
    "Где скачать приложения?": "Android: sj.qq.com\niOS: App Store",
}


def build_faq_markup():
    markup = types.InlineKeyboardMarkup()
    for k in FAQ_ITEMS:
        markup.add(types.InlineKeyboardButton(k, callback_data=f"faq_{k}"))
    return markup


@bot.message_handler(func=lambda message: message.text == "❓ FAQ")
@subscription_required
def faq_menu(message):
    log_user_message(message, note="[Кнопка меню] ")
    bot.send_message(
        message.chat.id, "Выберите вопрос:", reply_markup=build_faq_markup()
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("faq_"))
def handle_faq(call):
    key = call.data[4:]
    answer = FAQ_ITEMS.get(key, "❌ Вопрос не найден")
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"❓ {key}\n\n{answer}",
            reply_markup=build_faq_markup(),
        )
    except ApiTelegramException as e:
        # игнорируем "message is not modified"
        if "message is not modified" not in str(e):
            logger.warning("faq edit failed: %s", e)


# ====================== Связаться с менеджером ======================
@bot.message_handler(func=lambda message: message.text == "📞 Связаться с менеджером")
@subscription_required
def contact_manager(message):
    log_user_message(message, note="[Кнопка меню] ")
    bot.send_message(message.chat.id, "Введите ваш вопрос для менеджера:")
    bot.register_next_step_handler(message, forward_to_manager_question)


def forward_to_manager_question(message):
    if redirect_if_menu(message):
        return
    log_user_message(message, note="[Вопрос менеджеру] ")
    try:
        fwd_msg = bot.forward_message(
            MANAGER_CHAT_ID, message.chat.id, message.message_id
        )
        PENDING_REPLIES[fwd_msg.message_id] = message.chat.id
        bot.send_message(
            message.chat.id,
            "✅ Менеджер свяжется с вами в течение часа.",
            reply_markup=main_menu(),
        )
    except ApiTelegramException as e:
        logger.exception("forward to manager failed")
        bot.send_message(
            message.chat.id,
            f"⚠️ Не удалось отправить сообщение: {e}",
            reply_markup=main_menu(),
        )


# ====================== /send (только админ) ======================
@bot.message_handler(commands=["send"])
def send_command(message):
    if message.from_user.id != MANAGER_CHAT_ID:
        bot.reply_to(message, "❌ Команда доступна только менеджеру.")
        return

    log_user_message(message, note="[SEND команда] ")
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(
            message, "⚠️ Использование: /send <user_id | @username> <сообщение>"
        )
        return

    target, text = parts[1], parts[2]

    try:
        if target.isdigit():
            user_id = int(target)
            bot.send_message(user_id, text)
            bot.reply_to(message, f"✅ Сообщение отправлено пользователю {user_id}")
            return

        if target.startswith("@"):
            username = target[1:].lower()
            if username in USERS:
                user_id = USERS[username]
                bot.send_message(user_id, text)
                bot.reply_to(
                    message, f"✅ Сообщение отправлено пользователю @{username}"
                )
            else:
                bot.reply_to(
                    message,
                    f"⚠️ Я ещё не видел @{username}, не могу отправить сообщение.",
                )
            return

        bot.reply_to(message, "⚠️ Укажите корректный ID или @username.")
    except ApiTelegramException as e:
        logger.exception("/send failed")
        bot.reply_to(message, f"⚠️ Ошибка при отправке: {e}")


# ====================== Ответы менеджера клиентам ======================
@bot.message_handler(
    func=lambda message: message.reply_to_message is not None
    and message.from_user.id == MANAGER_CHAT_ID
    and message.chat.id == MANAGER_CHAT_ID,
    content_types=["text", "photo", "video", "document", "voice", "audio"],
)
def reply_to_client(message):
    log_user_message(message, note="[Ответ менеджера клиенту] ")
    reply_msg_id = message.reply_to_message.message_id
    if reply_msg_id not in PENDING_REPLIES:
        bot.send_message(
            message.chat.id, "ℹ️ Это сообщение не связано с запросом клиента."
        )
        return
    client_id = PENDING_REPLIES[reply_msg_id]
    try:
        if message.text:
            bot.send_message(client_id, f"💬 Ответ от менеджера:\n\n{message.text}")
        else:
            # Фото, видео, голосовое и т.п. — копируем как есть
            bot.send_message(client_id, "💬 Ответ от менеджера:")
            bot.copy_message(client_id, message.chat.id, message.message_id)
        bot.send_message(message.chat.id, "✅ Ответ отправлен клиенту.")
        del PENDING_REPLIES[reply_msg_id]
    except ApiTelegramException as e:
        logger.exception("reply_to_client failed")
        bot.send_message(message.chat.id, f"⚠️ Ошибка: {e}")


# ====================== Кнопка «Ответить пользователю» (только менеджер) ======================
@bot.callback_query_handler(func=lambda call: call.data.startswith("reply_"))
def handle_reply_button(call):
    if call.from_user.id != MANAGER_CHAT_ID:
        try:
            bot.answer_callback_query(
                call.id, "❌ Доступно только менеджеру.", show_alert=True
            )
        except ApiTelegramException:
            pass
        return

    try:
        user_id = int(call.data.split("_")[1])
        msg = bot.send_message(
            call.from_user.id, f"✏️ Введите сообщение для пользователя {user_id}:"
        )
        bot.register_next_step_handler(msg, send_message_to_user, user_id)
    except (ValueError, ApiTelegramException) as e:
        logger.exception("handle_reply_button failed")
        bot.send_message(call.from_user.id, f"⚠️ Ошибка: {e}")


def send_message_to_user(message, user_id):
    try:
        if message.text:
            bot.send_message(user_id, message.text)
        else:
            bot.copy_message(user_id, message.chat.id, message.message_id)
        bot.send_message(
            MANAGER_CHAT_ID, f"✅ Сообщение отправлено пользователю {user_id}"
        )
    except ApiTelegramException as e:
        logger.exception("send_message_to_user failed")
        bot.send_message(MANAGER_CHAT_ID, f"⚠️ Не удалось отправить сообщение: {e}")


# ====================== /gs_check ======================
@bot.message_handler(commands=["gs_check"])
def gs_check(message):
    if message.from_user.id != MANAGER_CHAT_ID:
        return
    try:
        vals = get_sheet_data()
        bot.reply_to(message, f"GS OK. Строк: {len(vals)}. Примеры: {vals[:5]}")
    except gspread.exceptions.APIError as e:
        bot.reply_to(message, f"GS ERROR: {e}")


# ====================== Web App данные ======================
@bot.message_handler(content_types=["web_app_data"])
def handle_web_app_data(message):
    try:
        data = json.loads(message.web_app_data.data)
    except (ValueError, AttributeError) as e:
        logger.exception("web_app_data parse failed")
        bot.send_message(message.chat.id, f"⚠️ Ошибка данных: {e}")
        return

    action = data.get("action")

    if action == "order":
        link = (data.get("link") or "").strip()
        comment = (data.get("comment") or "").strip()

        bot.send_message(
            message.chat.id,
            "✅ Ваш заказ получен! Менеджер свяжется с вами в течение часа.",
            reply_markup=main_menu(),
        )

        username = (
            f"@{message.from_user.username}"
            if message.from_user.username
            else message.from_user.first_name
        )
        caption = (
            f"🛒 Новый заказ (Mini App) от {username} ({message.from_user.id}):\n\n"
            f"🔗 Ссылка: {link or '—'}\n"
            f"💬 Комментарий: {comment or '—'}"
        )

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton(
                "Ответить пользователю", callback_data=f"reply_{message.from_user.id}"
            )
        )

        try:
            bot.send_message(LOG_CHAT_ID, caption, reply_markup=markup)
        except ApiTelegramException:
            logger.exception("web_app order to log chat failed")

        try:
            bot.send_message(MANAGER_CHAT_ID, caption, reply_markup=markup)
        except ApiTelegramException:
            logger.exception("web_app order to manager failed")
    else:
        bot.send_message(message.chat.id, "⚠️ Неизвестное действие из мини-приложения.")


# ====================== Catch-all входящих ======================
@bot.message_handler(content_types=["text", "photo", "video", "document"])
def handle_all_messages(message):
    user_id = message.from_user.id

    if USER_STATES.get(user_id) == "ordering":
        process_order_message(message)
        USER_STATES[user_id] = None
        return

    log_user_message(message)


# ====================== Запуск ======================
if __name__ == "__main__":
    logger.info("Проверка токена...")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    try:
        r = requests.get(url, timeout=10)
    except requests.RequestException as e:
        logger.error("Ошибка при проверке токена: %s", e)
        r = None

    if r and r.status_code == 200 and r.json().get("ok"):
        logger.info("✅ Токен рабочий. Бот: @%s", r.json()["result"]["username"])
        bot.infinity_polling(timeout=60, long_polling_timeout=30, skip_pending=True)
    else:
        logger.error(
            "❌ Ошибка токена! Ответ Telegram: %s", r.text if r else "нет ответа"
        )
