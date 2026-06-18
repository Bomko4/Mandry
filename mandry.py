import asyncio
import gspread
import socket
import random
import os
from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from zoneinfo import ZoneInfo

socket.setdefaulttimeout(10)
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import BotCommand
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    raise SystemExit(f"Не задано обов'язкову змінну середовища: {name}")


TOKEN = get_required_env("BOT_TOKEN")
SHEET_NAME = os.getenv("SHEET_NAME", "Мандри бронь")
MENU_URL = os.getenv(
    "MENU_URL",
    "https://mandry-sup.choiceqr.com/menu?fbclid=PAdGRleAQvo3hleHRuA2FlbQIxMQABpyHGzMZ2j68WOIA7gDKCuCqXp30fz7ITK-gRUKSmhmg5-lJYxrIhrYtnu0A0_aem_1Qlu08flvE4mnL3TsIC_pw",
)

staff_chat_id_raw = os.getenv("STAFF_CHAT_ID")
if staff_chat_id_raw:
    try:
        STAFF_CHAT_ID = int(staff_chat_id_raw)
    except ValueError as err:
        raise SystemExit("STAFF_CHAT_ID має бути числом, наприклад -1001234567890") from err
else:
    STAFF_CHAT_ID = None

GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
APP_TIMEZONE = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Kyiv"))
BLACKLIST_SHEET_NAME = os.getenv("BLACKLIST_SHEET_NAME", "Чорний Список")

COLUMNS = [
    "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий",
    "Сап червоний", "Сап червоний", "Сап червоний", "Сап червоний", "Сап червоний", "Сап червоний", "Сап червоний", "Сап червоний",
    "Сап Оранжевий", "Сап Оранжевий",
    "Каяк двомісний", "Каяк одномісний"
]

TIME_SLOTS = [
    "10:15-11:15", "11:30-12:30", "12:45-13:45", "14:00-15:00",
    "15:15-16:15", "16:30-17:30", "17:45-18:45", "19:00-20:00", "20:10-21:10"
]

MORNING_WINDOWS = ["05:30-06:30", "06:30-07:30", "07:30-08:30", "08:30-09:30"]
MORNING_STARTS = [w.split("-")[0] for w in MORNING_WINDOWS]

EQUIPMENT_OPTIONS = [
    ("Сап одномісний", "sup_single"),
    ("Сап двомісний", "sup_double"),
    ("Каяк одномісний", "kayak_single"),
    ("Каяк двомісний", "kayak_double"),
]

EQUIPMENT_LABELS = {
    "sup_single": "Сап одномісний",
    "sup_double": "Сап двомісний",
    "kayak_single": "Каяк одномісний",
    "kayak_double": "Каяк двомісний",
}

EQUIPMENT_COLUMN_GROUPS = {
    "sup_single": ["Сап білий"],
    "sup_double": ["Сап червоний", "Сап Оранжевий"],
    "kayak_single": ["Каяк одномісний"],
    "kayak_double": ["Каяк двомісний"],
}

try:
    gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_PATH)
    sh = gc.open(SHEET_NAME)
except RefreshError as err:
    raise SystemExit(
        "\nПомилка авторизації Google Service Account: invalid_grant (account not found).\n"
        "Що перевірити:\n"
        f"1) У {GOOGLE_CREDENTIALS_PATH} поле client_email має існувати в Google Cloud IAM.\n"
        "2) Якщо service account видалений/перейменований - створіть новий ключ JSON.\n"
        "3) Увімкніть Google Drive API та Google Sheets API в тому самому проєкті.\n"
        "4) Відкрийте таблицю для client_email (доступ Editor).\n"
        f"\nДеталі: {err}"
    ) from err

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Активні таски нагадувань за кодом бронювання.
reminder_tasks: dict[str, asyncio.Task] = {}

class Booking(StatesGroup):
    date = State()
    duration = State()
    equipment = State()
    time = State()
    quantity = State()
    name = State()
    phone = State()
    cancel_code = State()


def parse_booking_datetime(date_str: str, start_time_str: str) -> datetime:
    now = get_current_time()
    day = datetime.strptime(date_str, "%d.%m")
    start_time = datetime.strptime(start_time_str, "%H:%M")
    booking_dt = datetime(
        year=now.year,
        month=day.month,
        day=day.day,
        hour=start_time.hour,
        minute=start_time.minute,
        tzinfo=APP_TIMEZONE,
    )
    if booking_dt < now - timedelta(days=1):
        booking_dt = booking_dt.replace(year=now.year + 1)
    return booking_dt


def cancel_reminder_task(booking_code: str):
    task = reminder_tasks.pop(booking_code, None)
    if task and not task.done():
        task.cancel()


async def send_reminder(
    user_chat_id: int,
    booking_code: str,
    booking_date: str,
    booking_window: str,
    actual_equipment: str,
    equipment_note: str,
):
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(
            text="✅ Так, все в силі",
            callback_data=f"rem_yes_{booking_code}",
        )
    )
    builder.row(
        types.InlineKeyboardButton(
            text="❌ Ні, передумав",
            callback_data=f"rem_no_{booking_code}",
        )
    )

    await bot.send_message(
        chat_id=user_chat_id,
        text=(
            "⏰ Нагадування про бронювання\n"
            f"Код: {booking_code}\n"
            f"Дата: {booking_date}\n"
            f"Час: {booking_window}\n"
            f"Обладнання: {actual_equipment} {equipment_note}".rstrip()
        ),
        reply_markup=builder.as_markup(),
    )


