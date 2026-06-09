import logging
import os
from pathlib import Path

import gspread

logger = logging.getLogger(__name__)

# =================== КОЛОНКИ GOOGLE ТАБЛИЦЫ ===================

GOOGLE_FORM_COLUMNS = [
    "Кто заказал?",
    "Вещь",
    "Размер",
    "Цена в Юанях(Рублях)",
    "Плата за доставку",
    "Время доставки",
    "Платформа",
    "order_number",
]

# Состояние диалога
FILLING = {}  # user_id -> {"step": int, "values": list}

GOOGLE_SHEET_KEY = os.environ.get("GOOGLE_SHEET_KEY")
if not GOOGLE_SHEET_KEY:
    raise RuntimeError("GOOGLE_SHEET_KEY не задан. Добавьте его в .env")

_worksheet_cache = None


# -------------- Работа с таблицей ------------------

def open_sheet():
    global _worksheet_cache
    if _worksheet_cache is not None:
        return _worksheet_cache
    creds_path = str(Path(__file__).with_name("credentials.json"))
    gc = gspread.service_account(filename=creds_path)
    sh = gc.open_by_key(GOOGLE_SHEET_KEY)
    _worksheet_cache = sh.get_worksheet(0)
    return _worksheet_cache


def _ws_call(op):
    """Выполнить операцию с листом; при APIError пересоздать подключение и повторить."""
    global _worksheet_cache
    try:
        return op(open_sheet())
    except gspread.exceptions.APIError:
        logger.warning("gspread APIError, пересоздаю подключение и повторяю")
        _worksheet_cache = None
        return op(open_sheet())


def append_row(row):
    _ws_call(lambda ws: ws.append_row(row))


def get_next_index():
    return len(_ws_call(lambda ws: ws.col_values(1)))


# -------------- Начало заполнения ------------------

def start_filling(bot, message):
    user_id = message.from_user.id
    FILLING[user_id] = {"step": 0, "values": []}

    bot.send_message(
        message.chat.id,
        f"📝 Начинаем заполнение заказа.\nВведите: *{GOOGLE_FORM_COLUMNS[0]}*",
        parse_mode="Markdown",
    )


# -------------- Продолжение диалога ------------------

def continue_filling(bot, message):
    user_id = message.from_user.id

    if user_id not in FILLING:
        bot.send_message(message.chat.id, "⚠️ Ошибка состояния. Нажмите /addorder")
        return None

    state = FILLING[user_id]
    step = state["step"]

    state["values"].append(message.text)

    if step + 1 < len(GOOGLE_FORM_COLUMNS):
        state["step"] += 1
        next_field = GOOGLE_FORM_COLUMNS[state["step"]]
        bot.send_message(
            message.chat.id,
            f"Введите: *{next_field}*",
            parse_mode="Markdown",
        )
        return None

    saved_values = None
    try:
        index = get_next_index()
        full_row = [index] + state["values"]
        append_row(full_row)
        saved_values = state["values"]

        bot.send_message(
            message.chat.id,
            "✅ Заказ успешно добавлен в таблицу!\nДля нового заказа: /addorder",
        )
    except gspread.exceptions.APIError as e:
        logger.exception("gspread append failed")
        bot.send_message(message.chat.id, f"⚠️ Ошибка записи в таблицу: {e}")
    except Exception as e:
        logger.exception("append_row failed")
        bot.send_message(message.chat.id, f"⚠️ Ошибка записи: {e}")

    del FILLING[user_id]
    return saved_values
