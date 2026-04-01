import asyncio
import gspread
import socket
import random
from google.auth.exceptions import RefreshError

socket.setdefaulttimeout(10)
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = "7750596802:AAHlQ2gCCIxeWSjmu2wk7l6BigSP41sgxUU"
SHEET_NAME = "Мандри бронь"
MENU_URL = "https://mandry-sup.choiceqr.com/menu?fbclid=PAdGRleAQvo3hleHRuA2FlbQIxMQABpyHGzMZ2j68WOIA7gDKCuCqXp30fz7ITK-gRUKSmhmg5-lJYxrIhrYtnu0A0_aem_1Qlu08flvE4mnL3TsIC_pw"
STAFF_CHAT_ID = -1003788282371

COLUMNS = [
    "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий", "Сап білий",
    "Сап червоний", "Сап червоний", "Сап червоний",
    "Сап Оранжевий", "Сап Оранжевий",
    "Каяк двомісний", "Каяк одномісний"
]

TIME_SLOTS = [
    "10:15-11:15", "11:30-12:30", "12:45-13:45", "14:00-15:00",
    "15:15-16:15", "16:30-17:30", "17:45-18:45", "19:00-20:00"
]

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
    gc = gspread.service_account(filename='credentials.json')
    sh = gc.open(SHEET_NAME)
except RefreshError as err:
    raise SystemExit(
        "\nПомилка авторизації Google Service Account: invalid_grant (account not found).\n"
        "Що перевірити:\n"
        "1) У credentials.json поле client_email має існувати в Google Cloud IAM.\n"
        "2) Якщо service account видалений/перейменований - створіть новий ключ JSON.\n"
        "3) Увімкніть Google Drive API та Google Sheets API в тому самому проєкті.\n"
        "4) Відкрийте таблицю для client_email (доступ Editor).\n"
        f"\nДеталі: {err}"
    ) from err

bot = Bot(token=TOKEN)
dp = Dispatcher()

class Booking(StatesGroup):
    date = State()
    duration = State()
    equipment = State()
    time = State()
    name = State()
    phone = State()
    cancel_code = State()


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
    return [index + 1 for index, name in enumerate(COLUMNS) if name in names]


def find_free_column_for_duration(all_values, row_idx: int, duration: int, target_cols: list[int]):
    for col in target_cols:
        is_block_free = True
        for offset in range(duration):
            current_row_idx = row_idx + offset
            current_row_data = all_values[current_row_idx - 1] if current_row_idx - 1 < len(all_values) else []
            if col < len(current_row_data) and current_row_data[col].strip():
                is_block_free = False
                break

        if is_block_free:
            return col + 1

    return None


def build_time_window(start_index: int, duration: int) -> str:
    start_time = TIME_SLOTS[start_index].split('-')[0]
    start_dt = datetime.strptime(start_time, "%H:%M")
    end_time = (start_dt + timedelta(hours=duration)).strftime("%H:%M")
    return f"{start_time}-{end_time}"


def resolve_equipment_booking(requested_equipment: str, all_values, row_idx: int, duration: int):
    preferred_names = EQUIPMENT_COLUMN_GROUPS[requested_equipment]
    preferred_col = find_free_column_for_duration(
        all_values,
        row_idx,
        duration,
        get_target_columns_for_names(preferred_names),
    )
    if preferred_col:
        return {
            "equip_col": preferred_col,
            "actual_equipment": EQUIPMENT_LABELS[requested_equipment],
            "note": "",
        }

    if requested_equipment == "sup_single":
        fallback_col = find_free_column_for_duration(
            all_values,
            row_idx,
            duration,
            get_target_columns_for_names(EQUIPMENT_COLUMN_GROUPS["sup_double"]),
        )
        if fallback_col:
            return {
                "equip_col": fallback_col,
                "actual_equipment": "Сап двомісний",
                "note": "(одна людина)",
            }

    return None

def get_or_create_sheet(date_str):
    try:
        return sh.worksheet(date_str)
    except gspread.exceptions.WorksheetNotFound:
        new_ws = sh.add_worksheet(title=date_str, rows="100", cols="20")
        
        headers = ["ВІКНО"] + COLUMNS
        new_ws.update('A1', [headers])
        
        time_col = [[t] for t in TIME_SLOTS]
        new_ws.update('A2:A9', time_col)
        
        return new_ws

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
    now = datetime.now()
    today = now.strftime("%d.%m")
    tomorrow = (now + timedelta(days=1)).strftime("%d.%m")
    day_after = (now + timedelta(days=2)).strftime("%d.%m")

    builder.row(types.InlineKeyboardButton(text=today, callback_data=f"date_{today}"))
    builder.row(types.InlineKeyboardButton(text=tomorrow, callback_data=f"date_{tomorrow}"))
    builder.row(types.InlineKeyboardButton(text=day_after, callback_data=f"date_{day_after}"))

    await message.answer("Оберіть дату:", reply_markup=builder.as_markup())
    await state.set_state(Booking.date)

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
        "Телефон: <a href='tel:+380123456789'>+380 12 345 67 89</a>\n"
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
    await message.answer(
        "🚨 Краш ліст:\n\n"
        ":"
    )

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

    cancel_notify = (
        "Скасування бронювання!\n"
        f"Код: {code}\n"
        f"Клієнт: {booking_name if booking_name else 'Невідомо'}\n"
        f"Очищено слотів: {cleared_count}"
    )
    try:
        await bot.send_message(chat_id=STAFF_CHAT_ID, text=cancel_notify)
    except Exception:
        pass

    await message.answer(f"✅ Бронювання {code} скасовано.")
    await state.clear()