async def reminder_worker(
    user_chat_id: int,
    booking_code: str,
    booking_date: str,
    booking_window: str,
    actual_equipment: str,
    equipment_note: str,
):
    try:
        start_time_str = booking_window.split("-")[0]
        booking_start_dt = parse_booking_datetime(booking_date, start_time_str)
        reminder_dt = booking_start_dt - timedelta(hours=2)
        delay_seconds = (reminder_dt - get_current_time()).total_seconds()

        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        await send_reminder(
            user_chat_id=user_chat_id,
            booking_code=booking_code,
            booking_date=booking_date,
            booking_window=booking_window,
            actual_equipment=actual_equipment,
            equipment_note=equipment_note,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        reminder_tasks.pop(booking_code, None)


def schedule_booking_reminder(
    user_chat_id: int,
    booking_code: str,
    booking_date: str,
    booking_window: str,
    actual_equipment: str,
    equipment_note: str,
):
    cancel_reminder_task(booking_code)
    reminder_tasks[booking_code] = asyncio.create_task(
        reminder_worker(
            user_chat_id=user_chat_id,
            booking_code=booking_code,
            booking_date=booking_date,
            booking_window=booking_window,
            actual_equipment=actual_equipment,
            equipment_note=equipment_note,
        )
    )


def booking_code_exists(code: str) -> bool:
    marker = f"ID:{code}"
    for ws in sh.worksheets():
        values = ws.get_all_values()
        for row in values:
            for cell in row:
                if marker in cell:
                    return True
    return False


def generate_booking_code() -> str:
    for _ in range(1000):
        code = f"{random.randint(0, 99999):05d}"
        if not booking_code_exists(code):
            return code
    raise RuntimeError("Не вдалося згенерувати унікальний код бронювання")


def normalize_phone_number(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


def is_phone_blacklisted(phone: str) -> bool:
    normalized_phone = normalize_phone_number(phone)
    if not normalized_phone:
        return False

    try:
        blacklist_ws = sh.worksheet(BLACKLIST_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        return False

    for row in blacklist_ws.get_all_values():
        for cell in row:
            if normalize_phone_number(cell) == normalized_phone:
                return True

    return False


def find_and_clear_booking_by_code(code: str):
    marker = f"ID:{code}"
    cleared_cells = []
    booking_name = ""

    for ws in sh.worksheets():
        values = ws.get_all_values()
        for r_idx, row in enumerate(values, start=1):
            for c_idx, cell in enumerate(row, start=1):
                if marker in cell:
                    if not booking_name:
                        lines = cell.split("\n")
                        if len(lines) > 1:
                            booking_name = lines[1].strip()
                        elif "|" in cell:
                            booking_name = cell.split("|", 1)[1].strip()
                    cleared_cells.append((ws, r_idx, c_idx))

    for ws, r_idx, c_idx in cleared_cells:
        ws.update(gspread.utils.rowcol_to_a1(r_idx, c_idx), [[""]])

    return len(cleared_cells), booking_name


def get_target_columns_for_names(names: list[str]) -> list[int]:
    # COLUMNS lists equipment starting from sheet column B (A is 'ВІКНО'),
    # so add 2 to enumerate index to get 1-based sheet column numbers.
    return [index + 2 for index, name in enumerate(COLUMNS) if name in names]


def find_free_column_for_duration(all_values, row_idx: int, duration: int, target_cols: list[int]):
    for col in target_cols:
        is_block_free = True
        for offset in range(duration):
            current_row_idx = row_idx + offset
            current_row_data = all_values[current_row_idx - 1] if current_row_idx - 1 < len(all_values) else []
            if col - 1 < len(current_row_data) and current_row_data[col - 1].strip():
                is_block_free = False
                break

        if is_block_free:
            return col

    return None


def find_free_columns_for_duration(all_values, row_idx: int, duration: int, target_cols: list[int], quantity: int) -> list[int]:
    free_cols = []

    for col in target_cols:
        is_block_free = True
        for offset in range(duration):
            current_row_idx = row_idx + offset
            current_row_data = all_values[current_row_idx - 1] if current_row_idx - 1 < len(all_values) else []
            if col - 1 < len(current_row_data) and current_row_data[col - 1].strip():
                is_block_free = False
                break

        if is_block_free:
            free_cols.append(col)
            if len(free_cols) >= quantity:
                return free_cols

    return []


def build_time_window(start_index: int, duration: int) -> str:
    start_time = TIME_SLOTS[start_index].split('-')[0]
    start_dt = datetime.strptime(start_time, "%H:%M")
    end_time = (start_dt + timedelta(hours=duration)).strftime("%H:%M")
    return f"{start_time}-{end_time}"


def get_current_time() -> datetime:
    return datetime.now(APP_TIMEZONE)


def resolve_equipment_booking(requested_equipment: str, all_values, row_idx: int, duration: int):
    preferred_names = EQUIPMENT_COLUMN_GROUPS[requested_equipment]
    preferred_target_cols = get_target_columns_for_names(preferred_names)

    # Try to find at least one free column in preferred group
    free_pref = find_free_columns_for_duration(all_values, row_idx, duration, preferred_target_cols, 1)
    if free_pref:
        col = free_pref[0]
        return {
            "resolved_equipment": requested_equipment,
            "equip_col": col,
            "actual_equipment": EQUIPMENT_LABELS[requested_equipment],
            "note": "",
        }

    # If requesting single sup, allow fallback to double if no single available
    if requested_equipment == "sup_single":
        double_target_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS["sup_double"])
        free_double = find_free_columns_for_duration(all_values, row_idx, duration, double_target_cols, 1)
        if free_double:
            col = free_double[0]
            return {
                "resolved_equipment": "sup_double",
                "equip_col": col,
                "actual_equipment": "Сап двомісний",
                "note": "(одна людина)",
            }

    return None

def has_dash_reserved_slots(all_values, row_idx: int, duration: int, target_cols: list[int]) -> bool:
    """Check if all target columns are marked with '-' (reserved for live queue)"""
    if not target_cols:
        return False
    
    for col in target_cols:
        for offset in range(duration):
            current_row_idx = row_idx + offset
            current_row_data = all_values[current_row_idx - 1] if current_row_idx - 1 < len(all_values) else []
            cell_value = current_row_data[col - 1].strip() if col - 1 < len(current_row_data) else ""
            # If any cell is not "-", then it's not all reserved for live queue
            if cell_value != "-":
                return False
    
    return True

def get_non_dash_columns(all_values, row_idx: int, duration: int, target_cols: list[int]) -> list[int]:
    """Get columns that are NOT marked with '-'"""
    non_dash_cols = []
    
    for col in target_cols:
        has_dash = False
        for offset in range(duration):
            current_row_idx = row_idx + offset
            current_row_data = all_values[current_row_idx - 1] if current_row_idx - 1 < len(all_values) else []
            cell_value = current_row_data[col - 1].strip() if col - 1 < len(current_row_data) else ""
            if cell_value == "-":
                has_dash = True
                break
        if not has_dash:
            non_dash_cols.append(col)
    
    return non_dash_cols

def are_all_non_dash_columns_occupied(all_values, row_idx: int, duration: int, target_cols: list[int]) -> bool:
    """Check if all non-dash columns are occupied (have content)"""
    non_dash_cols = get_non_dash_columns(all_values, row_idx, duration, target_cols)
    
    if not non_dash_cols:
        # All columns are marked with "-", so online booking is unavailable (live queue mode)
        return True
    
    # Check if all non-dash columns are occupied
    for col in non_dash_cols:
        is_column_occupied = True
        for offset in range(duration):
            current_row_idx = row_idx + offset
            current_row_data = all_values[current_row_idx - 1] if current_row_idx - 1 < len(all_values) else []
            cell_value = current_row_data[col - 1].strip() if col - 1 < len(current_row_data) else ""
            if not cell_value:
                # Found an empty cell in this column
                is_column_occupied = False
                break
        if not is_column_occupied:
            # Found a column that's not fully occupied
            return False
    
    # All non-dash columns are fully occupied
    return True

def get_or_create_sheet(date_str):
    try:
        ws = sh.worksheet(date_str)
        existing_values = ws.col_values(1)
        for index, slot in enumerate(TIME_SLOTS, start=2):
            current_value = existing_values[index - 1].strip() if len(existing_values) >= index else ""
            if current_value != slot:
                ws.update(gspread.utils.rowcol_to_a1(index, 1), [[slot]])
        return ws
    except gspread.exceptions.WorksheetNotFound:
        new_ws = sh.add_worksheet(title=date_str, rows="100", cols="20")
        
        headers = ["ВІКНО"] + COLUMNS
        new_ws.update('A1', [headers])
        
        time_col = [[t] for t in TIME_SLOTS]
        end_row = 1 + len(TIME_SLOTS)
        new_ws.update(f'A2:A{end_row}', time_col)
        return new_ws


def ensure_morning_table(ws):
    """Ensure the worksheet has a morning table block with a header 'Ранковий сплав' and morning rows."""
    try:
        all_vals = ws.get_all_values()
    except Exception:
        all_vals = []

    header_row_idx = None
    for r_idx, row in enumerate(all_vals):
        if row and len(row) > 0 and isinstance(row[0], str) and row[0].strip().lower().startswith("ранков"):
            header_row_idx = r_idx
            break

    if header_row_idx is not None:
        return

    # Append header and morning rows at the bottom
    header = ["Ранковий сплав"] + COLUMNS
    ws.append_row(header)
    for w in MORNING_WINDOWS:
        row = [w] + ["" for _ in COLUMNS]
        ws.append_row(row)


def is_weather_blocked_sheet(all_values) -> bool:
    for row_idx in range(1, len(TIME_SLOTS) + 1):
        current_row_data = all_values[row_idx] if row_idx < len(all_values) else []
        for col_idx in range(1, len(COLUMNS) + 1):
            cell_value = current_row_data[col_idx] if col_idx < len(current_row_data) else ""
            if cell_value.strip() != "*":
                return False
    return True

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()

    reply_keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="📚 Забронювати"), types.KeyboardButton(text="❌ Скасувати бронювання")],
            [types.KeyboardButton(text="📋 Правила користування"), types.KeyboardButton(text="🚨 Краш ліст")],
            [types.KeyboardButton(text="🍽️ Меню")],
            [types.KeyboardButton(text="📞 Контакти")]
        ],
        resize_keyboard=True
    )

    await message.answer("Головне меню:", reply_markup=reply_keyboard)

