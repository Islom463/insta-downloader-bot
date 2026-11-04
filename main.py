import os
import re
import logging
import sqlite3
import asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
import yt_dlp
from aiohttp import web
import urllib.request
import threading

logging.basicConfig(level=logging.INFO)

# --- RENDER UCHUN MINI VEB-SERVER (Uchib ketmasligi uchun) ---
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
        return None  # Yangi foydalanuvchi, tilni tanlatish kerak
    conn.close()
    return row[0]

# --- TARJIMALAR ---
TRANSLATIONS = {
    'uz_lat': {
        'welcome': "👋 Xush kelibsiz!\n\nVideoni yuklab olish uchun havolani yuboring:\n\n• YouTube\n• Instagram\n• TikTok\n• Pinterest",
        'choose_lang': "🌐 Iltimos, interfeys tilini tanlang:\nПожалуйста, выберите язык:",
        'lang_changed': "✅ Til o'zgartirildi: O'zbekcha (Lotin)",
        'downloading': "📥 Yuklab olinmoqda...",
        'error': "❌ Xatolik yuz berdi. Havola yaroqsiz.",
        'btn_save': "📥 Saqlash",
        'btn_audio': "🎵 Qo'shiqni yuklab olish",
        'btn_group': "👉 Guruhga qo'shish",
        'audio_loading': "🎵 Qo'shiqlar qidirilmoqda...",
        'select_song': "🎵 Videodagi musiqa bo'yicha topilgan variantlar (raqamni tanlang):"
    },
    'uz_cyr': {
        'welcome': "👋 Хуш келибсиз!\n\nВидеони юклаб олиш учун ҳаволани юборинг:\n\n• YouTube\n• Instagram\n• TikTok\n• Pinterest",
        'choose_lang': "🌐 Илтимос, тилни танланг:",
        'lang_changed': "✅ Тил ўзгартирилди: Ўзбекча (Кирилл)",
        'downloading': "📥 Юклаб олинмоқда...",
        'error': "❌ Хатолик юз берди.",
        'btn_save': "📥 Сақлаш",
        'btn_audio': "🎵 Қўшиқни юклаб олиш",
        'btn_group': "👉 Гуруҳга қўшиш",
        'audio_loading': "🎵 Қўшиқлар қидирилмоқда...",
        'select_song': "🎵 Мусиқа бўйича топилган вариантлар:"
    },
    'ru': {
        'welcome': "👋 Добро пожаловать!\n\nОтправьте ссылку для скачивания видео:\n\n• YouTube\n• Instagram\n• TikTok\n• Pinterest",
        'choose_lang': "🌐 Пожалуйста, выберите язык:",
        'lang_changed': "✅ Язык изменен: Русский",
        'downloading': "📥 Загрузка...",
        'error': "❌ Произошла ошибка.",
        'btn_save': "📥 Сохранить",
        'btn_audio': "🎵 Скачать песню",
        'btn_group': "👉 Добавить в группу",
        'audio_loading': "🎵 Поиск песен...",
        'select_song': "🎵 Найденные варианты песни:"
    },
    'en': {
        'welcome': "👋 Welcome!\n\nSend a link to download video:\n\n• YouTube\n• Instagram\n• TikTok\n• Pinterest",
        'choose_lang': "🌐 Please select a language:",
        'lang_changed': "✅ Language changed: English",
        'downloading': "📥 Downloading...",
        'error': "❌ An error occurred.",
        'btn_save': "📥 Save",
        'btn_audio': "🎵 Download song",
        'btn_group': "👉 Add to group",
        'audio_loading': "🎵 Searching songs...",
        'select_song': "🎵 Found song options:"
    }
}

def get_text(user_id, key):
    lang = get_user_lang(user_id) or 'uz_lat'
    return TRANSLATIONS.get(lang, TRANSLATIONS['uz_lat']).get(key, "")

def lang_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🇺🇿 O'zbekcha", callback_data="lang:uz_lat")
    builder.button(text="🇺🇿 Ўзбекча", callback_data="lang:uz_cyr")
    builder.button(text="🇷🇺 Русский", callback_data="lang:ru")
    builder.button(text="🇬🇧 English", callback_data="lang:en")
    builder.adjust(2)
    return builder.as_markup()

# --- START VA TIL ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    lang = get_user_lang(user_id, message.from_user.full_name)
    
    # Stiker yuborish (chiroyli kutib olish uchun)
    try:
        await message.answer_sticker("CAACAgIAAxkBAAEOMmhn2v... (yoki istalgan sticker file_id)")
    except Exception:
        pass

    if not lang:
        await message.answer("🌐 Iltimos, interfeys tilini tanlang:\n\nПожалуйста, выберите язык:", reply_markup=lang_keyboard())
    else:
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
    await callback.message.answer(TRANSLATIONS[lang]['welcome'])
    await callback.answer()

