import sqlite3
import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

BOT_TOKEN = " " #özeltokeni

dp = Dispatcher()

class TaskFlow(StatesGroup):
    waiting_for_task_title = State()
    waiting_for_deadline = State()

class DeleteTaskFlow(StatesGroup):
    waiting_for_task_id = State()

class GPACalc(StatesGroup):
    waiting_for_grades = State()

class ScheduleFlow(StatesGroup):
    waiting_for_course_info = State()

class ToolsFlow(StatesGroup):
    waiting_for_number = State()

def init_db():
    conn = sqlite3.connect("student_assistant.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS Users (user_id INTEGER PRIMARY KEY, telegram_id TEXT UNIQUE, username TEXT)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Tasks (
            task_id INTEGER PRIMARY KEY AUTOINCREMENT, 
            user_id INTEGER, 
            task_title TEXT, 
            deadline TEXT,
            is_completed BOOLEAN DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Schedule (
            schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            course_name TEXT,
            day_of_week TEXT,
            start_time TEXT,
            room_number TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_main_menu():
    kb = [
        [KeyboardButton(text="Yeni Ödev Ekle"), KeyboardButton(text="Ödevlerimi Listele")],
        [KeyboardButton(text="Ödev Sil"), KeyboardButton(text="Ders Programım")],
        [KeyboardButton(text="Not Hesapla"), KeyboardButton(text="Mühendislik Araçları")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(CommandStart())
async def start_command(message: types.Message):
    conn = sqlite3.connect("student_assistant.db")
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO Users (telegram_id, username) VALUES (?, ?)", 
                       (str(message.from_user.id), message.from_user.username))
        conn.commit()
    except: pass
    conn.close()
    
    welcome_text = (
        f"Merhaba {message.from_user.first_name}.\n\n"
        "Akıllı Öğrenci Asistanı sistemine hoş geldin.\n\n"
        "Neler Yapabilirim?\n"
        "- Ödev Takibi: Ödev ekleme, listeleme ve silme işlemleri.\n"
        "- Not Hesaplama: Vize/Final ortalamanı bulur.\n"
        "- Ders Programı: Ders programını listeler ve yönetir.\n"
        "- Mühendislik Araçları: Binary/Hex/Decimal dönüşümü yapar.\n\n"
        "Aşağıdaki menüden bir işlem seçebilirsin."
    )
    await message.answer(welcome_text, reply_markup=get_main_menu())

@dp.message(F.text == "Yeni Ödev Ekle")
async def add_task_start(message: types.Message, state: FSMContext):
    await message.answer("Lütfen ödevin veya projenin başlığını yazın:")
    await state.set_state(TaskFlow.waiting_for_task_title)

@dp.message(TaskFlow.waiting_for_task_title)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(task_title=message.text)
    await message.answer("Teslim tarihini şu formatta girin:\nGG.AA.YYYY SS:DD\n(Örnek: 01.05.2026 23:59)")
    await state.set_state(TaskFlow.waiting_for_deadline)

@dp.message(TaskFlow.waiting_for_deadline)
async def process_deadline(message: types.Message, state: FSMContext):
    user_input = message.text
    try:
        valid_date = datetime.strptime(user_input, "%d.%m.%Y %H:%M")
        data = await state.get_data()
        conn = sqlite3.connect("student_assistant.db")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM Users WHERE telegram_id = ?", (str(message.from_user.id),))
        uid = cursor.fetchone()[0]
        cursor.execute("INSERT INTO Tasks (user_id, task_title, deadline) VALUES (?, ?, ?)", (uid, data['task_title'], valid_date.strftime("%d.%m.%Y %H:%M")))
        conn.commit()
        conn.close()
        await state.clear()
        formatted_date = valid_date.strftime("%d %B %Y saat %H:%M")
        await message.answer(f"Görev kaydedildi.\nBaşlık: {data['task_title']}\nSon Tarih: {formatted_date}", reply_markup=get_main_menu())
    except ValueError:
        await message.answer("Hatalı Tarih Formatı. Lütfen şu şekilde yazın: 01.05.2026 23:59")

@dp.message(F.text == "Ödevlerimi Listele")
async def list_tasks(message: types.Message):
    conn = sqlite3.connect("student_assistant.db")
    cursor = conn.cursor()
    cursor.execute("SELECT task_id, task_title, deadline FROM Tasks JOIN Users ON Tasks.user_id = Users.user_id WHERE Users.telegram_id = ?", (str(message.from_user.id),))
    tasks = cursor.fetchall()
    conn.close()
    if not tasks:
        await message.answer("Bekleyen göreviniz bulunmuyor.")
    else:
        res = "\n".join([f"ID: {t[0]} | {t[1]} - {t[2]}" for t in tasks])
        await message.answer(f"Aktif Ödevleriniz:\n\n{res}")

@dp.message(F.text == "Ödev Sil")
async def delete_task_start(message: types.Message, state: FSMContext):
    conn = sqlite3.connect("student_assistant.db")
    cursor = conn.cursor()
    cursor.execute("SELECT Tasks.task_id, Tasks.task_title FROM Tasks JOIN Users ON Tasks.user_id = Users.user_id WHERE Users.telegram_id = ?", (str(message.from_user.id),))
    tasks = cursor.fetchall()
    conn.close()
    
    if not tasks:
        await message.answer("Silinecek aktif göreviniz bulunmuyor.")
        return

    res = "\n".join([f"ID: {t[0]} | {t[1]}" for t in tasks])
    await message.answer(f"Silmek istediğiniz ödevin ID numarasını yazın:\n\n{res}")
    await state.set_state(DeleteTaskFlow.waiting_for_task_id)

@dp.message(DeleteTaskFlow.waiting_for_task_id)
async def process_delete_task(message: types.Message, state: FSMContext):
    task_id_to_delete = message.text.strip()
    
    if not task_id_to_delete.isdigit():
        await message.answer("Lütfen sadece geçerli bir sayı (ID) girin.", reply_markup=get_main_menu())
        await state.clear()
        return

    conn = sqlite3.connect("student_assistant.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT user_id FROM Users WHERE telegram_id = ?", (str(message.from_user.id),))
    user_row = cursor.fetchone()
    
    if user_row:
        uid = user_row[0]
        cursor.execute("DELETE FROM Tasks WHERE task_id = ? AND user_id = ?", (int(task_id_to_delete), uid))
        
        if cursor.rowcount > 0:
            await message.answer(f"{task_id_to_delete} ID numaralı görev başarıyla silindi.", reply_markup=get_main_menu())
        else:
            await message.answer("Bu ID'ye ait bir göreviniz bulunamadı.", reply_markup=get_main_menu())
        conn.commit()
    conn.close()
    await state.clear()

@dp.message(F.text == "Not Hesapla")
async def gpa_start(message: types.Message, state: FSMContext):
    await message.answer("Vize ve Final notunuzu boşluk bırakarak yazın (Örn: 60 70):")
    await state.set_state(GPACalc.waiting_for_grades)

@dp.message(GPACalc.waiting_for_grades)
async def process_gpa(message: types.Message, state: FSMContext):
    try:
        vize, final = map(int, message.text.split())
        ortalama = (vize * 0.4) + (final * 0.6)
        
        if final < 50:
            durum = "Başarısız (Final barajı altı)"
        elif ortalama >= 50:
            durum = "Başarılı"
        else:
            durum = "Başarısız"
            
        await message.answer(f"Sonuç:\nOrtalama: {ortalama:.2f}\nDurum: {durum}", reply_markup=get_main_menu())
    except:
        await message.answer("Geçerli notlar girin (Örn: 50 80)", reply_markup=get_main_menu())
    await state.clear()

@dp.message(F.text == "Ders Programım")
async def view_schedule(message: types.Message):
    conn = sqlite3.connect("student_assistant.db")
    cursor = conn.cursor()
    cursor.execute("SELECT course_name, day_of_week, start_time, room_number FROM Schedule JOIN Users ON Schedule.user_id = Users.user_id WHERE Users.telegram_id = ?", (str(message.from_user.id),))
    courses = cursor.fetchall()
    conn.close()
    
    if not courses:
        await message.answer("Programınız boş.\nYeni ders eklemek için /ders_ekle komutunu kullanın.")
    else:
        res = "\n".join([f"{c[0]} | {c[1]} {c[2]} | Sınıf: {c[3]}" for c in courses])
        await message.answer(f"Haftalık Ders Programınız:\n\n{res}")

@dp.message(Command("ders_ekle"))
async def add_course_start(message: types.Message, state: FSMContext):
    await message.answer("Ders bilgisini şu formatta girin:\nDers Adı, Gün, Saat, Sınıf\n\n(Örnek: Design Project, Cuma, 14:00, Lab 2)")
    await state.set_state(ScheduleFlow.waiting_for_course_info)

@dp.message(ScheduleFlow.waiting_for_course_info)
async def process_course(message: types.Message, state: FSMContext):
    try:
        parts = [p.strip() for p in message.text.split(",")]
        if len(parts) != 4:
            raise ValueError
        
        conn = sqlite3.connect("student_assistant.db")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM Users WHERE telegram_id = ?", (str(message.from_user.id),))
        uid = cursor.fetchone()[0]
        cursor.execute("INSERT INTO Schedule (user_id, course_name, day_of_week, start_time, room_number) VALUES (?, ?, ?, ?, ?)", (uid, parts[0], parts[1], parts[2], parts[3]))
        conn.commit()
        conn.close()
        
        await message.answer(f"{parts[0]} programa eklendi.")
    except:
        await message.answer("Hatalı format. Lütfen aralara virgül koyarak yazın.")
    await state.clear()

@dp.message(F.text == "Mühendislik Araçları")
async def eng_tools_start(message: types.Message, state: FSMContext):
    await message.answer("Taban Dönüştürücü (Base Converter)\nÇevirmek istediğiniz sayıyı yazın.\n\nÖrnekler:\n- 255 (Decimal)\n- 0b1010 (Binary)\n- 0xff (Hexadecimal)")
    await state.set_state(ToolsFlow.waiting_for_number)

@dp.message(ToolsFlow.waiting_for_number)
async def process_conversion(message: types.Message, state: FSMContext):
    text = message.text.strip().lower()
    try:
        if text.startswith('0b'):
            num = int(text, 2)
        elif text.startswith('0x'):
            num = int(text, 16)
        else:
            num = int(text)
            
        response = (
            f"Çeviri Sonucu:\n\n"
            f"Decimal: {num}\n"
            f"Binary: {bin(num)}\n"
            f"Hex: {hex(num)}"
        )
        await message.answer(response, reply_markup=get_main_menu())
    except:
        await message.answer("Geçersiz sayı formatı.", reply_markup=get_main_menu())
    await state.clear()

async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
