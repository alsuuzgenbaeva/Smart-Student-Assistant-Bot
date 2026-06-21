from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Çevresel değişkenlerin (Env variables) yüklenmesi
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "student_assistant.db")

# Geçerli günler ve dönüştürme sözlüğü
VALID_DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
WEEKDAY_INDEX = {day.lower(): i for i, day in enumerate(VALID_DAYS)}
DAY_ALIASES = {
    "pazartesi": "Pazartesi", "sali": "Salı", "salı": "Salı", 
    "carsamba": "Çarşamba", "çarşamba": "Çarşamba", "persembe": "Perşembe", 
    "perşembe": "Perşembe", "cuma": "Cuma", "cumartesi": "Cumartesi", "pazar": "Pazar"
}

def normalize_day_name(raw: str) -> Optional[str]:
    return DAY_ALIASES.get(raw.strip().lower())

# Durum (State) Yönetimi Sınıfları
class TaskFlow(StatesGroup):
    waiting_for_task_title = State()
    waiting_for_deadline = State()

class GPACalc(StatesGroup):
    waiting_for_grades = State()

class ScheduleFlow(StatesGroup):
    waiting_for_course_info = State()

class ToolsFlow(StatesGroup):
    waiting_for_number = State()

# Esnek Tarih Algılama Fonksiyonu (Regex)
RELATIVE_DAY_PATTERN = re.compile(r"(\d+)\s*g[uü]n\s*sonra")
RELATIVE_WEEK_PATTERN = re.compile(r"(\d+)\s*hafta\s*sonra")

# Saat belirtilmeden sadece günün bir bölümü söylenirse kullanılacak varsayılan saatler.
TIME_OF_DAY_DEFAULTS = [
    (("sabah",), (9, 0)),
    (("öğlen", "oglen", "öğle", "ogle"), (13, 0)),
    (("akşam", "aksam"), (20, 0)),
    (("gece",), (23, 0)),
]

def _lookup_time_of_day(tokens: list[str]) -> Optional[tuple[int, int]]:
    for token in tokens:
        for roots, value in TIME_OF_DAY_DEFAULTS:
            if any(token.startswith(root) for root in roots):
                return value
    return None

def parse_flexible_date(user_input: str) -> datetime:
    raw = user_input.strip()
    lowered = raw.lower()
    now = datetime.now()

    try:
        return datetime.strptime(raw, "%d.%m.%Y %H:%M")
    except ValueError:
        pass

    tokens = re.findall(r'[a-zçğıöşü]+', lowered)
    target_date = now
    is_weekday_match = False
    used_relative_offset = False

    week_match = RELATIVE_WEEK_PATTERN.search(lowered)
    day_match = RELATIVE_DAY_PATTERN.search(lowered)

    if week_match:
        target_date = now + timedelta(weeks=int(week_match.group(1)))
        used_relative_offset = True
    elif day_match:
        target_date = now + timedelta(days=int(day_match.group(1)))
        used_relative_offset = True
    elif "yarın" in tokens or "yarin" in tokens:
        target_date = now + timedelta(days=1)
    elif "bugün" in tokens or "bugun" in tokens:
        target_date = now
    else:
        for token in tokens:
            normalized = normalize_day_name(token)
            if normalized:
                days_ahead = (WEEKDAY_INDEX[normalized.lower()] - now.weekday()) % 7
                target_date = now + timedelta(days=days_ahead)
                is_weekday_match = True
                break

    time_match = re.search(r'(\d{1,2}):(\d{2})', raw)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
    elif used_relative_offset:
        # "3 gün sonra" gibi saat verilmeyen ifadelerde günün sonu (23:59) varsayılır.
        hour, minute = 23, 59
    else:
        hour_match = re.search(r'(?<!\d)(\d{1,2})(?!\d)', raw)
        if hour_match:
            hour = int(hour_match.group(1))
            minute = 0
            if ("akşam" in lowered or "aksam" in lowered) and hour < 12:
                hour += 12
        else:
            default = _lookup_time_of_day(tokens)
            if default is None:
                raise ValueError("Saat bilgisi bulunamadi")
            hour, minute = default

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Gecersiz saat degeri")

    result = target_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if result <= now:
        result += timedelta(days=7) if is_weekday_match else timedelta(days=1)
    return result

