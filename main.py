import sqlite3
import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart

BOT_TOKEN = "" # Token buraya eklenecek

dp = Dispatcher()

def init_db():
    conn = sqlite3.connect("student_assistant.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Users (
            user_id INTEGER PRIMARY KEY,
            telegram_id TEXT UNIQUE NOT NULL,
            username TEXT,
            language_code TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Courses (
            course_id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_code TEXT UNIQUE NOT NULL,
            course_name TEXT NOT NULL
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS User_Courses (
            user_id INTEGER,
            course_id INTEGER,
            PRIMARY KEY (user_id, course_id),
            FOREIGN KEY (user_id) REFERENCES Users(user_id),
            FOREIGN KEY (course_id) REFERENCES Courses(course_id)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS Tasks (
            task_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            course_id INTEGER,
            task_type TEXT,
            task_title TEXT NOT NULL,
            deadline DATETIME NOT NULL,
            is_completed BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES Users(user_id),
            FOREIGN KEY (course_id) REFERENCES Courses(course_id)
        )
    """)
    
    conn.commit()
    conn.close()

@dp.message(CommandStart())
async def start_command(message: types.Message):
    conn = sqlite3.connect("student_assistant.db")
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            "INSERT INTO Users (telegram_id, username, language_code) VALUES (?, ?, ?)",
            (str(message.from_user.id), message.from_user.username, message.from_user.language_code)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()
    
    await message.answer("Sisteme hoş geldiniz. Akıllı Öğrenci Asistanı aktif.")

async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