# --- QO'SHIQNI QIDIRISH VA VARIANTLARNI CHIQARISH (1, 2, 3) ---
@dp.callback_query(F.data.startswith("dl_audio:"))
async def process_download_audio(callback: types.CallbackQuery):
    url = callback.data.split("dl_audio:")[1]
    user_id = callback.from_user.id
    status_msg = await callback.message.answer(get_text(user_id, 'audio_loading'))
    
    loop = asyncio.get_event_loop()
    os.makedirs("downloads", exist_ok=True)
    
    try:
        # Videodan musiqa nomini aniqlash
        def extract_song_query():
            ydl_opts = {'noplaylist': True, 'skip_download': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # Agar Instagram metadata'da musiqa nomi bo'lsa
                track = info.get('track')
                artist = info.get('artist')
                if track:
                    return f"{artist or ''} {track}".strip()
                return info.get('title', 'popular music')

        query = await loop.run_in_executor(None, extract_song_query)

        # YouTube'dan shu nom bo'yicha TOP 3 ta variantni qidirish
        def search_songs():
            ydl_opts = {
                'format': 'bestaudio/best',
                'default_search': 'ytsearch3',
                'noplaylist': True,
                'extract_flat': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                res = ydl.extract_info(f"ytsearch3:{query}", download=False)
                return res.get('entries', [])

        entries = await loop.run_in_executor(None, search_songs)
        
        if entries:
            builder = InlineKeyboardBuilder()
            text_response = f"{get_text(user_id, 'select_song')}\n\n"
            
            for idx, entry in enumerate(entries[:3], 1):
                song_title = entry.get('title', f'Track {idx}')
                song_url = entry.get('url') or f"https://youtu.be/{entry.get('id')}"
                text_response.w_text = f"{idx}. {song_title}\n"
                # Har bir variant uchun alohida tugma (1, 2, 3)
                builder.button(text=f"🎵 {idx}-qo'shiq", callback_data=f"get_mp3:{entry.get('id')}")
            
            builder.adjust(3) # 3 ta tugmani yonma-yon qo'yish (1, 2, 3)
            await status_msg.edit_text(text_response + f"\n🔍 Qidirildi: <b>{query}</b>", reply_markup=builder.as_markup())
        else:
            await status_msg.edit_text(get_text(user_id, 'error'))

    except Exception as e:
        logging.error(f"Audio qidirish xatosi: {e}")
        await status_msg.edit_text(get_text(user_id, 'error'))
    
    await callback.answer()

# --- TANLANGAN QO'SHIQNI .MP3 QILIB YUBORISH ---
@dp.callback_query(F.data.startswith("get_mp3:"))
async def send_selected_mp3(callback: types.CallbackQuery):
    video_id = callback.data.split("get_mp3:")[1]
    song_url = f"https://youtu.be/{video_id}"
    user_id = callback.from_user.id
    status_msg = await callback.message.answer(get_text(user_id, 'downloading'))

    loop = asyncio.get_event_loop()
    os.makedirs("downloads", exist_ok=True)

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'downloads/%(id)s.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'noplaylist': True,
    }

    try:
        def download_mp3():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(song_url, download=True)
                base, _ = os.path.splitext(ydl.prepare_filename(info))
                return base + ".mp3", info.get('title', 'Audio')

        file_path, title = await loop.run_in_executor(None, download_mp3)
        if os.path.exists(file_path):
            bot_username = (await bot.get_me()).username
            await callback.message.answer_audio(
                types.FSInputFile(file_path),
                caption=f"🎵 {title}\n❤️ @{bot_username} orqali yuklab olindi"
            )
            os.remove(file_path)
        await status_msg.delete()
    except Exception as e:
        logging.error(f"MP3 yuklash xatosi: {e}")
        await status_msg.edit_text(get_text(user_id, 'error'))
    
    await callback.answer()

# --- VIDEO VA RASMLARNI YUKLASH ---
@dp.message(F.text.regexp(r'https?://[^\s]+'))
async def handle_url(message: types.Message):
    user_id = message.from_user.id
    
    # Agar foydalanuvchi hali til tanlamagan bo'lsa
    if not get_user_lang(user_id):
        await message.answer("🌐 Iltimos, avval tilni tanlang:", reply_markup=lang_keyboard())
        return

    url = message.text.strip()
    status_msg = await message.answer(get_text(user_id, 'downloading'))

    loop = asyncio.get_event_loop()
    os.makedirs("downloads", exist_ok=True)

    ydl_opts = {
        'format': 'best',
        'outtmpl': 'downloads/%(id)s.%(ext)s',
        'noplaylist': True,
    }

    try:
        def download_media():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info), info.get('ext')

        file_path, ext = await loop.run_in_executor(None, download_media)
        
        if os.path.exists(file_path):
            bot_username = (await bot.get_me()).username
            
            # Tugmalarni yasash (Skrinshotdagi kabi)
            builder = InlineKeyboardBuilder()
            builder.button(text=get_text(user_id, 'btn_save'), callback_data="saved_ok")
            
            # Instagram bo'lsa "Qo'shiqni yuklab olish" tugmasi qo'shiladi
            if "instagram.com" in url:
                builder.button(text=get_text(user_id, 'btn_audio'), callback_data=f"dl_audio:{url}")
            
            # Guruhga qo'shish tugkmasi (URL tugma)
            builder.button(text=get_text(user_id, 'btn_group'), url=f"https://t.me/{bot_username}?startgroup=true")
            builder.adjust(1) # Har bir qatorda 1 tadan tugma

            caption = f"❤️ @{bot_username} orqali yuklab olindi"

            if ext in ['jpg', 'jpeg', 'png']:
                await message.answer_photo(types.FSInputFile(file_path), caption=caption, reply_markup=builder.as_markup())
            else:
                await message.answer_video(types.FSInputFile(file_path), caption=caption, reply_markup=builder.as_markup())
            
            os.remove(file_path)

        await status_msg.delete()

    except Exception as e:
        logging.error(f"Yuklash xatosi: {e}")
        await status_msg.edit_text(get_text(user_id, 'error'))

@dp.callback_query(F.data == "saved_ok")
async def saved_ok_cb(callback: types.CallbackQuery):
    await callback.answer("✅ Saqlab olindi!", show_alert=False)

async def main():
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
