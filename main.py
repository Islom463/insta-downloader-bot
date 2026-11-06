"""
Media yuklovchi bot: Instagram, YouTube, TikTok, Pinterest, Facebook, X va boshqalar.
Havola yuborasiz -> video; tugmalar orqali MP3 (shu videodan) yoki qo'shiqni nomi bo'yicha qidirish.
Qo'shiq nomini yozsangiz ham MP3 variantlari chiqadi.

Render Environment:
  BOT_TOKEN       - majburiy
  COOKIES_TXT     - ixtiyoriy, Instagram/YouTube bloklasa: brauzerdan olingan cookies.txt (Netscape) mazmuni
  PYTHON_VERSION  - tavsiya: 3.12.8
"""
import os
import re
import html
import time
import uuid
import shutil
import asyncio
import logging
import sqlite3
import threading
import subprocess
import urllib.request
from collections import OrderedDict
from contextlib import closing

import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
BOT_STATUS = {"text": "Bot ishga tushmoqda..."}

DB_PATH = os.getenv("DB_PATH", "downloader_bot.db")
WORK_ROOT = "downloads"
TG_LIMIT = 49 * 1024 * 1024        # Bot API orqali yuborish chegarasi (50 MB)
MAX_DL = 300 * 1024 * 1024         # diskni to'ldirmaslik uchun
URL_RE = re.compile(r"https?://[^\s<>\"']+")
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


# ---------------- Render uchun veb-server va keep-alive ----------------
async def _root_handler(request):
    return web.Response(text=BOT_STATUS["text"])


async def _ping_handler(request):
    return web.Response(text="pong")