@dp.message(F.text == "📚 Забронювати")
async def start_booking_from_menu(message: types.Message, state: FSMContext):
    builder = InlineKeyboardBuilder()
    now = get_current_time()
    left_column_dates = [(now + timedelta(days=day_offset)).strftime("%d.%m") for day_offset in range(7)]
    right_column_dates = [(now + timedelta(days=day_offset)).strftime("%d.%m") for day_offset in range(7, 14)]

    for left_date, right_date in zip(left_column_dates, right_column_dates):
        builder.row(
            types.InlineKeyboardButton(text=left_date, callback_data=f"date_{left_date}"),
            types.InlineKeyboardButton(text=right_date, callback_data=f"date_{right_date}"),
        )

    # Add a single larger button to navigate to the next two weeks
    builder.row(types.InlineKeyboardButton(text="➡️", callback_data="dates_next"))

    await message.answer("Оберіть дату:", reply_markup=builder.as_markup())
    await state.set_state(Booking.date)


@dp.callback_query(F.data == "dates_next")
async def show_next_dates(callback: types.CallbackQuery):
    now = get_current_time()
    # show days 15..28 (offsets 14..27)
    left_column_dates = [(now + timedelta(days=day_offset)).strftime("%d.%m") for day_offset in range(14, 21)]
    right_column_dates = [(now + timedelta(days=day_offset)).strftime("%d.%m") for day_offset in range(21, 28)]

    builder = InlineKeyboardBuilder()
    for left_date, right_date in zip(left_column_dates, right_column_dates):
        builder.row(
            types.InlineKeyboardButton(text=left_date, callback_data=f"date_{left_date}"),
            types.InlineKeyboardButton(text=right_date, callback_data=f"date_{right_date}"),
        )

    # show a back button to return to the previous two weeks
    builder.row(types.InlineKeyboardButton(text="⬅️", callback_data="dates_prev"))

    await callback.message.edit_text("Оберіть дату:", reply_markup=builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "dates_prev")