@dp.callback_query(F.data.startswith("date_"))
async def process_date(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(date=callback.data.split("_")[1])

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="1 година", callback_data="dur_1"))
    builder.row(types.InlineKeyboardButton(text="2 години", callback_data="dur_2"))
    builder.row(types.InlineKeyboardButton(text="3 години", callback_data="dur_3"))

    await callback.message.edit_text("Скільки часу хочете плавати?", reply_markup=builder.as_markup())
    await state.set_state(Booking.duration)

@dp.callback_query(F.data.startswith("dur_"))
async def process_duration(callback: types.CallbackQuery, state: FSMContext):
    duration = int(callback.data.split("_")[1])
    await state.update_data(duration=duration)

    builder = InlineKeyboardBuilder()
    for label, code in EQUIPMENT_OPTIONS:
        builder.row(types.InlineKeyboardButton(text=label, callback_data=f"eq_{code}"))

    await callback.message.edit_text("Оберіть обладнання:", reply_markup=builder.as_markup())
    await state.set_state(Booking.equipment)

@dp.callback_query(F.data.startswith("eq_"))
async def process_equip(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(equipment=callback.data.split("_", 1)[1])

    data = await state.get_data()
    duration = int(data.get('duration', 1))
    selected_date = data.get('date')
    today_str = datetime.now().strftime("%d.%m")
    now_time = datetime.now().time()

    builder = InlineKeyboardBuilder()
    max_start_index = len(TIME_SLOTS) - duration
    available_slots = 0
    for start_index in range(max_start_index + 1):
        start_time_str = TIME_SLOTS[start_index].split('-')[0]
        start_time = datetime.strptime(start_time_str, "%H:%M").time()
        if selected_date == today_str and start_time <= now_time:
            continue

        window_label = build_time_window(start_index, duration)
        builder.row(types.InlineKeyboardButton(text=window_label, callback_data=f"tmidx_{start_index}"))
        available_slots += 1

    if available_slots == 0:
        await callback.message.edit_text("На сьогодні вільні часові слоти вже завершилися. Оберіть іншу дату.")
        await state.clear()
        return

    await callback.message.edit_text("Оберіть час:", reply_markup=builder.as_markup())
    await state.set_state(Booking.time)

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

    booking_resolution = resolve_equipment_booking(data['equipment'], all_values, row_idx, duration)

    if not booking_resolution:
        await callback.answer("❌ Немає вільних місць на обраний час!", show_alert=True)
    else:
        await state.update_data(
            time_row=row_idx,
            equip_col=booking_resolution["equip_col"],
            duration=duration,
            actual_equipment=booking_resolution["actual_equipment"],
            equipment_note=booking_resolution["note"],
        )
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

    booking_lines = [f"ID:{booking_code}", client_name, phone]
    if equipment_note:
        booking_lines.append(equipment_note)
    booking_value = "\n".join(booking_lines)

    start_cell = gspread.utils.rowcol_to_a1(data['time_row'], data['equip_col'])
    end_cell = gspread.utils.rowcol_to_a1(data['time_row'] + duration - 1, data['equip_col'])
    ws.update(f"{start_cell}:{end_cell}", [[booking_value] for _ in range(duration)])

    start_slot_index = data['time_row'] - 2
    booking_window = build_time_window(start_slot_index, duration)

    notify_text = (
        "Нове бронювання!\n"
        f"Код: {booking_code}\n"
        f"Дата: {data['date']}\n"
        f"Час: {booking_window}\n"
        f"Тривалість: {duration} год\n"
        f"Обладнання: {actual_equipment} {equipment_note}".rstrip() + "\n"
        f"Клієнт: {client_name}\n"
        f"Телефон: {phone}"
    )
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
        f"Час: {booking_window}\n"
        f"Обладнання: {actual_equipment} {equipment_note}".rstrip() + "\n"
        f"Чекаємо вас на воді!",
        reply_markup=reply_keyboard
    )
    await state.clear()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())