async def start_web_server():
    app = web.Application()
    app.add_routes([web.get("/", _root_handler), web.get("/ping", _ping_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()


def self_ping():
    while True:
        time.sleep(240)
        try:
            url = os.getenv("RENDER_EXTERNAL_URL") or f"http://127.0.0.1:{os.environ.get('PORT', 8080)}"
            urllib.request.urlopen(url, timeout=20).read()
        except Exception:
            pass


threading.Thread(target=self_ping, daemon=True).start()


# ---------------- Token ----------------
def detect_token():
    tok_re = r"\d{6,}:[A-Za-z0-9_-]{30,}"
    env = {k: (v or "").strip().strip('"').strip("'") for k, v in os.environ.items()}
    for k in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN"):
        m = re.search(tok_re, env.get(k, ""))
        if m:
            return m.group(0)
    for v in env.values():
        m = re.search(tok_re, v)
        if m:
            return m.group(0)
    return ""


BOT_TOKEN = detect_token()
if not BOT_TOKEN:
    while True:
        logging.error("BOT_TOKEN topilmadi! Render Environment'ga BOT_TOKEN ni kiriting.")
        time.sleep(30)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ---------------- Token / ulanish diagnostikasi ----------------
class _StatusLogHandler(logging.Handler):
    """aiogram loglaridagi Conflict/Unauthorized xatolarini '/' sahifasida ko'rsatadi."""
    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return
        stamp = time.strftime("%H:%M:%S", time.gmtime())
        if "TelegramConflictError" in msg or "terminated by other getUpdates" in msg:
            BOT_STATUS["text"] = (
                f"XATO (Conflict, {stamp} UTC): bu token boshqa joyda ham ishlayapti. "
                "Eski Render xizmatini, kompyuter yoki boshqa hostingdagi nusxani to'xtating."
            )
        elif "TelegramUnauthorizedError" in msg:
            BOT_STATUS["text"] = f"XATO (Unauthorized, {stamp} UTC): Telegram tokenni qabul qilmadi."


logging.getLogger("aiogram").addHandler(_StatusLogHandler())


async def wait_for_valid_token():
    """Token Telegram'da yaroqli ekanini tekshiradi; xatoni logda va '/' sahifada tushunarli ko'rsatadi."""
    while True:
        try:
            me = await bot.get_me()
            BOT_STATUS["text"] = f"OK: @{me.username} ishlayapti"
            logging.info("Bot ulandi: @%s", me.username)
            return
        except TelegramUnauthorizedError:
            BOT_STATUS["text"] = (
                "XATO: Telegram tokenni qabul qilmadi (Unauthorized). Sabablari: token noto'g'ri nusxalangan yoki "
                "BotFather'da /revoke qilingan. BotFather'dan joriy tokenni oling, Render'da BOT_TOKEN ni yangilab qayta deploy qiling."
            )
            logging.error(BOT_STATUS["text"])
        except Exception as ex:
            BOT_STATUS["text"] = f"Telegramga ulanib bo'lmadi ({type(ex).__name__}). Qayta urinilmoqda..."
            logging.error("%s: %s", BOT_STATUS["text"], ex)
        await asyncio.sleep(60)

SEM = asyncio.Semaphore(2)


# ---------------- ffmpeg va cookies ----------------
def setup_ffmpeg():
    """(ffmpeg_path, dir) — tizimda bo'lmasa imageio-ffmpeg paketidagi binarni ishlatadi."""
    sys_ff = shutil.which("ffmpeg")
    if sys_ff:
        return sys_ff, None
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        logging.warning("ffmpeg topilmadi (%s). requirements.txt ga imageio-ffmpeg qo'shing.", e)
        return None, None
    bindir = os.path.abspath("bin")
    os.makedirs(bindir, exist_ok=True)
    link = os.path.join(bindir, "ffmpeg")
    if not os.path.exists(link):
        try:
            os.symlink(exe, link)
        except OSError:
            shutil.copy2(exe, link)
            os.chmod(link, 0o755)
    os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
    return link, bindir


FFMPEG, FFMPEG_DIR = setup_ffmpeg()


def setup_cookies():
    txt = os.getenv("COOKIES_TXT", "")
    if txt.strip():
        path = os.path.abspath("cookies.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt.replace("\\n", "\n").strip() + "\n")
        return path
    p = os.getenv("COOKIES_FILE", "")
    return p if p and os.path.exists(p) else None


COOKIES_FILE = setup_cookies()
logging.info("ffmpeg=%s cookies=%s", bool(FFMPEG), bool(COOKIES_FILE))


# ---------------- Baza (til) ----------------
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as c:
        c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, full_name TEXT, lang TEXT)")
        c.commit()


init_db()


def get_user_lang(uid):
    with closing(sqlite3.connect(DB_PATH)) as c:
        row = c.execute("SELECT lang FROM users WHERE user_id = ?", (uid,)).fetchone()
    return row[0] if row else None


def set_user_lang(uid, lang, name="User"):
    with closing(sqlite3.connect(DB_PATH)) as c:
        c.execute("INSERT OR REPLACE INTO users (user_id, full_name, lang) VALUES (?, ?, ?)", (uid, name, lang))
        c.commit()


# ---------------- Tarjimalar ----------------
TRANSLATIONS = {
    "uz_lat": {
        "uploading": "📤 Telegramga yuklanmoqda...",
        "welcome": "👋 Xush kelibsiz!\n\nVideoni yuklab olish uchun havolani yuboring:\n\n• YouTube\n• Instagram\n• TikTok\n• Pinterest\n• Facebook, X (Twitter) va boshqalar\n\n🎵 Qo'shiq nomini yozsangiz — MP3 variantlarini topib beraman.",
        "choose_lang": "🌐 Iltimos, interfeys tilini tanlang:\nПожалуйста, выберите язык:",
        "lang_changed": "✅ Til o'zgartirildi: O'zbekcha (Lotin)",
        "downloading": "📥 Yuklab olinmoqda...",
        "error": "❌ Xatolik yuz berdi. Havolani tekshirib, qayta urinib ko'ring.",
        "blocked": "🚫 Sayt serverimizni blokladi yoki kirish (login) talab qilmoqda. Keyinroq urinib ko'ring yoki boshqa havola yuboring.",
        "private": "🔒 Bu video yopiq yoki o'chirilgan.",
        "unsupported": "⚠️ Bu havola qo'llab-quvvatlanmaydi.",
        "too_big": "⚠️ Video juda katta (Telegram botlar uchun 50 MB chegarasi). Qisqaroq video yuboring.",
        "btn_save": "📥 Saqlash",
        "btn_mp3": "🎵 Audio (shu videodan)",
        "btn_find": "🔎 Qo'shiqni topish",
        "btn_group": "👉 Guruhga qo'shish",
        "saved": "✅ Saqlab olindi!",
        "audio_loading": "🎵 Audio tayyorlanmoqda...",
        "searching": "🔎 Qidirilmoqda...",
        "select_song": "🎵 Topilgan variantlar (raqamni tanlang):",
        "not_found": "😕 Hech narsa topilmadi. Qo'shiq nomini yozib yuboring.",
        "no_query": "🎵 Bu videodan qo'shiq nomini aniqlab bo'lmadi. Qo'shiq nomini yozib yuboring.",
        "expired": "⌛ Havola eskirgan. Videoni qayta yuboring.",
        "sent_by": "❤️ @{bot} orqali yuklab olindi",
    },
    "uz_cyr": {
        "uploading": "📤 Telegramга юкланмоқда...",
        "welcome": "👋 Хуш келибсиз!\n\nВидеони юклаб олиш учун ҳаволани юборинг:\n\n• YouTube\n• Instagram\n• TikTok\n• Pinterest\n• Facebook, X (Twitter) ва бошқалар\n\n🎵 Қўшиқ номини ёзсангиз — MP3 вариантларини топиб бераман.",
        "choose_lang": "🌐 Илтимос, тилни танланг:",
        "lang_changed": "✅ Тил ўзгартирилди: Ўзбекча (Кирилл)",
        "downloading": "📥 Юклаб олинмоқда...",
        "error": "❌ Хатолик юз берди. Ҳаволани текшириб, қайта уриниб кўринг.",
        "blocked": "🚫 Сайт серверимизни блоклади ёки кириш (login) талаб қилмоқда. Кейинроқ уриниб кўринг.",
        "private": "🔒 Бу видео ёпиқ ёки ўчирилган.",
        "unsupported": "⚠️ Бу ҳавола қўллаб-қувватланмайди.",
        "too_big": "⚠️ Видео жуда катта (50 MB чегараси). Қисқароқ видео юборинг.",
        "btn_save": "📥 Сақлаш",
        "btn_mp3": "🎵 Аудио (шу видеодан)",
        "btn_find": "🔎 Қўшиқни топиш",
        "btn_group": "👉 Гуруҳга қўшиш",
        "saved": "✅ Сақлаб олинди!",
        "audio_loading": "🎵 Аудио тайёрланмоқда...",
        "searching": "🔎 Қидирилмоқда...",
        "select_song": "🎵 Топилган вариантлар (рақамни танланг):",
        "not_found": "😕 Ҳеч нарса топилмади. Қўшиқ номини ёзиб юборинг.",
        "no_query": "🎵 Бу видеодан қўшиқ номини аниқлаб бўлмади. Қўшиқ номини ёзиб юборинг.",
        "expired": "⌛ Ҳавола эскирган. Видеони қайта юборинг.",
        "sent_by": "❤️ @{bot} орқали юклаб олинди",
    },
    "ru": {
        "uploading": "📤 Отправляю в Telegram...",
        "welcome": "👋 Добро пожаловать!\n\nОтправьте ссылку для скачивания видео:\n\n• YouTube\n• Instagram\n• TikTok\n• Pinterest\n• Facebook, X (Twitter) и др.\n\n🎵 Напишите название песни — найду MP3.",
        "choose_lang": "🌐 Пожалуйста, выберите язык:",
        "lang_changed": "✅ Язык изменен: Русский",
        "downloading": "📥 Загрузка...",
        "error": "❌ Произошла ошибка. Проверьте ссылку и попробуйте снова.",
        "blocked": "🚫 Сайт заблокировал сервер или требует вход. Попробуйте позже.",
        "private": "🔒 Видео закрыто или удалено.",
        "unsupported": "⚠️ Эта ссылка не поддерживается.",
        "too_big": "⚠️ Видео слишком большое (лимит 50 МБ). Отправьте короче.",
        "btn_save": "📥 Сохранить",
        "btn_mp3": "🎵 Аудио (из видео)",
        "btn_find": "🔎 Найти песню",
        "btn_group": "👉 Добавить в группу",
        "saved": "✅ Сохранено!",
        "audio_loading": "🎵 Готовлю аудио...",
        "searching": "🔎 Поиск...",
        "select_song": "🎵 Найденные варианты (выберите номер):",
        "not_found": "😕 Ничего не найдено. Напишите название песни.",
        "no_query": "🎵 Не удалось определить песню. Напишите её название.",
        "expired": "⌛ Ссылка устарела. Отправьте видео снова.",
        "sent_by": "❤️ Скачано через @{bot}",
    },
    "en": {
        "uploading": "📤 Uploading to Telegram...",
        "welcome": "👋 Welcome!\n\nSend a link to download a video:\n\n• YouTube\n• Instagram\n• TikTok\n• Pinterest\n• Facebook, X (Twitter) and more\n\n🎵 Type a song name — I'll find MP3 options.",
        "choose_lang": "🌐 Please select a language:",
        "lang_changed": "✅ Language changed: English",
        "downloading": "📥 Downloading...",
        "error": "❌ An error occurred. Check the link and try again.",
        "blocked": "🚫 The site blocked our server or requires login. Try again later.",
        "private": "🔒 This video is private or removed.",
        "unsupported": "⚠️ This link is not supported.",
        "too_big": "⚠️ Video is too large (50 MB bot limit). Send a shorter one.",
        "btn_save": "📥 Save",
        "btn_mp3": "🎵 Audio (from video)",
        "btn_find": "🔎 Find the song",
        "btn_group": "👉 Add to group",
        "saved": "✅ Saved!",
        "audio_loading": "🎵 Preparing audio...",
        "searching": "🔎 Searching...",
        "select_song": "🎵 Found options (choose a number):",
        "not_found": "😕 Nothing found. Type the song name.",
        "no_query": "🎵 Couldn't detect the song. Type its name.",
        "expired": "⌛ Link expired. Send the video again.",
        "sent_by": "❤️ Downloaded via @{bot}",
    },
}


def T(uid, key):
    lang = get_user_lang(uid) or "uz_lat"
    return TRANSLATIONS.get(lang, TRANSLATIONS["uz_lat"]).get(key) or TRANSLATIONS["uz_lat"].get(key, "")


def lang_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="🇺🇿 O'zbekcha", callback_data="lang:uz_lat")
    b.button(text="🇺🇿 Ўзбекча", callback_data="lang:uz_cyr")
    b.button(text="🇷🇺 Русский", callback_data="lang:ru")
    b.button(text="🇬🇧 English", callback_data="lang:en")
    b.adjust(2)
    return b.as_markup()


# ---------------- yt-dlp qismi (sinxron, thread ichida ishlaydi) ----------------
class TooBigError(Exception):
    pass


def base_opts():
    o = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 25, "retries": 3, "fragment_retries": 3,
        "cachedir": False, "max_filesize": MAX_DL, "concurrent_fragment_downloads": 4,
        "noprogress": True, "no_color": True, "extractor_retries": 2, "http_chunk_size": 10 * 1024 * 1024,
    }
    if COOKIES_FILE:
        o["cookiefile"] = COOKIES_FILE
    if FFMPEG_DIR:
        o["ffmpeg_location"] = FFMPEG_DIR
    return o


MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "720") or 720)
PREFER_HD = os.getenv("VIDEO_HD", "0") == "1"