async def show_prev_dates(callback: types.CallbackQuery):
    now = get_current_time()
    left_column_dates = [(now + timedelta(days=day_offset)).strftime("%d.%m") for day_offset in range(7)]
    right_column_dates = [(now + timedelta(days=day_offset)).strftime("%d.%m") for day_offset in range(7, 14)]

    builder = InlineKeyboardBuilder()
    for left_date, right_date in zip(left_column_dates, right_column_dates):
        builder.row(
            types.InlineKeyboardButton(text=left_date, callback_data=f"date_{left_date}"),
            types.InlineKeyboardButton(text=right_date, callback_data=f"date_{right_date}"),
        )

    builder.row(types.InlineKeyboardButton(text="➡️", callback_data="dates_next"))

    await callback.message.edit_text("Оберіть дату:", reply_markup=builder.as_markup())
    await callback.answer()

@dp.message(F.text == "🍽️ Меню")
async def show_prices(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Відкрити меню", url=MENU_URL))
    await message.answer("Натисніть кнопку нижче, щоб відкрити меню:", reply_markup=builder.as_markup())


@dp.message(F.text == "❌ Скасувати бронювання")
async def start_cancel_booking(message: types.Message, state: FSMContext):
    await state.set_state(Booking.cancel_code)
    await message.answer("Введіть 5-значний номер бронювання для скасування:")

@dp.message(F.text == "📞 Контакти")
async def show_contacts(message: types.Message):
    await message.answer(
        "Контакти:\n"
        "Телефон: <a href='tel:+380989055753'>+380 98 905 57 53</a>\n"
        "Пошта: <a href='mailto:mandry70625@gmail.com'>mandry70625@gmail.com</a>\n"
        "Inst: <a href='https://www.instagram.com/mandry.sup/'>@mandry.sup</a>",
        parse_mode="HTML"
    )

@dp.message(F.text == "📋 Правила користування")
async def show_rules(message: types.Message):
    await message.answer(
        "📋 Правила користування:\n\n"
        "1. Дотримуйтесь безпеки на воді\n"
        "2. Використовуйте рятувальний жилет\n"
        "3. Не перевищуйте дозволену вагу\n"
        "4. Приходьте за 15 хвилин до старту\n"
        "5. Слідуйте інструкціям персоналу\n"
        "6. Повідомте про травми чи поломки"
    )

@dp.message(F.text == "🚨 Краш ліст")
async def show_crash_list(message: types.Message):
    crash_list_text = (
        "🚨 <b>КРАШ ЛІСТ</b> - Ціна відшкодування\n\n"
        "<b>Надувні SUP дошки:</b>\n"
        "• AQUA MARINA MONSTER BT-21 - \n15 500 грн\n"
        "• AQUA MARINA MONSTER BT-23 - \n15 500 грн\n"
        "• AQUA MARINA PURE AIR - 9 100 грн\n\n"
        "<b>Надувні каяки:</b>\n"
        "• Aqua Marina BETTA BE-312 - 12 300 грн\n"
        "• Aqua Marina LAXO LA-380 - 17 700 грн\n\n"
        "<b>Аксесуари:</b>\n"
        "• Весло під сапборд - 2 200 грн\n"
        "• Весло під каяк - 2 800 грн\n"
        "• Плавник під сап-дошку - 600 грн\n"
        "• Плавник до каяка - 700 грн\n"
        "• Сидіння до каяка - 1 200 грн\n"
        "• Гермомішок - 400 грн\n"
        "• Страхувальний лиш для SUP - 650 грн\n\n"
        "<i>У разі пошкодження обладнання до вас буде застосована відповідна компенсація.</i>"
    )
    await message.answer(crash_list_text, parse_mode="HTML")

@dp.message(Booking.cancel_code)
async def process_cancel_booking(message: types.Message, state: FSMContext):
    code = message.text.strip()
    if not (code.isdigit() and len(code) == 5):
        await message.answer("Невірний формат. Введіть саме 5 цифр, наприклад: 04231")
        return

    cleared_count, booking_name = find_and_clear_booking_by_code(code)
    if cleared_count == 0:
        await message.answer("❌ Бронювання з таким номером не знайдено.")
        await state.clear()
        return

    cancel_reminder_task(code)

    cancel_notify = (
        "Скасування бронювання!\n"
        f"Код: {code}\n"
        f"Клієнт: {booking_name if booking_name else 'Невідомо'}\n"
        f"Очищено слотів: {cleared_count}"
    )
    if STAFF_CHAT_ID is not None:
        try:
            await bot.send_message(chat_id=STAFF_CHAT_ID, text=cancel_notify)
        except Exception:
            pass

    await message.answer(f"✅ Бронювання {code} скасовано.")
    await state.clear()


@dp.callback_query(F.data.startswith("rem_yes_"))
async def process_reminder_yes(callback: types.CallbackQuery):
    await callback.message.edit_text("Дякуємо за підтвердження. Чекаємо вас.")
    await callback.answer()


@dp.callback_query(F.data.startswith("rem_no_"))
async def process_reminder_no(callback: types.CallbackQuery):
    code = callback.data.split("_", 2)[2]
    cleared_count, _ = find_and_clear_booking_by_code(code)
    cancel_reminder_task(code)

    if cleared_count == 0:
        await callback.message.edit_text("❌ Бронювання вже неактуальне або не знайдено.")
        await callback.answer()
        return

    if STAFF_CHAT_ID is not None:
        try:
            await bot.send_message(
                chat_id=STAFF_CHAT_ID,
                text=(
                    "Скасування з нагадування!\n"
                    f"Код: {code}\n"
                    "Клієнт натиснув: Ні, передумав"
                ),
            )
        except Exception:
            pass

    await callback.message.edit_text("✅ Бронювання скасовано. Якщо захочете, можете створити нове у головному меню.")
    await callback.answer()

@dp.callback_query(F.data.startswith("date_"))
async def process_date(callback: types.CallbackQuery, state: FSMContext):
    selected_date = callback.data.split("_", 1)[1]
    ws = get_or_create_sheet(selected_date)
    all_values = ws.get_all_values()

    if is_weather_blocked_sheet(all_values):
        await callback.message.edit_text("Прогнозується негода, тому ми зачинені на цю дату🏄‍♂️")
        await state.clear()
        await callback.answer()
        return

    await state.update_data(date=selected_date)

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="1 година", callback_data="dur_1"))
    builder.row(types.InlineKeyboardButton(text="2 години", callback_data="dur_2"))
    #builder.row(types.InlineKeyboardButton(text="Ранковий сплав", callback_data="morning"))

    await callback.message.edit_text("Скільки часу хочете плавати?", reply_markup=builder.as_markup())
    await state.set_state(Booking.duration)
    await callback.answer()

