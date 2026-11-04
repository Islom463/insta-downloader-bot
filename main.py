import os
import re
import logging
import sqlite3
import asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
import yt_dlp
from aiohttp import web
import urllib.request
import threading

logging.basicConfig(level=logging.INFO)

# --- RENDER UCHUN MINI VEB-SERVER (O'chib qolmasligi uchun) ---
async def start_web_server():
    app = web.Application()
    app.add_routes([
        web.get('/', lambda r: web.Response(text="Bot is running!")),
        web.get('/ping', lambda r: web.Response(text="pong"))
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

def self_ping():
    while True:
        time.sleep(300)
        try:
            app_url = os.environ.get("RENDER_EXTERNAL_URL")
            if not app_url:
                port = int(os.environ.get("PORT", 8080))
                app_url = f"http://127.0.0.1:{port}"
            urllib.request.urlopen(app_url, timeout=20)
        except Exception:
            pass

threading.Thread(target=self_ping, daemon=True).start()

# --- BOT TOKEN ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- BAZA SOZLAMALARI ---
def init_db():
    conn = sqlite3.connect("downloader_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            lang TEXT DEFAULT 'uz_lat'
        )
    """)
    conn.commit()
    conn.close()

init_db()

def set_user_lang(user_id, lang):
    conn = sqlite3.connect("downloader_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))
    conn.commit()
    conn.close()

def get_user_lang(user_id, full_name="User"):
    conn = sqlite3.connect("downloader_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT lang FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT OR REPLACE INTO users (user_id, full_name, lang) VALUES (?, ?, ?)", (user_id, full_name, 'uz_lat'))
        conn.commit()
        conn.close()
        return 'uz_lat'
    conn.close()
    return row[0]

# --- TARJIMALAR (4 xil til) ---
TRANSLATIONS = {
    'uz_lat': {
        'welcome': "👋 Xush kelibsiz!\n\nMenga quyidagi platformalardan havola yuboring:\n\n• YouTube\n• Instagram (Video va Qo'shiq)\n• TikTok\n• Pinterest (Rasm va Video)\n• Facebook / Likee / Twitter",
        'choose_lang': "🌐 Iltimos, tilni tanlang:",
        'lang_changed': "✅ Til o'zgartirildi: O'zbekcha (Lotin)",
        'downloading': "📥 Yuklab olinmoqda, iltimos kuting...",
        'error': "❌ Xatolik yuz berdi. Havola yaroqsiz yoki media topilmadi."
    },
    'uz_cyr': {
        'welcome': "👋 Хуш келибсиз!\n\nМенга қуйидаги платформалардан ҳавола юборинг:\n\n• YouTube\n• Instagram (Видео ва Қўшиқ)\n• TikTok\n• Pinterest (Расм ва Видео)\n• Facebook / Likee / Twitter",
        'choose_lang': "🌐 Илтимос, тилни танланг:",
        'lang_changed': "✅ Тил ўзгартирилди: Ўзбекча (Кирилл)",
        'downloading': "📥 Юклаб олинмоқда, илтимос кутинг...",
        'error': "❌ Хатолик юз берди. Ҳавола яроқсиз ёки медиа топилмади."
    },
    'ru': {
        'welcome': "👋 Добро пожаловать!\n\nОтправьте мне ссылку с:\n\n• YouTube\n• Instagram (Видео и Аудио)\n• TikTok\n• Pinterest (Фото и Видео)\n• Facebook / Likee / Twitter",
        'choose_lang': "🌐 Пожалуйста, выберите язык:",
        'lang_changed': "✅ Язык изменен: Русский",
        'downloading': "📥 Загрузка, пожалуйста, подождите...",
        'error': "❌ Произошла ошибка. Ссылка неверна или медиа не найдено."
    },
    'en': {
        'welcome': "👋 Welcome!\n\nSend me a link from:\n\n• YouTube\n• Instagram (Video & Audio)\n• TikTok\n• Pinterest (Photo & Video)\n• Facebook / Likee / Twitter",
        'choose_lang': "🌐 Please select a language:",
        'lang_changed': "✅ Language changed: English",
        'downloading': "📥 Downloading, please wait...",
        'error': "❌ An error occurred. Invalid link or media not found."
    }
}

def get_text(user_id, key):
    lang = get_user_lang(user_id)
    return TRANSLATIONS.get(lang, TRANSLATIONS['uz_lat']).get(key, "")

# --- KLAVIATURA ---
def lang_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🇺🇿 O'zbekcha", callback_data="lang:uz_lat")
    builder.button(text="🇺🇿 Ўзбекча", callback_data="lang:uz_cyr")
    builder.button(text="🇷🇺 Русский", callback_data="lang:ru")
    builder.button(text="🇬🇧 English", callback_data="lang:en")
    builder.adjust(2)
    return builder.as_markup()

# --- KOMANDALAR ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    get_user_lang(user_id, message.from_user.full_name)
    await message.answer(get_text(user_id, 'welcome'))

@dp.message(Command("lang"))
async def cmd_lang(message: types.Message):
    user_id = message.from_user.id
    await message.answer(get_text(user_id, 'choose_lang'), reply_markup=lang_keyboard())

@dp.callback_query(F.data.startswith("lang:"))
async def process_lang(callback: types.CallbackQuery):
    lang = callback.data.split(":")[1]
    user_id = callback.from_user.id
    set_user_lang(user_id, lang)
    await callback.message.edit_text(TRANSLATIONS[lang]['lang_changed'])
    await callback.answer()

# --- HAVolalarni QABUL QILISH VA YUKLASH ---
@dp.message(F.text.regexp(r'https?://[^\s]+'))
async def handle_url(message: types.Message):
    url = message.text.strip()
    user_id = message.from_user.id
    status_msg = await message.answer(get_text(user_id, 'downloading'))

    loop = asyncio.get_event_loop()
    os.makedirs("downloads", exist_ok=True)

    ydl_opts_video = {
        'format': 'best',
        'outtmpl': 'downloads/%(id)s.%(ext)s',
        'noplaylist': True,
    }

    ydl_opts_audio = {
        'format': 'bestaudio/best',
        'outtmpl': 'downloads/%(id)s_audio.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'noplaylist': True,
    }

    try:
        # 1. Instagram uchun ham video, ham musiqani (audio) tekshiramiz
        if "instagram.com" in url:
            def download_insta():
                with yt_dlp.YoutubeDL(ydl_opts_video) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return ydl.prepare_filename(info), info.get('ext')

            file_path, ext = await loop.run_in_executor(None, download_insta)
            
            if os.path.exists(file_path):
                if ext in ['jpg', 'jpeg', 'png']:
                    await message.answer_photo(types.FSInputFile(file_path))
                else:
                    await message.answer_video(types.FSInputFile(file_path))
                os.remove(file_path)

            # Instagram audiosi (qo'shig'i) bo'lsa uni ham tortamiz
            try:
                def download_insta_audio():
                    with yt_dlp.YoutubeDL(ydl_opts_audio) as ydl:
                        info = ydl.extract_info(url, download=True)
                        base, _ = os.path.splitext(ydl.prepare_filename(info))
                        return base + ".mp3"

                audio_path = await loop.run_in_executor(None, download_insta_audio)
                if os.path.exists(audio_path):
                    await message.answer_audio(types.FSInputFile(audio_path), caption="🎵 Topilgan qo'shiq")
                    os.remove(audio_path)
            except Exception:
                pass

        # 2. Pinterest uchun rasm yoki video
        elif "pinterest.com" in url or "pin.it" in url:
            def download_pin():
                with yt_dlp.YoutubeDL(ydl_opts_video) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return ydl.prepare_filename(info), info.get('ext')

            file_path, ext = await loop.run_in_executor(None, download_pin)
            if os.path.exists(file_path):
                if ext in ['jpg', 'jpeg', 'png']:
                    await message.answer_photo(types.FSInputFile(file_path))
                else:
                    await message.answer_video(types.FSInputFile(file_path))
                os.remove(file_path)

        # 3. YouTube, TikTok, Facebook va boshqa videolar uchun
        else:
            def download_general():
                with yt_dlp.YoutubeDL(ydl_opts_video) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return ydl.prepare_filename(info)

            file_path = await loop.run_in_executor(None, download_general)
            if os.path.exists(file_path):
                await message.answer_video(types.FSInputFile(file_path))
                os.remove(file_path)

        await status_msg.delete()

    except Exception as e:
        logging.error(f"Yuklash xatosi: {e}")
        await status_msg.edit_text(get_text(user_id, 'error'))

async def main():
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