def video_formats():
    """Tezkor rejim (standart): tayyor (birlashtirilmagan) mp4 oldin. VIDEO_HD=1 bo'lsa sifatliroq, lekin sekinroq."""
    h = MAX_HEIGHT
    prog = f"b[ext=mp4][height<={h}]/b[height<={h}]"
    merged = f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={h}]+ba"
    low = "b[height<=480]/b[height<=360]/w"
    if FFMPEG:
        first = f"{merged}/{prog}/b" if PREFER_HD else f"{prog}/{merged}/b"
        return [first, f"b[height<=480]/bv*[height<=480]+ba/b", "bv*[height<=360]+ba/w"]
    return [f"{prog}/b", low, "w"]


def list_media(workdir):
    out = []
    for name in sorted(os.listdir(workdir)):
        p = os.path.join(workdir, name)
        if os.path.isfile(p) and not name.endswith((".part", ".ytdl", ".temp", ".json")):
            out.append(p)
    return out


def first_entry(info):
    if info and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if entries:
            return entries[0]
    return info or {}


def dl_video(url, workdir):
    """Natija: ([fayl yo'llari], info). 50 MB dan oshsa pastroq sifatga o'tadi."""
    last = None
    for fmt in video_formats():
        opts = base_opts()
        opts.update({"format": fmt, "outtmpl": os.path.join(workdir, "%(id).60s.%(ext)s"), "merge_output_format": "mp4"})
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            low = str(e).lower()
            if ("requested format" in low and "not available" in low) or "larger than max-filesize" in low:
                last = e
                for f in list_media(workdir):
                    os.remove(f)
                continue
            raise
        files = list_media(workdir)
        if not files:
            last = RuntimeError("fayl topilmadi")
            continue
        ok = [f for f in files if os.path.getsize(f) <= TG_LIMIT]
        if ok:
            return ok[:5], first_entry(info)
        for f in files:
            os.remove(f)
        last = TooBigError()
    raise last or TooBigError()