@dp.callback_query(F.data.startswith("dur_"))
async def process_duration(callback: types.CallbackQuery, state: FSMContext):
    duration = int(callback.data.split("_")[1])
    await state.update_data(duration=duration)

    builder = InlineKeyboardBuilder()
    for label, code in EQUIPMENT_OPTIONS:
        builder.row(types.InlineKeyboardButton(text=label, callback_data=f"eq_{code}"))

    await callback.message.edit_text("Оберіть обладнання:", reply_markup=builder.as_markup())
    await state.set_state(Booking.equipment)


@dp.callback_query(F.data == "morning")
async def process_morning(callback: types.CallbackQuery, state: FSMContext):
    # Ensure morning table exists only when user explicitly requests it
    data = await state.get_data()
    selected_date = data.get('date')
    if selected_date:
        try:
            ws = get_or_create_sheet(selected_date)
            ensure_morning_table(ws)
        except Exception:
            pass

    await state.update_data(morning=True)

    builder = InlineKeyboardBuilder()
    for label, code in EQUIPMENT_OPTIONS:
        builder.row(types.InlineKeyboardButton(text=label, callback_data=f"eq_{code}"))

    await callback.message.edit_text("Оберіть обладнання для ранкового сплаву:", reply_markup=builder.as_markup())
    await state.set_state(Booking.equipment)
    await callback.answer()