# Asenkron veritabanı yöneticisi
class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._create_tables()

    async def close(self):
        if self._conn is not None:
            await self._conn.close()

    # Tabloların oluşturulması (Many-to-Many ilişkisi ile)
    async def _create_tables(self):
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS Users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT, 
                telegram_id TEXT UNIQUE NOT NULL, 
                username TEXT, 
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS Courses (
                course_id INTEGER PRIMARY KEY AUTOINCREMENT, 
                course_name TEXT UNIQUE NOT NULL
            )
        """)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS User_Courses (
                user_course_id INTEGER PRIMARY KEY AUTOINCREMENT, 
                user_id INTEGER NOT NULL, 
                course_id INTEGER NOT NULL, 
                day_of_week TEXT NOT NULL, 
                start_time TEXT NOT NULL, 
                room_number TEXT, 
                FOREIGN KEY (user_id) REFERENCES Users(user_id) ON DELETE CASCADE, 
                FOREIGN KEY (course_id) REFERENCES Courses(course_id) ON DELETE CASCADE
            )
        """)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS Tasks (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT, 
                user_id INTEGER NOT NULL, 
                task_title TEXT NOT NULL, 
                deadline TEXT NOT NULL, 
                is_completed BOOLEAN DEFAULT 0, 
                FOREIGN KEY (user_id) REFERENCES Users(user_id) ON DELETE CASCADE
            )
        """)
        await self._conn.commit()

    async def register_user(self, telegram_id: str, username: Optional[str]):
        try:
            await self._conn.execute("INSERT INTO Users (telegram_id, username) VALUES (?, ?)", (telegram_id, username))
            await self._conn.commit()
        except aiosqlite.IntegrityError:
            pass

    async def get_user_id(self, telegram_id: str) -> Optional[int]:
        cursor = await self._conn.execute("SELECT user_id FROM Users WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()
        return row[0] if row else None

    async def get_or_create_user_id(self, telegram_id: str, username: Optional[str]) -> int:
        await self.register_user(telegram_id, username)
        return await self.get_user_id(telegram_id)

    async def get_all_users(self):
        cursor = await self._conn.execute("SELECT user_id, telegram_id FROM Users")
        return await cursor.fetchall()

    async def add_task(self, user_id: int, title: str, deadline: str):
        await self._conn.execute(
            "INSERT INTO Tasks (user_id, task_title, deadline) VALUES (?, ?, ?)",
            (user_id, title, deadline),
        )
        await self._conn.commit()

    async def get_tasks_for_telegram_id(self, telegram_id: str):
        cursor = await self._conn.execute(
            """
            SELECT Tasks.task_id, Tasks.task_title, Tasks.deadline, Tasks.is_completed
            FROM Tasks
            JOIN Users ON Tasks.user_id = Users.user_id
            WHERE Users.telegram_id = ?
            ORDER BY Tasks.deadline
            """,
            (telegram_id,),
        )
        return await cursor.fetchall()

    async def get_task_stats(self, user_id: int):
        cursor = await self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(is_completed), 0) FROM Tasks WHERE user_id = ?",
            (user_id,),
        )
        total, completed = await cursor.fetchone()
        return total or 0, completed or 0

    # Güvenlik kontrolü: Görev bu kullanıcıya mı ait?
    async def task_belongs_to_user(self, task_id: int, telegram_id: str) -> bool:
        cursor = await self._conn.execute("SELECT 1 FROM Tasks JOIN Users ON Tasks.user_id = Users.user_id WHERE Tasks.task_id = ? AND Users.telegram_id = ?", (task_id, telegram_id))
        return await cursor.fetchone() is not None

    async def delete_task(self, task_id: int):
        await self._conn.execute("DELETE FROM Tasks WHERE task_id = ?", (task_id,))
        await self._conn.commit()

    async def mark_task_completed(self, task_id: int):
        await self._conn.execute("UPDATE Tasks SET is_completed = 1 WHERE task_id = ?", (task_id,))
        await self._conn.commit()

    async def get_pending_tasks_with_telegram_id(self):
        cursor = await self._conn.execute("SELECT Tasks.task_title, Tasks.deadline, Users.telegram_id FROM Tasks JOIN Users ON Tasks.user_id = Users.user_id WHERE Tasks.is_completed = 0")
        return await cursor.fetchall()

    async def get_pending_tasks_for_user(self, user_id: int):
        cursor = await self._conn.execute("SELECT task_title, deadline FROM Tasks WHERE user_id = ? AND is_completed = 0", (user_id,))
        return await cursor.fetchall()

    async def get_or_create_course(self, course_name: str) -> int:
        cursor = await self._conn.execute("SELECT course_id FROM Courses WHERE course_name = ? COLLATE NOCASE", (course_name,))
        row = await cursor.fetchone()
        if row:
            return row[0]
        cursor = await self._conn.execute("INSERT INTO Courses (course_name) VALUES (?)", (course_name,))
        await self._conn.commit()
        return cursor.lastrowid

    async def add_schedule_entry(self, user_id: int, course_id: int, day_of_week: str, start_time: str, room_number: str):
        await self._conn.execute("INSERT INTO User_Courses (user_id, course_id, day_of_week, start_time, room_number) VALUES (?, ?, ?, ?, ?)", (user_id, course_id, day_of_week, start_time, room_number))
        await self._conn.commit()

    async def get_schedule_for_telegram_id(self, telegram_id: str):
        cursor = await self._conn.execute("SELECT Courses.course_name, User_Courses.day_of_week, User_Courses.start_time, User_Courses.room_number FROM User_Courses JOIN Users ON User_Courses.user_id = Users.user_id JOIN Courses ON User_Courses.course_id = Courses.course_id WHERE Users.telegram_id = ? ORDER BY User_Courses.day_of_week, User_Courses.start_time", (telegram_id,))
        return await cursor.fetchall()

    async def get_today_schedule(self, user_id: int, day_name: str):
        cursor = await self._conn.execute("SELECT Courses.course_name, User_Courses.start_time, User_Courses.room_number FROM User_Courses JOIN Courses ON User_Courses.course_id = Courses.course_id WHERE User_Courses.user_id = ? AND User_Courses.day_of_week = ?", (user_id, day_name))
        return await cursor.fetchall()

db = DatabaseManager(DB_PATH)

# Otomatik bildirim motoru
class NotificationEngine:
    def __init__(self, bot: Bot, db_manager: DatabaseManager):
        self.bot = bot
        self.db = db_manager

    # Teslim tarihine yaklaşan ödevleri kontrol eder (1 ve 24 saat uyarısı)
    async def check_deadlines(self):
        now = datetime.now()
        tasks = await self.db.get_pending_tasks_with_telegram_id()
        for title, deadline_str, telegram_id in tasks:
            try:
                deadline = datetime.strptime(deadline_str, "%d.%m.%Y %H:%M")
            except ValueError:
                continue
            time_diff = deadline - now
            message_text = None
            if timedelta(hours=23) <= time_diff <= timedelta(hours=24):
                message_text = f"⏳ Hatırlatma: '{title}' adlı görevinizin teslimine 24 saatten az kaldı!"
            elif timedelta(0) < time_diff <= timedelta(hours=1):
                message_text = f"🚨 ACİL: '{title}' adlı görevinizin teslimine 1 saatten az kaldı!"
            if message_text is None:
                continue
            try:
                await self.bot.send_message(telegram_id, message_text)
            except Exception:
                pass

    async def _build_summary_body(self, user_id: int, day_name: str) -> Optional[str]:
        """Bir kullanıcı için günün ders ve görev özetini metin olarak üretir.
        Hiçbir şey yoksa None döner. Hem otomatik sabah özeti hem de /bugun
        komutu bu fonksiyonu paylaşır."""
        courses = await self.db.get_today_schedule(user_id, day_name)
        tasks = await self.db.get_pending_tasks_for_user(user_id)
        if not courses and not tasks:
            return None

        body = ""
        if courses:
            body += "📚 Bugünkü dersleriniz:\n"
            for c_name, c_time, c_room in courses:
                body += f"- {c_name} ({c_time} | Sınıf: {c_room})\n"
            body += "\n"
        if tasks:
            body += "📝 Bekleyen ödevleriniz:\n"
            for t_title, t_deadline in tasks:
                body += f"- {t_title} (Son Teslim: {t_deadline})\n"
        return body

    # Sabah 08:00'de gönderilen günlük ders ve ödev özeti
    async def send_daily_summary(self):
        today_str = VALID_DAYS[datetime.now().weekday()]
        users = await self.db.get_all_users()
        for uid, tid in users:
            body = await self._build_summary_body(uid, today_str)
            if body is None:
                continue
            try:
                await self.bot.send_message(tid, "🌅 Günaydın! İşte bugünün özeti:\n\n" + body)
            except Exception:
                pass

    # Kullanıcının istediği an "/bugun" ile çağırabileceği özet
    async def get_summary_for_user(self, user_id: int, day_name: str) -> str:
        body = await self._build_summary_body(user_id, day_name)
        if body is None:
            return "Bugün için planlanmış ders veya bekleyen göreviniz yok. 🎉"
        return "📋 Bugünün Özeti:\n\n" + body

# Klavyeler (Kullanıcı Menüleri)
def get_main_menu() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="Yeni Ödev Ekle"), KeyboardButton(text="Ödevlerimi Listele")],
        [KeyboardButton(text="Ders Programım"), KeyboardButton(text="Not Hesapla")],
        [KeyboardButton(text="Mühendislik Araçları")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_task_keyboard(task_id: int) -> InlineKeyboardMarkup:
    button_completed = InlineKeyboardButton(text="✅ Tamamlandı", callback_data=f"complete_{task_id}")
    button_delete = InlineKeyboardButton(text="🗑️ Sil", callback_data=f"delete_{task_id}")
    return InlineKeyboardMarkup(inline_keyboard=[[button_completed, button_delete]])

EMPTY_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[])

# Router Tanımlamaları (Modüler Mimari)
main_router = Router()
tasks_router = Router()
grades_router = Router()
schedule_router = Router()
tools_router = Router()

# Ana Karşılama Menüsü
notifier: Optional["NotificationEngine"] = None


@main_router.message(CommandStart())
async def start_command(message: types.Message):
    await db.get_or_create_user_id(str(message.from_user.id), message.from_user.username)
    welcome_text = (
        f"Merhaba {message.from_user.first_name}.\n\n"
        "Akıllı Öğrenci Asistanı sistemine hoş geldin.\n\n"
        "Neler Yapabilirim?\n"
        "- Ödev Takibi: Ödev ekleme, listeleme ve silme işlemleri.\n"
        "- Not Hesaplama: Vize/Final ortalamanı bulur.\n"
        "- Ders Programı: Ders programını listeler ve yönetir.\n"
        "- Mühendislik Araçları: Binary/Hex/Decimal dönüşümü yapar.\n\n"
        "Komutlar: /bugun (günün özeti), /istatistik (görev istatistikleri), /ders_ekle\n\n"
        "Aşağıdaki menüden bir işlem seçebilirsin."
    )
    await message.answer(welcome_text, reply_markup=get_main_menu())


@main_router.message(Command("bugun"))
async def today_summary_command(message: types.Message):
    telegram_id = str(message.from_user.id)
    user_id = await db.get_or_create_user_id(telegram_id, message.from_user.username)
    today_str = VALID_DAYS[datetime.now().weekday()]
    text = await notifier.get_summary_for_user(user_id, today_str)
    await message.answer(text)

# Ödev işlemleri
@tasks_router.message(F.text == "Yeni Ödev Ekle")
async def add_task_start(message: types.Message, state: FSMContext):
    await message.answer("Lütfen ödevin veya projenin başlığını yazın:")
    await state.set_state(TaskFlow.waiting_for_task_title)

@tasks_router.message(TaskFlow.waiting_for_task_title)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(task_title=message.text)
    await message.answer(
        "Teslim tarihini şu formatta girin:\nGG.AA.YYYY SS:DD\n(Örnek: 01.05.2026 23:59)\n\n"
        "Veya esnek yazın:\n'Yarın 20:00', '3 gün sonra', 'Cuma sabahı'"
    )
    await state.set_state(TaskFlow.waiting_for_deadline)

@tasks_router.message(TaskFlow.waiting_for_deadline)
async def process_deadline(message: types.Message, state: FSMContext):
    try:
        valid_date = parse_flexible_date(message.text)
    except ValueError:
        await message.answer("Tarih anlaşılamadı. Lütfen 'yarın 20:00', '3 gün sonra', 'cuma 14:00' veya '01.05.2026 23:59' formatında yazın.")
        return
    data = await state.get_data()
    telegram_id = str(message.from_user.id)
    user_id = await db.get_or_create_user_id(telegram_id, message.from_user.username)

    await db.add_task(user_id, data['task_title'], valid_date.strftime("%d.%m.%Y %H:%M"))
    await state.clear()
    formatted_date = valid_date.strftime("%d %B %Y saat %H:%M")
    await message.answer(
        f"Görev kaydedildi.\nBaşlık: {data['task_title']}\nSon Tarih: {formatted_date}",
        reply_markup=get_main_menu(),
    )

@tasks_router.message(F.text == "Ödevlerimi Listele")
async def list_tasks(message: types.Message):
    tasks = await db.get_tasks_for_telegram_id(str(message.from_user.id))
    if not tasks:
        await message.answer("Henüz herhangi bir göreviniz bulunmuyor.")
        return
    await message.answer("Görevleriniz:")
    for task_id, title, deadline, is_completed in tasks:
        status = "✅ Tamamlandı" if is_completed else "⏳ Bekliyor"
        text = f"📌 {title}\n📅 Son Teslim: {deadline}\nDurum: {status}"
        await message.answer(text, reply_markup=get_task_keyboard(task_id))

@tasks_router.message(Command("istatistik"))
async def task_stats_command(message: types.Message):
    telegram_id = str(message.from_user.id)
    user_id = await db.get_or_create_user_id(telegram_id, message.from_user.username)
    total, completed = await db.get_task_stats(user_id)

    if total == 0:
        await message.answer("Henüz hiç göreviniz yok, istatistik gösterilemiyor.")
        return

    pending = total - completed
    rate = (completed / total) * 100
    bar_len = 10
    filled = round(rate / 100 * bar_len)
    bar = "▓" * filled + "░" * (bar_len - filled)

    text = (
        "📊 Görev İstatistikleriniz\n\n"
        f"Toplam görev: {total}\n"
        f"Tamamlanan: {completed}\n"
        f"Bekleyen: {pending}\n"
        f"Tamamlama oranı: %{rate:.0f}\n"
        f"{bar}"
    )
    await message.answer(text)

@tasks_router.callback_query(F.data.startswith("delete_"))
async def process_delete_task_inline(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    telegram_id = str(callback.from_user.id)
    if not await db.task_belongs_to_user(task_id, telegram_id):
        await callback.answer("Bu görev size ait değil veya artık mevcut değil.", show_alert=True)
        return
    await db.delete_task(task_id)
    await callback.message.edit_text("🗑️ Görev başarıyla silindi.", reply_markup=EMPTY_KEYBOARD)
    await callback.answer()

@tasks_router.callback_query(F.data.startswith("complete_"))
async def process_complete_task_inline(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    telegram_id = str(callback.from_user.id)
    if not await db.task_belongs_to_user(task_id, telegram_id):
        await callback.answer("Bu görev size ait değil veya artık mevcut değil.", show_alert=True)
        return
    await db.mark_task_completed(task_id)
    await callback.message.edit_text("✅ Görev tamamlandı olarak işaretlendi.", reply_markup=EMPTY_KEYBOARD)
    await callback.answer()

# Not hesaplama
@grades_router.message(F.text == "Not Hesapla")
async def gpa_start(message: types.Message, state: FSMContext):
    await message.answer("Vize ve Final notunuzu boşluk bırakarak yazın (Örn: 60 70):")
    await state.set_state(GPACalc.waiting_for_grades)

@grades_router.message(GPACalc.waiting_for_grades)
async def process_gpa(message: types.Message, state: FSMContext):
    parts = message.text.split()
    try:
        if len(parts) != 2:
            raise ValueError
        vize, final = float(parts[0]), float(parts[1])
        if not (0 <= vize <= 100 and 0 <= final <= 100):
            raise ValueError
        ortalama = (vize * 0.4) + (final * 0.6)
        if final < 50:
            durum = "Başarısız (Final barajı altı)"
        elif ortalama >= 50:
            durum = "Başarılı"
        else:
            durum = "Başarısız"
        await message.answer(f"Sonuç:\nOrtalama: {ortalama:.2f}\nDurum: {durum}", reply_markup=get_main_menu())
    except ValueError:
        await message.answer("Geçerli iki not girin, 0-100 arası (Örn: 50 80):", reply_markup=get_main_menu())
    await state.clear()

# Ders programı işlemleri
@schedule_router.message(F.text == "Ders Programım")
async def view_schedule(message: types.Message):
    courses = await db.get_schedule_for_telegram_id(str(message.from_user.id))
    if not courses:
        await message.answer("Programınız boş.\nYeni ders eklemek için /ders_ekle komutunu kullanın.")
    else:
        res = "\n".join(f"{c[0]} | {c[1]} {c[2]} | Sınıf: {c[3]}" for c in courses)
        await message.answer(f"Haftalık Ders Programınız:\n\n{res}")

@schedule_router.message(Command("ders_ekle"))
async def add_course_start(message: types.Message, state: FSMContext):
    await message.answer("Ders bilgisini şu formatta girin:\nDers Adı, Gün, Saat, Sınıf\n\n(Örnek: Design Project, Cuma, 14:00, Lab 2)")
    await state.set_state(ScheduleFlow.waiting_for_course_info)

@schedule_router.message(ScheduleFlow.waiting_for_course_info)
async def process_course(message: types.Message, state: FSMContext):
    parts = [p.strip() for p in message.text.split(",")]
    if len(parts) != 4:
        await message.answer("Hatalı format. Lütfen aralara virgül koyarak yazın.")
        await state.clear()
        return
    course_name, day_raw, time_raw, room = parts
    normalized_day = normalize_day_name(day_raw)
    if normalized_day is None:
        await message.answer("Geçersiz gün adı. Lütfen şu günlerden birini kullanın:\n" + ", ".join(VALID_DAYS))
        await state.clear()
        return
    if not re.fullmatch(r'\d{1,2}:\d{2}', time_raw):
        await message.answer("Geçersiz saat formatı. Lütfen SS:DD formatında yazın (örn. 14:00).")
        await state.clear()
        return
    telegram_id = str(message.from_user.id)
    user_id = await db.get_or_create_user_id(telegram_id, message.from_user.username)
    course_id = await db.get_or_create_course(course_name)
    await db.add_schedule_entry(user_id, course_id, normalized_day, time_raw, room)
    await message.answer(f"{course_name} programa eklendi ({normalized_day} {time_raw}).")
    await state.clear()

# Mühendislik araçları (dönüştürücü)
@tools_router.message(F.text == "Mühendislik Araçları")
async def eng_tools_start(message: types.Message, state: FSMContext):
    await message.answer("Taban Dönüştürücü (Base Converter)\nÇevirmek istediğiniz sayıyı yazın.\n\nÖrnekler:\n- 255 (Decimal)\n- 0b1010 (Binary)\n- 0xff (Hexadecimal)")
    await state.set_state(ToolsFlow.waiting_for_number)

@tools_router.message(ToolsFlow.waiting_for_number)
async def process_conversion(message: types.Message, state: FSMContext):
    text = message.text.strip().lower()
    try:
        if text.startswith('0b'):
            num = int(text, 2)
        elif text.startswith('0x'):
            num = int(text, 16)
        else:
            num = int(text)
        response = f"Çeviri Sonucu:\n\nDecimal: {num}\nBinary: {bin(num)}\nHex: {hex(num)}"
        await message.answer(response, reply_markup=get_main_menu())
    except ValueError:
        await message.answer("Geçersiz sayı formatı.", reply_markup=get_main_menu())
    await state.clear()

# Ana döngü (Main)
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN bulunamadı!")
        
    await db.connect()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    
    # Routerların eklenmesi
    dp.include_router(main_router)
    dp.include_router(tasks_router)
    dp.include_router(grades_router)
    dp.include_router(schedule_router)
    dp.include_router(tools_router)
    
    # Arka plan görevleri
    global notifier
    notifier = NotificationEngine(bot, db)
    scheduler = AsyncIOScheduler(timezone="Europe/Istanbul")
    scheduler.add_job(notifier.check_deadlines, 'interval', hours=1)
    scheduler.add_job(notifier.send_daily_summary, 'cron', hour=8, minute=0)
    scheduler.start()
    
    # Webhook kullanılmıyor; bot her zaman polling (uzun anketleme) modunda çalışır.
    try:
        await bot.delete_webhook()
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