def to_audio(src):
    """m4a/mp3 bo'lsa tegmaydi. Aks holda avval tezkor 'copy' (qayta kodlamasdan), bo'lmasa mp3 128k."""
    ext = os.path.splitext(src)[1].lower()
    if ext in (".mp3", ".m4a") or not FFMPEG:
        return src
    base = os.path.splitext(src)[0]
    attempts = [
        (base + ".m4a", ["-vn", "-c:a", "copy"], 120),
        (base + ".mp3", ["-vn", "-c:a", "libmp3lame", "-b:a", "128k", "-threads", "1"], 300),
    ]
    for dst, args, tmo in attempts:
        try:
            r = subprocess.run([FFMPEG, "-y", "-i", src, *args, dst], capture_output=True, timeout=tmo)
        except Exception as e:
            logging.warning("ffmpeg xatosi: %s", e)
            continue
        if r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 1000:
            os.remove(src)
            return dst
        if os.path.exists(dst):
            os.remove(dst)
    return src


def dl_audio(url, workdir):
    opts = base_opts()
    opts.update({"format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best",
                 "outtmpl": os.path.join(workdir, "%(id).60s.%(ext)s")})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = list_media(workdir)
    if not files:
        raise RuntimeError("audio topilmadi")
    path = max(files, key=os.path.getsize)
    if os.path.getsize(path) > MAX_DL:
        raise TooBigError()
    path = to_audio(path)
    if os.path.getsize(path) > TG_LIMIT:
        raise TooBigError()
    return path, first_entry(info)