@dp.callback_query(F.data.startswith("eq_"))
async def process_equip(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(equipment=callback.data.split("_", 1)[1])

    data = await state.get_data()

    # If this is a morning booking flow, present the fixed morning slots
    if data.get('morning'):
        builder = InlineKeyboardBuilder()
        for i, window in enumerate(MORNING_WINDOWS):
            builder.row(types.InlineKeyboardButton(text=window, callback_data=f"mor_tmidx_{i}"))

        await callback.message.edit_text("Оберіть час ранкового сплаву:", reply_markup=builder.as_markup())
        await state.set_state(Booking.time)
        await callback.answer()
        return

    duration = int(data.get('duration', 1))
    selected_date = data.get('date')
    current_time = get_current_time()
    today_str = current_time.strftime("%d.%m")
    now_time = current_time.time()

    ws = get_or_create_sheet(selected_date)
    all_values = ws.get_all_values()
    
    builder = InlineKeyboardBuilder()
    max_start_index = len(TIME_SLOTS) - duration
    visible_slots = 0
    
    preferred_names = EQUIPMENT_COLUMN_GROUPS[data['equipment']]
    target_cols = get_target_columns_for_names(preferred_names)
    
    for start_index in range(max_start_index + 1):
        start_time_str = TIME_SLOTS[start_index].split('-')[0]
        start_time = datetime.strptime(start_time_str, "%H:%M").time()
        if selected_date == today_str and start_time <= now_time:
            continue

        row_idx = start_index + 2
        window_label = build_time_window(start_index, duration)

        # If the whole slot is marked with '-', keep it visible but only for live queue.
        if has_dash_reserved_slots(all_values, row_idx, duration, target_cols):
            builder.row(types.InlineKeyboardButton(text=window_label, callback_data=f"tmidx_{start_index}"))
            visible_slots += 1
            continue

        # Regular online booking availability.
        booking_resolution = resolve_equipment_booking(data['equipment'], all_values, row_idx, duration)
        if booking_resolution:
            builder.row(types.InlineKeyboardButton(text=window_label, callback_data=f"tmidx_{start_index}"))
            visible_slots += 1

    if visible_slots == 0:
        # Check if all non-dash columns are occupied
        all_occupied = False
        for start_index in range(max_start_index + 1):
            start_time_str = TIME_SLOTS[start_index].split('-')[0]
            start_time = datetime.strptime(start_time_str, "%H:%M").time()
            if selected_date == today_str and start_time <= now_time:
                continue
                
            row_idx = start_index + 2
            if are_all_non_dash_columns_occupied(all_values, row_idx, duration, target_cols):
                all_occupied = True
                break
        
        if all_occupied:
            await callback.message.edit_text("На жаль, бронювання вже недоступне — наразі працюємо лише в форматі живої черги.\nБудемо раді бачити вас на сплаві!🏄‍♂️")
        else:
            await callback.message.edit_text("На сьогодні вільні часові слоти вже завершилися. Оберіть іншу дату.")
        await state.clear()
        return

    await callback.message.edit_text("Оберіть час:", reply_markup=builder.as_markup())
    await state.set_state(Booking.time)


@dp.callback_query(F.data.startswith("mor_tmidx_"))
async def process_morning_time(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[2])
    await state.update_data(morning_time_index=idx)
    data = await state.get_data()

    if data.get('equipment', '').startswith("sup_"):
        builder = InlineKeyboardBuilder()
        for value in range(1, 11):
            builder.button(text=str(value), callback_data=f"qty_{value}")
        builder.adjust(5, 5)

        await callback.message.edit_text("Скільки сапів бронюєте?", reply_markup=builder.as_markup())
        await state.set_state(Booking.quantity)
    else:
        await callback.message.edit_text("Введіть ваше Прізвище та Ім'я:")
        await state.set_state(Booking.name)
    await callback.answer()

@dp.callback_query(F.data.startswith("tmidx_"))
async def process_time(callback: types.CallbackQuery, state: FSMContext):
    start_index = int(callback.data.split("_")[1])
    data = await state.get_data()
    duration = int(data.get('duration', 1))
    
    ws = get_or_create_sheet(data['date'])
    all_values = ws.get_all_values()
    
    row_idx = start_index + 2 

    
    if row_idx + duration - 1 > len(TIME_SLOTS) + 1:
        await callback.answer("❌ Для цієї тривалості оберіть раніший старт!", show_alert=True)
        return

    preferred_names = EQUIPMENT_COLUMN_GROUPS[data['equipment']]
    target_cols = get_target_columns_for_names(preferred_names)

    if has_dash_reserved_slots(all_values, row_idx, duration, target_cols):
        await callback.answer("⏳ На цей час доступна лише жива черга", show_alert=True)
        return

    booking_resolution = resolve_equipment_booking(data['equipment'], all_values, row_idx, duration)

    if not booking_resolution:
        await callback.answer("❌ Немає вільних місць на обраний час!", show_alert=True)
    else:
        requested_equipment = data['equipment']
        await state.update_data(
            time_row=row_idx,
            equip_col=booking_resolution["equip_col"],
            duration=duration,
            actual_equipment=booking_resolution["actual_equipment"],
            equipment_note=booking_resolution["note"],
            resolved_equipment=booking_resolution.get("resolved_equipment", requested_equipment),
        )
        if requested_equipment.startswith("sup_"):
            builder = InlineKeyboardBuilder()
            for value in range(1, 11):
                builder.button(text=str(value), callback_data=f"qty_{value}")
            builder.adjust(5, 5)

            await callback.message.edit_text("Скільки сапів бронюєте?", reply_markup=builder.as_markup())
            await state.set_state(Booking.quantity)
        else:
            await callback.message.edit_text("Введіть ваше Прізвище та Ім'я:")
            await state.set_state(Booking.name)


@dp.callback_query(F.data.startswith("qty_"))
async def process_quantity(callback: types.CallbackQuery, state: FSMContext):
    quantity = int(callback.data.split("_")[1])
    if quantity < 1 or quantity > 10:
        await callback.answer("❌ Оберіть кількість від 1 до 10", show_alert=True)
        return

    data = await state.get_data()
    requested_equipment = data.get('resolved_equipment', data.get('equipment'))
    if not requested_equipment or not requested_equipment.startswith("sup_"):
        await callback.answer("❌ Цей крок доступний лише для сапів", show_alert=True)
        return

    ws = get_or_create_sheet(data['date'])
    all_values = ws.get_all_values()
    if data.get('morning'):
        ensure_morning_table(ws)
        all_values = ws.get_all_values()

        header_row_idx = None
        header_row = []
        for r_idx, row in enumerate(all_values):
            if row and len(row) > 0 and isinstance(row[0], str) and row[0].strip().lower().startswith("ранков"):
                header_row_idx = r_idx
                header_row = row
                break

        if header_row_idx is None:
            await callback.answer("❌ Не вдалося знайти ранкову таблицю", show_alert=True)
            return

        morning_index = int(data.get('morning_time_index', 0))
        row_idx = header_row_idx + 1 + morning_index + 1
    else:
        row_idx = int(data.get('time_row', 0))

    duration = int(data.get('duration', 1))
    target_cols = get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS[requested_equipment])
    free_cols = find_free_columns_for_duration(all_values, row_idx, duration, target_cols, quantity)

    if len(free_cols) < quantity:
        await callback.answer("❌ На цей час немає достатньої кількості вільних сапів", show_alert=True)
        return

    await state.update_data(quantity=quantity, equip_cols=free_cols)
    await callback.message.edit_text("Введіть ваше Прізвище та Ім'я:")
    await state.set_state(Booking.name)