def search_songs(query, n=5):
    opts = base_opts()
    opts.pop("max_filesize", None)
    opts.update({"extract_flat": True, "skip_download": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    out = []
    for e in (res or {}).get("entries") or []:
        if e and e.get("id"):
            out.append({"id": e["id"], "title": e.get("title") or e["id"], "duration": e.get("duration"),
                        "uploader": e.get("uploader") or e.get("channel") or ""})
    return out


def song_query(info):
    track = info.get("track")
    artist = info.get("artist") or ((info.get("artists") or [None])[0])
    if track:
        return f"{artist or ''} {track}".strip()
    title = info.get("title") or ""
    if not title.strip() or re.match(r"(video|reel|photo)\s+by\s", title, re.I):
        title = (info.get("description") or "").split("\n")[0]
    title = re.sub(r"#\w+|https?://\S+|@\w+", "", title)
    return re.sub(r"\s+", " ", title).strip()[:80]


def explain_error(e):
    low = str(e).lower()
    if isinstance(e, TooBigError) or "larger than max-filesize" in low:
        return "too_big"
    if any(x in low for x in ("sign in", "not a bot", "login", "cookies", "rate-limit", "rate limit", "429", "403", "challenge")):
        return "blocked"
    if any(x in low for x in ("private", "unavailable", "removed", "deleted", "not exist", "404")):
        return "private"
    if "unsupported url" in low:
        return "unsupported"
    return "error"


# ---------------- Kesh va yordamchilar ----------------
URL_CACHE = OrderedDict()
BOT_USERNAME = None


def remember(url, info):
    token = uuid.uuid4().hex[:10]
    URL_CACHE[token] = {"url": url, "query": song_query(info or {})}
    while len(URL_CACHE) > 500:
        URL_CACHE.popitem(last=False)
    return token


def new_workdir():
    d = os.path.join(WORK_ROOT, uuid.uuid4().hex[:10])
    os.makedirs(d, exist_ok=True)
    return d


async def bot_username():
    global BOT_USERNAME
    if not BOT_USERNAME:
        BOT_USERNAME = (await bot.me()).username
    return BOT_USERNAME


def fmt_dur(sec):
    if not sec:
        return ""
    sec = int(sec)
    return f" ({sec // 60}:{sec % 60:02d})"


def error_text(uid, e):
    key = explain_error(e)
    text = T(uid, key)
    if key == "error":
        text += f"\n<code>{html.escape(str(e)[:150], quote=False)}</code>"
    return text


# ---------------- Handlerlar ----------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    if not get_user_lang(uid):
        await message.answer(TRANSLATIONS["uz_lat"]["choose_lang"], reply_markup=lang_keyboard())
    else:
        await message.answer(T(uid, "welcome"))


@dp.message(Command("lang"))
async def cmd_lang(message: types.Message):
    await message.answer(T(message.from_user.id, "choose_lang"), reply_markup=lang_keyboard())


@dp.callback_query(F.data.startswith("lang:"))
async def process_lang(callback: types.CallbackQuery):
    await callback.answer()
    lang = callback.data.split(":", 1)[1]
    if lang not in TRANSLATIONS:
        return
    set_user_lang(callback.from_user.id, lang, callback.from_user.full_name)
    try:
        await callback.message.edit_text(TRANSLATIONS[lang]["lang_changed"])
    except Exception:
        pass
    await callback.message.answer(TRANSLATIONS[lang]["welcome"])


@dp.callback_query(F.data == "ok")
async def saved_ok_cb(callback: types.CallbackQuery):
    await callback.answer(T(callback.from_user.id, "saved"), show_alert=False)


def result_keyboard(uid, token, username):
    b = InlineKeyboardBuilder()
    b.button(text=T(uid, "btn_save"), callback_data="ok")
    b.button(text=T(uid, "btn_mp3"), callback_data=f"a:{token}")
    b.button(text=T(uid, "btn_find"), callback_data=f"f:{token}")
    b.button(text=T(uid, "btn_group"), url=f"https://t.me/{username}?startgroup=true")
    b.adjust(1, 2, 1)
    return b.as_markup()


async def send_media_file(message, path, caption, markup):
    ext = os.path.splitext(path)[1].lower()
    f = types.FSInputFile(path)
    if ext in IMAGE_EXT:
        return await message.answer_photo(f, caption=caption, reply_markup=markup)
    if ext in VIDEO_EXT:
        try:
            return await message.answer_video(f, caption=caption, reply_markup=markup, supports_streaming=True)
        except Exception as e:
            logging.warning("video yuborilmadi (%s), hujjat sifatida urinaman", e)
    return await message.answer_document(f, caption=caption, reply_markup=markup)


@dp.message(F.text.regexp(URL_RE))
async def handle_url(message: types.Message):
    uid = message.from_user.id
    url = URL_RE.search(message.text).group(0).rstrip(").,;!")
    status = await message.answer(T(uid, "downloading"))
    workdir = new_workdir()
    t0 = time.time()
    try:
        async with SEM:
            files, info = await asyncio.to_thread(dl_video, url, workdir)
        t1 = time.time()
        try:
            await status.edit_text(T(uid, "uploading"))
        except Exception:
            pass
        username = await bot_username()
        token = remember(url, info)
        caption = T(uid, "sent_by").format(bot=username)
        markup = result_keyboard(uid, token, username)
        for i, path in enumerate(files):
            await send_media_file(message, path, caption, markup if i == 0 else None)
        logging.info("tezlik: yuklab olish %.1fs, yuborish %.1fs, hajm %.1f MB (%s)", t1 - t0, time.time() - t1,
                     sum(os.path.getsize(f) for f in files if os.path.exists(f)) / 1e6, url[:60])
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        logging.warning("Yuklash xatosi (%s): %s", url, e)
        try:
            await status.edit_text(error_text(uid, e))
        except Exception:
            await message.answer(error_text(uid, e))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def send_audio_file(message, path, info, uid):
    username = await bot_username()
    title = (info.get("track") or info.get("title") or "Audio")[:64]
    performer = (info.get("artist") or info.get("uploader") or "")[:64]
    name = re.sub(r"[^\w\- ]+", "", title).strip() or "audio"
    ext = os.path.splitext(path)[1] or ".mp3"
    await message.answer_audio(
        types.FSInputFile(path, filename=f"{name}{ext}"), title=title, performer=performer or None,
        caption=T(uid, "sent_by").format(bot=username),
    )


async def do_audio(message, uid, url):
    status = await message.answer(T(uid, "audio_loading"))
    workdir = new_workdir()
    try:
        async with SEM:
            path, info = await asyncio.to_thread(dl_audio, url, workdir)
        await send_audio_file(message, path, info, uid)
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        logging.warning("Audio xatosi (%s): %s", url, e)
        try:
            await status.edit_text(error_text(uid, e))
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@dp.callback_query(F.data.startswith("a:"))
async def cb_audio(callback: types.CallbackQuery):
    await callback.answer()
    item = URL_CACHE.get(callback.data.split(":", 1)[1])
    if not item:
        return await callback.message.answer(T(callback.from_user.id, "expired"))
    await do_audio(callback.message, callback.from_user.id, item["url"])


@dp.callback_query(F.data.startswith("g:"))
async def cb_get_song(callback: types.CallbackQuery):
    await callback.answer()
    vid = callback.data.split(":", 1)[1]
    if not re.fullmatch(r"[\w-]{6,20}", vid):
        return
    await do_audio(callback.message, callback.from_user.id, f"https://www.youtube.com/watch?v={vid}")


async def show_search(message, uid, query):
    status = await message.answer(T(uid, "searching"))
    try:
        async with SEM:
            results = await asyncio.to_thread(search_songs, query, 5)
    except Exception as e:
        logging.warning("Qidiruv xatosi: %s", e)
        return await status.edit_text(error_text(uid, e))
    if not results:
        return await status.edit_text(T(uid, "not_found"))
    lines = [f"{T(uid, 'select_song')}\n"]
    b = InlineKeyboardBuilder()
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {html.escape(r['title'][:70], quote=False)}{fmt_dur(r['duration'])}")
        b.button(text=str(i), callback_data=f"g:{r['id']}")
    b.adjust(5)
    lines.append(f"\n🔍 <b>{html.escape(query, quote=False)}</b>")
    await status.edit_text("\n".join(lines), reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("f:"))
async def cb_find(callback: types.CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    item = URL_CACHE.get(callback.data.split(":", 1)[1])
    if not item:
        return await callback.message.answer(T(uid, "expired"))
    if not item["query"]:
        return await callback.message.answer(T(uid, "no_query"))
    await show_search(callback.message, uid, item["query"])


@dp.message(F.text, ~F.text.startswith("/"))
async def handle_text_search(message: types.Message):
    uid = message.from_user.id
    query = message.text.strip()[:100]
    if len(query) < 2:
        return
    await show_search(message, uid, query)


async def main():
    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    os.makedirs(WORK_ROOT, exist_ok=True)
    asyncio.create_task(start_web_server())
    while True:
        try:
            await wait_for_valid_token()
            print("Bot polling boshlandi...")
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot, drop_pending_updates=True)
        except Exception as e:
            logging.error(f"Polling xatoligi: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