@dp.message(Booking.name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(client_name=message.text)

    phone_keyboard = types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="📱 Поділитись номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("Надішліть ваш номер телефону:", reply_markup=phone_keyboard)
    await state.set_state(Booking.phone)


@dp.message(Booking.phone)
async def process_phone(message: types.Message, state: FSMContext):
    if message.contact:
        phone = message.contact.phone_number
        if not phone.startswith("+"):
            phone = "+" + phone
    else:
        await message.answer("Будь ласка, скористайтесь кнопкою нижче щоб поділитись номером.")
        return

    data = await state.get_data()
    client_name = data.get('client_name', '')
    ws = get_or_create_sheet(data['date'])
    duration = int(data.get('duration', 1))
    booking_code = generate_booking_code()
    actual_equipment = data.get('actual_equipment', data['equipment'])
    equipment_note = data.get('equipment_note', '')
    user_chat_id = message.chat.id
    quantity = int(data.get('quantity', 1))

    if is_phone_blacklisted(phone):
        await message.answer("❌ Цей номер телефону є у чорному списку. Бронювання недоступне.")
        await state.clear()
        return

    booking_lines = [f"ID:{booking_code}", client_name, phone]
    if equipment_note:
        booking_lines.append(equipment_note)

    # Morning booking: write into the dedicated morning table block (single cell like main grid)
    if data.get('morning'):
        idx = int(data.get('morning_time_index', 0))
        booking_window = MORNING_WINDOWS[idx]
        actual_equipment = EQUIPMENT_LABELS.get(data.get('equipment'), data.get('equipment'))

        # Ensure morning table exists and fetch values
        ensure_morning_table(ws)
        all_vals = ws.get_all_values()

        # Find morning header row
        header_row_idx = None
        for r_idx, row in enumerate(all_vals):
            if row and len(row) > 0 and isinstance(row[0], str) and row[0].strip().lower().startswith("ранков"):
                header_row_idx = r_idx
                header_row = row
                break

        if header_row_idx is None:
            # fallback: append the morning block and reload
            ensure_morning_table(ws)
            all_vals = ws.get_all_values()
            for r_idx, row in enumerate(all_vals):
                if row and len(row) > 0 and isinstance(row[0], str) and row[0].strip().lower().startswith("ранков"):
                    header_row_idx = r_idx
                    header_row = row
                    break

        morning_row_idx0 = header_row_idx + 1 + idx

        booking_value = "\n".join([f"ID:{booking_code}", client_name, phone] + ([equipment_note] if equipment_note else []))

        if quantity > 1:
            equip_cols = data.get('equip_cols', [])
            if len(equip_cols) < quantity:
                await message.answer("❌ Не вдалося зафіксувати бронювання: недостатньо вільних сапів на цей час.")
                await state.clear()
                return

            for col in equip_cols:
                start_cell = gspread.utils.rowcol_to_a1(morning_row_idx0 + 1, col)
                try:
                    ws.update(start_cell, [[booking_value]])
                except Exception:
                    ws.append_row([booking_window, actual_equipment, f"ID:{booking_code}", client_name, phone])
        else:
            preferred_names = EQUIPMENT_COLUMN_GROUPS.get(data.get('equipment'), [])
            chosen_col_idx = None
            for name in preferred_names:
                if name in header_row:
                    col_idx = header_row.index(name)
                    cell_value = ""
                    if morning_row_idx0 < len(all_vals):
                        row_vals = all_vals[morning_row_idx0]
                        if col_idx < len(row_vals):
                            cell_value = row_vals[col_idx].strip()
                    if not cell_value:
                        chosen_col_idx = col_idx
                        break

            if chosen_col_idx is None and preferred_names:
                for name in preferred_names:
                    if name in header_row:
                        chosen_col_idx = header_row.index(name)
                        break

            if chosen_col_idx is None:
                chosen_col_idx = 1

            write_row = morning_row_idx0 + 1
            write_col = chosen_col_idx + 1
            start_cell = gspread.utils.rowcol_to_a1(write_row, write_col)
            try:
                ws.update(start_cell, [[booking_value]])
            except Exception:
                ws.append_row([booking_window, actual_equipment, f"ID:{booking_code}", client_name, phone])

        duration = 1
    else:
        booking_value = "\n".join(booking_lines)

        if quantity > 1:
            equip_cols = data.get('equip_cols', [])
            if len(equip_cols) < quantity:
                await message.answer("❌ Не вдалося зафіксувати бронювання: недостатньо вільних сапів на цей час.")
                await state.clear()
                return

            for row_offset in range(duration):
                row_idx = data['time_row'] + row_offset
                for col in equip_cols:
                    start_cell = gspread.utils.rowcol_to_a1(row_idx, col)
                    ws.update(start_cell, [[booking_value]])
        else:
            start_cell = gspread.utils.rowcol_to_a1(data['time_row'], data['equip_col'])
            end_cell = gspread.utils.rowcol_to_a1(data['time_row'] + duration - 1, data['equip_col'])
            ws.update(f"{start_cell}:{end_cell}", [[booking_value] for _ in range(duration)])

        start_slot_index = data['time_row'] - 2
        booking_window = build_time_window(start_slot_index, duration)

    schedule_booking_reminder(
        user_chat_id=user_chat_id,
        booking_code=booking_code,
        booking_date=data['date'],
        booking_window=booking_window,
        actual_equipment=actual_equipment,
        equipment_note=equipment_note,
    )

    notify_text = (
        "Нове бронювання!\n"
        f"Код: {booking_code}\n"
        f"Дата: {data['date']}\n"
        f"Час: {booking_window}\n"
        f"Тривалість: {duration} год\n"
        f"Кількість сапів: {quantity}\n"
        f"Обладнання: {actual_equipment} {equipment_note}".rstrip() + "\n"
        f"Клієнт: {client_name}\n"
        f"Телефон: {phone}"
    )
    if STAFF_CHAT_ID is not None:
        try:
            await bot.send_message(chat_id=STAFF_CHAT_ID, text=notify_text)
        except Exception:
            pass

    reply_keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="📚 Забронювати"), types.KeyboardButton(text="❌ Скасувати бронювання")],
            [types.KeyboardButton(text="📋 Правила користування"), types.KeyboardButton(text="🚨 Краш ліст")],
            [types.KeyboardButton(text="🍽️ Меню")],
            [types.KeyboardButton(text="📞 Контакти")]
        ],
        resize_keyboard=True
    )
    await message.answer(
        f"✅ Записано!\n"
        f"Номер бронювання: {booking_code}\n"
        f"Тривалість: {duration} год\n"
        f"Кількість сапів: {quantity}\n"
        f"Час: {booking_window}\n"
        f"Обладнання: {actual_equipment} {equipment_note}".rstrip() + "\n"
        f"Чекаємо вас на воді!",
        reply_markup=reply_keyboard
    )
    await state.clear()

async def main():
    await bot.set_my_commands([
        BotCommand(command="start", description="Bot Restart"),
    ])
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())