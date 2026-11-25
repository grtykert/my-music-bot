import os
import asyncio
import re
import sys
import shutil
import tempfile
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, InputMediaAudio
from telebot.apihelper import ApiTelegramException
import yt_dlp
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode, quote, urljoin
from html import unescape
import hmac
import hashlib
import time
import json
import random
import subprocess
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image

try:
    from shazamio import Shazam
except Exception as _shazam_import_error:
    Shazam = None
    print(f"⚠️ Shazam-распознавание недоступно: {_shazam_import_error}")

# --- ПОДКЛЮЧЕНИЕ FFMPEG / FFPROBE ДЛЯ ОБЛОЖЕК И СЖАТИЯ ---
import static_ffmpeg
static_ffmpeg.add_paths()

# --- ВЕБ-СЕРВЕР: Mini App + API + Render health check ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class DummyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_bytes(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            index_path = os.path.join(BASE_DIR, "index.html")
            try:
                with open(index_path, "rb") as f:
                    self._send_bytes(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send_json(404, {"error": "index.html не найден"})
            return
        if parsed.path in ("/healthz", "/health"):
            self._send_json(200, {"ok": True})
            return
        if parsed.path == "/api/data":
            handle_miniapp_get_data(self)
            return
        if parsed.path == "/api/search":
            handle_miniapp_search(self, parse_qs(parsed.query))
            return
        if parsed.path == "/api/stream":
            handle_miniapp_stream(self, parse_qs(parsed.query))
            return
        if parsed.path == "/api/lyrics":
            handle_miniapp_lyrics(self, parse_qs(parsed.query))
            return
        self._send_json(404, {"error": "Не найдено"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._send_json(404, {"error": "Не найдено"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 256 * 1024:
                self._send_json(400, {"error": "Некорректный размер запроса"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Ожидался JSON-объект")
            handle_miniapp_post(self, parsed.path, payload)
        except Exception as e:
            print(f"Ошибка API Mini App: {e}")
            self._send_json(400, {"error": "Некорректный запрос"})

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            index_path = os.path.join(BASE_DIR, "index.html")
            try:
                size = os.path.getsize(index_path)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(size))
                self.end_headers()
            except OSError:
                self.send_response(404)
                self.end_headers()
            return
        if parsed.path in ("/healthz", "/health"):
            self.send_response(200)
            self.end_headers()
            return
        if parsed.path == "/api/stream":
            handle_miniapp_stream(self, parse_qs(parsed.query))
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    ThreadingHTTPServer(("0.0.0.0", port), DummyHandler).serve_forever()


threading.Thread(target=run_dummy_server, daemon=True).start()

# --- БОТ И БЭКАП ГРУППА ---
API_TOKEN = os.environ.get("BOT_TOKEN")
if not API_TOKEN:
    print("❌ Ошибка: Не задана переменная окружения BOT_TOKEN!")
    sys.exit(1)

WEB_APP_URL_RAW = os.environ.get("WEB_APP_URL", "").strip().strip('"').strip("'").rstrip("/")
_web_app_parsed = urlparse(WEB_APP_URL_RAW)
WEB_APP_URL = (
    WEB_APP_URL_RAW
    if _web_app_parsed.scheme == "https"
    and _web_app_parsed.hostname
    and not any(char.isspace() for char in WEB_APP_URL_RAW)
    else ""
)
if WEB_APP_URL:
    print(f"✅ Mini App URL настроен: {WEB_APP_URL}")
else:
    print("⚠️ WEB_APP_URL отсутствует или некорректен. Кнопка Mini App будет скрыта.")

BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID", -1004445455425))
ADMIN_ID = int(os.environ.get("ADMIN_ID", 5378591975))

# ОБЯЗАТЕЛЬНЫЙ КАНАЛ/ГРУППА ДЛЯ ПОДПИСКИ
REQUIRED_CHANNEL = "-1004362385382"

bot = telebot.TeleBot(API_TOKEN)

# --- КЭШИ ---
tracks_cache = {}           # sid -> список треков конкретной выдачи
tracks_cache_lock = threading.Lock()
query_store = {}            # qid -> (query, base_query)
query_store_lock = threading.Lock()
search_results_cache = {}
search_cache_lock = threading.Lock()
SEARCH_CACHE_TTL = 180
stream_url_cache = {}
stream_cache_lock = threading.Lock()
STREAM_CACHE_TTL = 1800
inline_tracks_cache = {}
original_queries = {}
waiting_for_custom_stars = set()
sub_check_cache = {}  # кэш успешной проверки подписки на группу (user_id -> время)
SUB_CACHE_TTL = 120

# Переменные хранения данных и мьютексы для потокобезопасности
active_users = {}
stats_data = {"total_downloads": 0}
audio_cache = {}
user_settings = {}  # Хранение настроек пользователей
backup_msg_id = None
data_lock = threading.Lock()
stats_lock = threading.Lock()
users_offset = 0  # Поправка для ручной корректировки цифры в статистике

# Не больше 2 одновременных загрузок (экономим память на Render)
download_slots = threading.Semaphore(2)

# ============================================================
#                        VIP СИСТЕМА
# ============================================================
VIP_PLANS = {
    7: {"stars": 50, "title": "VIP на 1 неделю"},
    30: {"stars": 150, "title": "VIP на 1 месяц"},
}
VIP_THEMES = ("cyberpunk", "gold", "glass", "oled")
ALL_THEMES = ("default",) + VIP_THEMES
VIP_EFFECTS = ("bassboost", "8d", "slowed", "nightcore")
EFFECT_NAMES = {
    "bassboost": "Bass Boost",
    "8d": "8D Audio",
    "slowed": "Slowed & Reverb",
    "nightcore": "Nightcore",
}
EFFECT_FILTERS = {
    "bassboost": "bass=g=10,alimiter=limit=0.95",
    "8d": "apulsator=mode=sine:hz=0.125",
    "slowed": "aresample=44100,asetrate=37485,aresample=44100,aecho=0.8:0.88:60:0.4",
    "nightcore": "aresample=44100,asetrate=55125,aresample=44100",
}


def max_search_limit(user_id):
    return 50 if is_user_vip(user_id) else 25


def is_user_vip(user_id):
    uid = str(user_id)
    settings = user_settings.get(uid)
    if not isinstance(settings, dict):
        return False
    try:
        return float(settings.get("vip_expires", 0)) > time.time()
    except (TypeError, ValueError):
        return False


def get_vip_expires(user_id):
    settings = user_settings.get(str(user_id))
    if not isinstance(settings, dict):
        return 0
    try:
        return float(settings.get("vip_expires", 0))
    except (TypeError, ValueError):
        return 0


def format_vip_status(user_id):
    expires = get_vip_expires(user_id)
    if expires > time.time():
        return "👑 VIP активен до " + time.strftime("%d.%m.%Y %H:%M", time.localtime(expires))
    return "❌ VIP не активен"


def set_vip_status(user_id, duration_days=None, remove=False):
    """Выдаёт VIP (если VIP уже есть — продлевает), либо забирает его."""
    uid = str(user_id)
    if uid not in user_settings or not isinstance(user_settings[uid], dict):
        user_settings[uid] = {"search_limit": 10}
    settings = user_settings[uid]
    if remove:
        settings["vip_expires"] = 0
        settings["theme"] = "default"
        settings["search_limit"] = min(int(settings.get("search_limit", 10)), 25)
    elif duration_days:
        base = max(time.time(), get_vip_expires(uid))
        settings["vip_expires"] = base + duration_days * 24 * 60 * 60
    save_all_data()  # VIP сохраняем сразу, а не отложенно
    return settings.get("vip_expires", 0)


def is_admin(message):
    return message.from_user is not None and message.from_user.id == ADMIN_ID


# --- ФУНКЦИЯ ПРОВЕРКИ ПОДПИСКИ ---
def check_user_subscription(user_id):
    # VIP пользователи обходят проверку подписки, пока VIP активен
    if is_user_vip(user_id):
        return True
    cached = sub_check_cache.get(user_id)
    if cached and time.time() - cached < SUB_CACHE_TTL:
        return True
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        status = getattr(member, "status", "left")
        print(f"Проверка подписки: user_id={user_id}, group={REQUIRED_CHANNEL}, status={status}")
        ok = status in ("creator", "administrator", "member")
        if ok:
            sub_check_cache[user_id] = time.time()
        return ok
    except Exception as e:
        print(f"Ошибка проверки подписки для user_id={user_id} в {REQUIRED_CHANNEL}: {e}")
        return False


def send_subscription_prompt(chat_id):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("📢 Подписаться на группу", url="https://t.me/+8JYfdTy2YhE0NDJi"))
    markup.row(InlineKeyboardButton("🔄 Я подписался", callback_data="check_sub"))
    markup.row(InlineKeyboardButton("👑 Купить VIP (без подписки)", callback_data="show_vip"))
    bot.send_message(
        chat_id,
        "❌ **Доступ ограничен!**\n\nЧтобы пользоваться ботом и искать музыку, необходимо подписаться на нашу группу: https://t.me/+8JYfdTy2YhE0NDJi\n\nПосле подписки нажмите кнопку ниже:\n\n*Или купите VIP, чтобы отключить это требование на время подписки!*",
        reply_markup=markup,
        parse_mode="Markdown"
    )


# ============================================================
#              ОШИБКИ: ЛОГ + СООБЩЕНИЕ АДМИНУ
# ============================================================
_last_admin_report = 0
_report_lock = threading.Lock()


def report_error(context, error):
    """Печатает полную причину в лог и (не чаще раза в 10 сек) шлёт админу."""
    global _last_admin_report
    print(f"❌ {context}: {error!r}")
    with _report_lock:
        now = time.time()
        if now - _last_admin_report < 10:
            return
        _last_admin_report = now
    try:
        bot.send_message(ADMIN_ID, f"⚠️ {context}\n{repr(error)[:600]}")
    except Exception:
        pass


def explain_error(error):
    """Короткая понятная причина для пользователя."""
    text = str(error).lower()
    if "429" in text or "403" in text or "sign in" in text or "confirm you" in text:
        return "Источник временно блокирует запросы. Попробуй позже."
    if "geo" in text or "not available" in text or "blocked" in text or "private" in text or "premium" in text:
        return "Трек недоступен (закрыт, платный или ограничен по региону)."
    if "client id" in text or "client_id" in text:
        return "Сервис обновляется, попробуй позже."
    if "no space" in text or "too large" in text:
        return "Файл слишком большой."
    return ""


# --- ПОДГОТОВКА ОБЛОЖКИ ДЛЯ TELEGRAM ---
def prepare_telegram_thumbnail(image_filename):
    if not image_filename or not os.path.exists(image_filename):
        return None

    thumbnail_filename = os.path.splitext(image_filename)[0] + "_telegram.jpg"
    max_thumbnail_size = 200 * 1024

    try:
        with Image.open(image_filename) as image:
            image = image.convert("RGB")
            image.thumbnail((320, 320), Image.LANCZOS)

            quality = 85
            while True:
                image.save(thumbnail_filename, "JPEG", quality=quality, optimize=True)
                if os.path.getsize(thumbnail_filename) <= max_thumbnail_size:
                    return thumbnail_filename

                if quality > 40:
                    quality -= 10
                else:
                    width, height = image.size
                    if max(width, height) <= 160:
                        break
                    image = image.resize(
                        (max(1, int(width * 0.85)), max(1, int(height * 0.85))),
                        Image.LANCZOS
                    )
                    quality = 70

        if os.path.exists(thumbnail_filename):
            os.remove(thumbnail_filename)
    except Exception as e:
        print(f"Ошибка подготовки обложки для Telegram: {e}")
        if os.path.exists(thumbnail_filename):
            try:
                os.remove(thumbnail_filename)
            except Exception:
                pass

    return None


# --- ПОИСК И ЗАГРУЗКА ОБЛОЖКИ ДЛЯ ТРЕКА ---
def get_track_thumbnail(info, audio_filename):
    base_name = os.path.splitext(audio_filename)[0]

    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = base_name + ext
        if os.path.exists(candidate):
            prepared = prepare_telegram_thumbnail(candidate)
            if prepared:
                return candidate, prepared

    thumbnail_url = info.get("thumbnail")
    if not thumbnail_url:
        thumbnails = info.get("thumbnails") or []
        for item in reversed(thumbnails):
            if item.get("url"):
                thumbnail_url = item["url"]
                break

    if thumbnail_url:
        downloaded_thumb = base_name + "_source_thumb"
        try:
            request = urllib.request.Request(
                thumbnail_url,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("Обложка слишком большая")
            with open(downloaded_thumb, "wb") as image_file:
                image_file.write(data)
            prepared = prepare_telegram_thumbnail(downloaded_thumb)
            if prepared:
                return downloaded_thumb, prepared
            os.remove(downloaded_thumb)
        except Exception as e:
            print(f"Не удалось получить обложку по URL: {e}")
            if os.path.exists(downloaded_thumb):
                try:
                    os.remove(downloaded_thumb)
                except Exception:
                    pass

    print(f"⚠️ Для трека «{info.get('title', 'Без названия')}» обложка не найдена")
    return None, None


# --- ФУНКЦИЯ АВТОМАТИЧЕСКОГО СЖАТИЯ С СОХРАНЕНИЕМ ОБЛОЖКИ ---
def compress_audio_if_needed(input_filename, thumbnail_filename=None, max_size_mb=48):
    if not input_filename or not os.path.exists(input_filename):
        return input_filename

    file_size_mb = os.path.getsize(input_filename) / (1024 * 1024)
    if file_size_mb <= max_size_mb:
        return input_filename

    print(f"⚠️ Файл весит {file_size_mb:.1f} МБ. Сжимаем до 128k с сохранением обложки...")
    output_filename = os.path.splitext(input_filename)[0] + "_compressed.mp3"

    cmd = ["ffmpeg", "-y", "-i", input_filename]
    if thumbnail_filename and os.path.exists(thumbnail_filename):
        cmd += [
            "-i", thumbnail_filename,
            "-map", "0:a:0",
            "-map", "1:v:0",
            "-c:v", "copy",
            "-metadata:s:v", "title=Album cover",
            "-metadata:s:v", "comment=Cover (front)",
            "-disposition:v", "attached_pic"
        ]
    else:
        cmd += ["-map", "0:a:0", "-map", "0:v?", "-c:v", "copy"]

    cmd += [
        "-c:a", "libmp3lame",
        "-b:a", "128k",
        "-id3v2_version", "3",
        output_filename
    ]

    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        if os.path.exists(output_filename) and os.path.getsize(output_filename) > 0:
            os.remove(input_filename)
            return output_filename
    except Exception as e:
        print(f"Ошибка сжатия через ffmpeg: {e}")

    return input_filename


def ensure_telegram_audio(input_filename):
    """Telegram нормально показывает как аудио только mp3/m4a. Остальное (opus/ogg/webm) конвертируем."""
    if not input_filename or not os.path.exists(input_filename):
        return input_filename
    if input_filename.lower().endswith((".mp3", ".m4a")):
        return input_filename
    output_filename = os.path.splitext(input_filename)[0] + "_conv.mp3"
    cmd = [
        "ffmpeg", "-y", "-i", input_filename,
        "-vn", "-c:a", "libmp3lame", "-b:a", "160k",
        "-id3v2_version", "3", output_filename
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=300)
    if os.path.exists(output_filename) and os.path.getsize(output_filename) > 0:
        try:
            os.remove(input_filename)
        except OSError:
            pass
        return output_filename
    raise RuntimeError("ffmpeg не создал mp3")


# --- АУДИОЭФФЕКТЫ (VIP) ---
def apply_audio_effect(input_filename, effect):
    """Применяет эффект через ffmpeg. Возвращает путь к новому mp3 (старый файл удаляется)."""
    flt = EFFECT_FILTERS.get(effect)
    if not flt or not input_filename or not os.path.exists(input_filename):
        return input_filename
    output_filename = os.path.splitext(input_filename)[0] + f"_fx_{effect}.mp3"
    cmd = [
        "ffmpeg", "-y", "-i", input_filename,
        "-vn", "-af", flt,
        "-c:a", "libmp3lame", "-b:a", "192k",
        "-id3v2_version", "3",
        output_filename
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=300)
    if os.path.exists(output_filename) and os.path.getsize(output_filename) > 0:
        try:
            os.remove(input_filename)
        except OSError:
            pass
        return output_filename
    raise RuntimeError("ffmpeg не создал файл с эффектом")


# ============================================================
#              ЕДИНАЯ ЗАГРУЗКА АУДИО (с запасными вариантами)
# ============================================================
def ydl_download_audio(track_url, outtmpl):
    """Пробует несколько конфигураций yt-dlp. Возвращает (info, путь к файлу)."""
    base = {
        "format": "bestaudio[protocol^=http]/bestaudio/best",  # сначала прямой mp3, потом HLS
        "outtmpl": outtmpl,
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "retries": 2,
        "extractor_retries": 2,
    }
    attempts = [
        {**base, "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "EmbedThumbnail"},
        ]},
        {**base, "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
        ]},
        {**base, "writethumbnail": False, "format": "bestaudio/best"},
    ]
    last_error = None
    for opts in attempts:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(track_url, download=True)
                filename = ydl.prepare_filename(info)
                if filename and os.path.exists(filename):
                    return info, filename
                raise RuntimeError("Файл после загрузки не найден")
        except Exception as e:
            last_error = e
            print(f"Попытка загрузки не удалась ({track_url}): {e!r}")
    raise last_error


def prepare_track_file(track_url, workdir, effect="none"):
    """Скачивает, применяет эффект, приводит к mp3/m4a, сжимает. Возвращает (info, audio, thumb)."""
    info, audio_filename = ydl_download_audio(track_url, os.path.join(workdir, "track_%(id)s.%(ext)s"))
    _original_thumb, thumbnail_filename = get_track_thumbnail(info, audio_filename)
    if effect != "none":
        audio_filename = apply_audio_effect(audio_filename, effect)
    else:
        audio_filename = ensure_telegram_audio(audio_filename)
    audio_filename = compress_audio_if_needed(audio_filename, thumbnail_filename)
    return info, audio_filename, thumbnail_filename


def count_download():
    with stats_lock:
        stats_data["total_downloads"] += 1
    request_save()


# --- ТЕКСТЫ ПЕСЕН (VIP) через бесплатный LRCLIB ---
def _http_json(url, timeout=8):
    request = urllib.request.Request(url, headers={"User-Agent": "TelegramMusicBot/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return None
    except Exception as e:
        print(f"Ошибка запроса текста песни: {e}")
        return None


def split_artist_title(title, uploader=""):
    clean = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", title or "")
    clean = re.sub(r"(?i)\b(official|audio|video|lyrics|lyric video|prod\.?\s.*)$", "", clean).strip(" -–—|")
    artist = (uploader or "").strip()
    name = clean.strip()
    if " - " in clean:
        left, right = clean.split(" - ", 1)
        artist, name = left.strip(), right.strip()
    return artist, name


def fetch_lyrics(title, uploader=""):
    artist, name = split_artist_title(title, uploader)
    if not name:
        return None
    if artist:
        data = _http_json("https://lrclib.net/api/get?" + urlencode({"artist_name": artist, "track_name": name}))
        if isinstance(data, dict) and data.get("plainLyrics"):
            return data["plainLyrics"].strip()
    for q in (f"{artist} {name}".strip(), name):
        data = _http_json("https://lrclib.net/api/search?" + urlencode({"q": q}))
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("plainLyrics"):
                    return item["plainLyrics"].strip()
    return None


# ============================================================
#              СИСТЕМА ЕДИНОГО БЭКАПА ЧЕРЕЗ ЗАКРЕП
# ============================================================
def restore_all_data():
    global active_users, stats_data, audio_cache, user_settings, backup_msg_id, users_offset
    try:
        chat = bot.get_chat(BACKUP_CHANNEL_ID)
        if chat.pinned_message and chat.pinned_message.document:
            backup_msg_id = chat.pinned_message.message_id
            file_info = bot.get_file(chat.pinned_message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            data = json.loads(downloaded_file.decode('utf-8'))

            raw_users = data.get("users", {})
            now = time.time()
            month_ago = now - (28 * 24 * 60 * 60)

            if isinstance(raw_users, list):
                active_users = {}
                for uid in raw_users:
                    active_users[str(uid)] = now
            elif isinstance(raw_users, dict):
                active_users = {str(uid): t for uid, t in raw_users.items() if t >= month_ago}
            else:
                active_users = {}

            stats_data = data.get("stats", {"total_downloads": 0})
            audio_cache = data.get("cache", {})
            user_settings = data.get("settings", {})
            users_offset = data.get("users_offset", 0)

            active_users[str(ADMIN_ID)] = now
            print("✅ Все данные успешно восстановлены!")
    except Exception as e:
        print(f"⚠️ Ошибка или отсутствие закрепа при автовосстановлении: {e}")
        active_users[str(ADMIN_ID)] = time.time()


def _serialize_state():
    """Сериализация с повтором: другие потоки могут менять словари прямо во время дампа."""
    for _ in range(5):
        try:
            active_users[str(ADMIN_ID)] = time.time()
            return json.dumps({
                "users": active_users,
                "stats": stats_data,
                "cache": audio_cache,
                "settings": user_settings,
                "users_offset": users_offset
            }, ensure_ascii=False)
        except RuntimeError:
            time.sleep(0.05)
    return None


def save_all_data():
    global backup_msg_id
    with data_lock:
        try:
            payload = _serialize_state()
            if payload is None:
                print("Не удалось сериализовать данные для бэкапа")
                return
            with open("data.json", "w", encoding="utf-8") as f:
                f.write(payload)

            with open("data.json", "rb") as f:
                if backup_msg_id:
                    try:
                        bot.edit_message_media(
                            chat_id=BACKUP_CHANNEL_ID,
                            message_id=backup_msg_id,
                            media=telebot.types.InputMediaDocument(f, caption="💾 Бэкап базы данных")
                        )
                        return
                    except Exception as e:
                        print(f"Не удалось обновить бэкап: {e}")
                        f.seek(0)

                msg = bot.send_document(BACKUP_CHANNEL_ID, f, caption="💾 Бэкап базы данных")
                backup_msg_id = msg.message_id
                try:
                    bot.unpin_all_chat_messages(BACKUP_CHANNEL_ID)
                    bot.pin_chat_message(BACKUP_CHANNEL_ID, backup_msg_id, disable_notification=True)
                except Exception as pe:
                    print(f"Ошибка закрепления: {pe}")
        except Exception as e:
            print(f"Ошибка сохранения бэкапа: {e}")


# --- ОТЛОЖЕННОЕ СОХРАНЕНИЕ: не чаще раза в SAVE_DELAY секунд ---
SAVE_DELAY = 20
_save_dirty = threading.Event()


def request_save():
    _save_dirty.set()


def _saver_loop():
    while True:
        _save_dirty.wait()
        time.sleep(SAVE_DELAY)
        _save_dirty.clear()
        save_all_data()


restore_all_data()
bot_start_time = time.time()
threading.Thread(target=_saver_loop, daemon=True).start()


def register_user(chat_id):
    key = str(chat_id)
    now = time.time()
    prev = active_users.get(key)
    active_users[key] = now
    # сохраняем только для новых пользователей или если не заходил 10+ минут
    if prev is None or now - prev > 600:
        request_save()


def open_app_markup():
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(
        text="🎧 Открыть музыкальное приложение",
        web_app=telebot.types.WebAppInfo(url=WEB_APP_URL)
    ))
    return markup


def send_vip_menu(chat_id, user_id):
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("👑 VIP на 1 неделю — 50 ⭐", callback_data="buy_vip_7"),
        InlineKeyboardButton("👑 VIP на 1 месяц — 150 ⭐ (выгода 25%)", callback_data="buy_vip_30"),
    )
    text = (
        f"👑 **VIP подписка**\n\n{format_vip_status(user_id)}\n\n"
        "**Что даёт VIP:**\n"
        "🚀 Без обязательной подписки на группу\n"
        "📈 Выдача до 50 треков (вместо 25)\n"
        "📜 Тексты песен (`/lyrics название` и в приложении)\n"
        "🎚 Bass Boost, 8D, Slowed & Nightcore при скачивании\n"
        "🎨 Эксклюзивные темы приложения\n\n"
        "Покупка продлевает уже активную подписку."
    )
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)


@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    register_user(chat_id)

    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].strip().lower() == "vip":
        send_vip_menu(chat_id, message.from_user.id)
        return

    text = (
        "👋 Напиши название песни, несколько слов из неё или отправь ссылку на видео — я попробую найти трек. 🎵\n\n"
        "💬 Наша группа: https://t.me/+8JYfdTy2YhE0NDJi\n\n"
        "👑 VIP подписка: /vip\n"
        "💖 Поддержка разработчика: /donate"
    )
    if message.chat.type == "private" and not WEB_APP_URL:
        text += "\n\n⚠️ Mini App пока не настроен. Проверь WEB_APP_URL в переменных Render."
    bot.send_message(chat_id, text, disable_web_page_preview=True)

    if message.chat.type == "private" and WEB_APP_URL:
        bot.send_message(chat_id, "Музыкальное приложение:", reply_markup=open_app_markup())


@bot.message_handler(commands=['vip'])
def vip_command(message):
    register_user(message.chat.id)
    send_vip_menu(message.chat.id, message.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == "show_vip")
def callback_show_vip(call):
    bot.answer_callback_query(call.id)
    send_vip_menu(call.message.chat.id, call.from_user.id)


@bot.message_handler(commands=['app', 'webapp', 'mini', 'player'])
def open_app_command(message):
    chat_id = message.chat.id
    register_user(chat_id)
    if not check_user_subscription(message.from_user.id):
        send_subscription_prompt(chat_id)
        return
    if not WEB_APP_URL:
        bot.send_message(chat_id, "⚠️ Mini App пока не настроен. Проверь WEB_APP_URL в переменных Render.")
        return
    bot.send_message(chat_id, "👇 Нажми на кнопку ниже, чтобы открыть приложение:", reply_markup=open_app_markup())


# ОБРАБОТЧИК КНОПКИ ПРОВЕРКИ ПОДПИСКИ
@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def callback_check_sub(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    if check_user_subscription(user_id):
        bot.answer_callback_query(call.id, "✅ Спасибо за подписку!", show_alert=True)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        register_user(chat_id)
        bot.send_message(
            chat_id,
            "🎉 Подписка подтверждена! Теперь вы можете пользоваться ботом. Просто напишите название трека 🎵"
        )
    else:
        bot.answer_callback_query(call.id, "❌ Вы еще не подписались на группу!", show_alert=True)


# --- АДМИНСКАЯ КОМАНДА ДЛЯ РУЧНОЙ ПОПРАВКИ СТАТИСТИКИ ---
@bot.message_handler(commands=['setusers'])
def set_users_command(message):
    global users_offset
    if is_admin(message):
        args = message.text.split(maxsplit=1)
        if len(args) < 2 or not args[1].isdigit():
            bot.reply_to(message, "❗️ Укажи число. Пример: `/setusers 35`", parse_mode="Markdown")
            return

        target_count = int(args[1])
        now = time.time()
        month_ago = now - (28 * 24 * 60 * 60)
        current_real_count = sum(1 for uid, last_seen in active_users.items() if last_seen >= month_ago)

        users_offset = target_count - current_real_count
        save_all_data()
        bot.reply_to(message, f"✅ Готово! Установлена поправка: `{users_offset}`. Теперь в статистике отображается нужное число.", parse_mode="Markdown")
    else:
        bot.reply_to(message, "❌ У вас нет прав для этой команды.")


# --- АДМИН КОМАНДЫ VIP ---
def parse_admin_target(message):
    """ID берётся из аргумента, либо из сообщения, на которое ответили."""
    args = message.text.split()
    if len(args) >= 2 and args[1].lstrip("-").isdigit():
        return args[1], args[2:]
    if message.reply_to_message and message.reply_to_message.from_user:
        return str(message.reply_to_message.from_user.id), args[1:]
    return None, []


@bot.message_handler(commands=['givevip'])
def give_vip_admin(message):
    if not is_admin(message):
        return bot.reply_to(message, "❌ У вас нет прав.")
    uid, rest = parse_admin_target(message)
    if not uid:
        return bot.reply_to(
            message,
            "❗️ Пример: `/givevip ID Дни`\nНапр: `/givevip 5378591975 30`\nИли ответь на сообщение человека: `/givevip 7`",
            parse_mode="Markdown"
        )
    days = 30
    if rest:
        if not rest[0].isdigit() or int(rest[0]) <= 0 or int(rest[0]) > 3650:
            return bot.reply_to(message, "❗️ Дни — число от 1 до 3650.")
        days = int(rest[0])
    set_vip_status(uid, duration_days=days)
    bot.reply_to(message, f"✅ Пользователю {uid} выдан VIP на {days} дн.\n{format_vip_status(uid)}")
    try:
        bot.send_message(int(uid), f"🎁 Вам выдан VIP статус на {days} дн.!\n{format_vip_status(uid)}")
    except Exception:
        pass


@bot.message_handler(commands=['takevip'])
def take_vip_admin(message):
    if not is_admin(message):
        return bot.reply_to(message, "❌ У вас нет прав.")
    uid, _ = parse_admin_target(message)
    if not uid:
        return bot.reply_to(message, "❗️ Пример: `/takevip ID` (или ответом на сообщение)", parse_mode="Markdown")
    set_vip_status(uid, remove=True)
    bot.reply_to(message, f"❌ VIP статус пользователя {uid} отозван.")
    try:
        bot.send_message(int(uid), "⚠️ Ваш VIP статус был отозван администратором.")
    except Exception:
        pass


@bot.message_handler(commands=['vipinfo'])
def vip_info_admin(message):
    if not is_admin(message):
        return bot.reply_to(message, "❌ У вас нет прав.")
    uid, _ = parse_admin_target(message)
    if not uid:
        return bot.reply_to(message, "❗️ Пример: `/vipinfo ID`", parse_mode="Markdown")
    bot.reply_to(message, f"Пользователь {uid}:\n{format_vip_status(uid)}")


@bot.message_handler(commands=['viplist'])
def vip_list_admin(message):
    if not is_admin(message):
        return bot.reply_to(message, "❌ У вас нет прав.")
    lines = []
    for uid in list(user_settings.keys()):
        if is_user_vip(uid):
            lines.append(f"• `{uid}` — до {time.strftime('%d.%m.%Y %H:%M', time.localtime(get_vip_expires(uid)))}")
    bot.reply_to(
        message,
        "👑 **Активные VIP:**\n" + ("\n".join(lines) if lines else "Пока нет."),
        parse_mode="Markdown"
    )


# --- ТЕКСТЫ ПЕСЕН В БОТЕ (VIP) ---
@bot.message_handler(commands=['lyrics'])
def lyrics_command(message):
    register_user(message.chat.id)
    if not is_user_vip(message.from_user.id):
        return bot.reply_to(message, "📜 Тексты песен доступны только с VIP. Подробнее: /vip")
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return bot.reply_to(message, "❗️ Пример: `/lyrics Artist - Название`", parse_mode="Markdown")
    status = bot.reply_to(message, "🔍 Ищу текст...")

    def worker():
        text = fetch_lyrics(args[1])
        if not text:
            bot.edit_message_text("❌ Текст не найден. Попробуй формат `Исполнитель - Название`.", message.chat.id, status.message_id, parse_mode="Markdown")
            return
        chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)][:3]
        bot.edit_message_text(f"📜 {args[1]}\n\n{chunks[0]}", message.chat.id, status.message_id)
        for chunk in chunks[1:]:
            bot.send_message(message.chat.id, chunk)

    threading.Thread(target=worker, daemon=True).start()


# --- НАСТРОЙКИ И ПОЛЗУНОК (по user_id, а не chat_id) ---
def get_settings_keyboard(user_id):
    user_key = str(user_id)
    if user_key not in user_settings or not isinstance(user_settings[user_key], dict):
        user_settings[user_key] = {"search_limit": 10}

    max_limit = max_search_limit(user_id)
    limit = min(max_limit, int(user_settings[user_key].get("search_limit", 10)))
    markup = InlineKeyboardMarkup()

    total_blocks = max_limit // 5
    filled_blocks = min(total_blocks, limit // 5)
    slider_text = "🟦" * filled_blocks + "⬜️" * (total_blocks - filled_blocks)
    markup.row(
        InlineKeyboardButton("➖", callback_data="setslider_minus"),
        InlineKeyboardButton(f"{limit} треков", callback_data="ignore"),
        InlineKeyboardButton("➕", callback_data="setslider_plus")
    )
    markup.row(InlineKeyboardButton(f"Шкала: {slider_text}", callback_data="ignore"))
    return markup


@bot.message_handler(commands=['settings'])
def settings_command(message):
    register_user(message.chat.id)
    user_id = message.from_user.id
    note = "до 50 треков (VIP)" if is_user_vip(user_id) else "до 25 треков (с VIP — до 50)"
    bot.reply_to(
        message,
        f"⚙️ **Настройки поиска**\n\nС помощью ползунка ниже выбери количество треков на одной странице: {note}",
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard(user_id)
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("setslider_"))
def handle_slider_callback(call):
    user_id = call.from_user.id
    user_key = str(user_id)
    if user_key not in user_settings or not isinstance(user_settings[user_key], dict):
        user_settings[user_key] = {"search_limit": 10}

    max_limit = max_search_limit(user_id)
    current_limit = min(max_limit, int(user_settings[user_key].get("search_limit", 10)))
    action = call.data.split("_")[1]

    if action == "minus":
        new_limit = max(5, current_limit - 5)
    else:
        new_limit = min(max_limit, current_limit + 5)

    if new_limit != user_settings[user_key].get("search_limit"):
        user_settings[user_key]["search_limit"] = new_limit
        request_save()
        try:
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=get_settings_keyboard(user_id)
            )
        except Exception:
            pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "ignore")
def handle_ignore(call):
    bot.answer_callback_query(call.id)


@bot.message_handler(commands=['clearcache'])
def clear_bot_cache(message):
    if is_admin(message):
        audio_cache.clear()
        save_all_data()
        bot.reply_to(message, "✅ Кэш треков успешно очищен!")
    else:
        bot.reply_to(message, "❌ У вас нет прав для этой команды.")


@bot.my_chat_member_handler()
def handle_chat_member(message):
    chat_id = message.chat.id
    new_status = message.new_chat_member.status
    if new_status in ['kicked', 'left']:
        pass
    elif new_status == 'member':
        register_user(chat_id)


# ============================================================
#                 ПОИСК: НОРМАЛИЗАЦИЯ И РАНЖИРОВАНИЕ
# ============================================================
BAD_WORDS = ("slowed", "sped up", "speed up", "speedup", "nightcore", "remix",
             "reverb", "cover", "mashup", "bass boosted", "8d", "karaoke", "instrumental")

NOISE_TOKENS = {"official", "audio", "video", "lyrics", "lyric", "feat", "ft", "prod",
                "music", "new", "hd", "hq", "free", "download", "full"}
# Слова, которые часто встречаются в названиях разных песен и сами по себе
# почти ничего не говорят о том, какую композицию ищет пользователь.
GENERIC_SEARCH_TOKENS = {
    "i", "a", "an", "the", "to", "go", "of", "my", "your", "you", "me",
    "we", "us", "he", "she", "it", "is", "are", "am", "be", "do", "does",
    "did", "not", "no", "and", "or", "in", "on", "at", "for", "with", "from",
    "by", "up", "down", "out", "into", "when", "where", "what", "who", "how",
}
UNREQUESTED_VERSION_TAGS = (
    "bass boost", "bassboost", "bass boosted", "slowed", "sped up", "speed up",
    "speedup", "nightcore", "remix", "rmx", "reverb", "cover", "mashup",
    "karaoke", "instrumental", "8d audio", "8d",
)
WEAK_SCORE = 11  # если лучший результат слабее — пробуем варианты запроса

_LAYOUT_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,./"
_LAYOUT_RU = "йцукенгшщзхъфывапролджэячсмитьбю."
EN2RU = str.maketrans(_LAYOUT_EN, _LAYOUT_RU)
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def entry_url(item):
    # Всегда берём нормальную страницу трека, а не api-ссылку.
    return item.get("webpage_url") or item.get("original_url") or item.get("url")


def norm_text(s):
    s = (s or "").casefold().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def _query_tokens(q):
    tokens = [t for t in norm_text(q).split() if t not in NOISE_TOKENS]
    return tokens or norm_text(q).split()


def _match_one(track, q_norm, q_tokens):
    from difflib import SequenceMatcher
    title_n = norm_text(track.get("title"))
    up_n = norm_text(track.get("uploader"))
    text = (up_n + " " + title_n).strip()
    hay = set(text.split())
    matched = 0
    for tok in q_tokens:
        if tok in hay or (len(tok) >= 3 and any(h.startswith(tok) for h in hay)):
            matched += 1
    coverage = matched / max(1, len(q_tokens))
    score = coverage * 10
    if q_norm and q_norm in title_n:
        score += 5
    elif q_norm and q_norm in text:
        score += 4
    if q_norm and title_n == q_norm:
        score += 4
    elif q_norm and title_n.startswith(q_norm):
        score += 2
    score += SequenceMatcher(None, q_norm, title_n).ratio() * 3
    score += SequenceMatcher(None, q_norm, text).ratio() * 2
    if coverage < 0.5:
        score -= 5
    return score


def score_tracks(entries, queries):
    # Возвращает [(score, track)] по убыванию. queries — строка или список (первый — оригинал).
    if isinstance(queries, str):
        queries = [queries]
    prepared = []
    for i, qs in enumerate(queries):
        qn = norm_text(qs)
        if qn:
            prepared.append((qn, _query_tokens(qs), 1.0 if i == 0 else 0.8))
    if not prepared:
        return [(0, t) for t in (entries or []) if t]
    all_q = " ".join(p[0] for p in prepared)
    scored = []
    for pos, t in enumerate(entries or []):
        if not t:
            continue
        score = max(_match_one(t, qn, toks) * w for qn, toks, w in prepared)
        title = (t.get("title") or "").casefold()
        for w in BAD_WORDS:
            if w in title and w not in all_q:
                score -= 6
        dur = t.get("duration")
        if dur and (dur < 60 or dur > 900):
            score -= 8
        score -= pos * 0.05
        scored.append((score, t))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def rank_tracks(entries, query):
    return [t for _, t in score_tracks(entries, query)]


def _query_token_matches(token, words):
    """Мягкое совпадение слова, чтобы поиск переживал небольшие опечатки."""
    from difflib import SequenceMatcher

    if token in words:
        return True
    if len(token) < 3:
        return False
    for word in words:
        if len(word) < 3 or abs(len(word) - len(token)) > 2:
            continue
        if SequenceMatcher(None, token, word).ratio() >= 0.82:
            return True
    return False


def _has_unrequested_version_tag(track, query):
    title = f" {norm_text(track.get('title'))} "
    query_text = f" {norm_text(query)} "
    for tag in UNREQUESTED_VERSION_TAGS:
        tag_norm = norm_text(tag)
        if f" {tag_norm} " in title and f" {tag_norm} " not in query_text:
            return True
    return False


def _track_matches_track_query(track, query):
    """Оставляет результаты с совпадающими значимыми словами, а не общими фразами."""
    tokens = _query_tokens(query)
    if not tokens:
        return True

    # Не позволяем словам вроде «I / go / to / the» выдавать посторонние песни.
    significant = [token for token in tokens if token not in GENERIC_SEARCH_TOKENS]
    if not significant:
        significant = tokens

    title_words = set(norm_text(track.get("title")).split())
    artist_words = set(norm_text(track.get("uploader")).split())
    all_words = title_words | artist_words

    matched_significant = sum(
        _query_token_matches(token, all_words) for token in significant
    )
    required_matches = max(1, int(len(significant) * 0.8 + 0.999999))
    if matched_significant < required_matches:
        return False

    # Если пользователь явно не искал remix/bass boost/slowed и т.п.,
    # не показываем такую модификацию вместо оригинала.
    if _has_unrequested_version_tag(track, query):
        return False

    return True


def dedupe_ranked_titles(scored_tracks):
    """Оставляет лучший результат для повторяющегося многословного названия."""
    result = []
    seen_titles = set()
    for score, track in scored_tracks:
        title = norm_text(track.get("title"))
        # Однословные названия слишком часто совпадают у разных песен.
        if len(title.split()) >= 2:
            if title in seen_titles:
                continue
            seen_titles.add(title)
        result.append((score, track))
    return result


def dedupe_tracks(tracks):
    seen = set()
    out = []
    for t in tracks:
        k1 = t["url"].split("?")[0].rstrip("/")
        k2 = (norm_text(t.get("title")), norm_text(t.get("uploader")))
        if k1 in seen or k2 in seen:
            continue
        seen.add(k1)
        seen.add(k2)
        out.append(t)
    return out


def _looks_like_wrong_layout(q):
    words = [w for w in re.findall(r"[a-z\[\];',.]+", q.lower()) if len(w) >= 3]
    return bool(words) and all(not re.search(r"[aeiouy]", w) for w in words)


def query_variants(query):
    # Запасные формулировки запроса, если обычный поиск дал слабый результат.
    q = query.strip()
    out = []

    def add(v):
        v = " ".join((v or "").split())
        if v and v.casefold() != q.casefold() and v.casefold() not in [x.casefold() for x in out]:
            out.append(v)

    if re.search(r"[а-яА-ЯёЁ]", q):
        add("".join(_TRANSLIT.get(ch, ch) for ch in q.lower()))
    elif re.search(r"[a-zA-Z]", q) and _looks_like_wrong_layout(q):
        add(q.lower().translate(EN2RU))

    cleaned = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", q)
    cleaned = " ".join(w for w in cleaned.split() if w.casefold().strip(".,") not in NOISE_TOKENS)
    add(cleaned)

    if " - " in q:
        left, right = q.split(" - ", 1)
        add(f"{left} {right}")
        add(f"{right} {left}")
    return out[:3]



# --- ДОПОЛНИТЕЛЬНЫЙ ОФИЦИАЛЬНЫЙ ИСТОЧНИК: AUDIUS ---
# Используем только треки, для которых API прямо указывает доступность загрузки/стриминга.
AUDIUS_API_BASE = "https://api.audius.co/v1"
AUDIUS_TIMEOUT = 18
AUDIUS_MAX_FILE_BYTES = 100 * 1024 * 1024


def audius_api_json(path, params=None):
    query = urlencode(params or {})
    url = f"{AUDIUS_API_BASE}/{str(path).lstrip('/')}"
    if query:
        url += "?" + query
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SoundMusicTelegramBot/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=AUDIUS_TIMEOUT) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise RuntimeError("Audius вернул слишком большой ответ API")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("status") not in (None, "success"):
        raise RuntimeError("Audius вернул некорректный ответ")
    return payload


def _audius_data_track(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else None
    if isinstance(data, dict):
        if isinstance(data.get("track"), dict):
            return data["track"]
        return data
    return None


def audius_artist_name(item):
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    artists = []
    for artist in item.get("artists") or []:
        if isinstance(artist, dict):
            value = artist.get("name") or artist.get("handle")
            if value:
                artists.append(str(value).strip())
    if artists:
        return ", ".join(dict.fromkeys(x for x in artists if x))[:120]
    return str(user.get("name") or user.get("handle") or item.get("artist_name") or "Неизвестен")[:120]


def audius_artwork_url(item):
    artwork = item.get("artwork") or item.get("cover_art") or ""
    if isinstance(artwork, dict):
        artwork = (artwork.get("_480x480") or artwork.get("480x480") or
                   artwork.get("_1000x1000") or artwork.get("1000x1000") or
                   artwork.get("_150x150") or artwork.get("150x150") or "")
    url = str(artwork or "").strip()
    parsed = urlparse(url)
    return url[:1000] if parsed.scheme == "https" and parsed.hostname else ""


def audius_track_allowed(item, require_download=True):
    if not isinstance(item, dict):
        return False
    if (item.get("is_available") is False or item.get("is_deleted") is True
            or item.get("is_delete") is True or item.get("is_unlisted") is True):
        return False
    if item.get("allowed_api_keys"):
        return False
    if require_download:
        # Не пытаемся скачивать то, что сам Audius не помечает доступным для загрузки.
        if item.get("is_downloadable") is not True or item.get("is_download_gated") is True:
            return False
        if item.get("download_conditions") not in (None, {}, [], ""):
            return False
    else:
        if item.get("is_streamable") is not True or item.get("is_stream_gated") is True:
            return False
        if item.get("stream_conditions") not in (None, {}, [], ""):
            return False
    return True


def audius_candidate(item):
    if not isinstance(item, dict):
        return None
    track_id = str(item.get("id") or "").strip()
    title = str(item.get("title") or "").strip()
    permalink = str(item.get("permalink") or "").strip()
    parsed = urlparse(permalink)
    host = (parsed.hostname or "").lower()
    if not track_id or not title or parsed.scheme != "https" or not (host == "audius.co" or host.endswith(".audius.co")):
        return None
    try:
        duration = int(float(item.get("duration"))) if item.get("duration") is not None else None
    except (TypeError, ValueError):
        duration = None
    return {
        "url": permalink,
        "title": title[:180],
        "uploader": audius_artist_name(item),
        "thumbnail": audius_artwork_url(item),
        "duration": duration,
        "source": "audius",
        "audius_id": track_id,
    }


def _audius_search_raw(query, count=40):
    payload = audius_api_json("/tracks/search", {"query": query, "limit": min(50, max(1, int(count)))})
    entries = payload.get("data") or []
    if isinstance(entries, dict):
        entries = entries.get("tracks") or []
    return [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []


def _audius_flat_search(query, count=30, require_download=True):
    try:
        raw_items = _audius_search_raw(query, count)
    except Exception as e:
        print(f"Audius: поиск по запросу «{query}» не удался: {e!r}")
        return []
    tracks = []
    for item in raw_items:
        if not audius_track_allowed(item, require_download=require_download):
            continue
        candidate = audius_candidate(item)
        if candidate:
            tracks.append(candidate)
    return tracks


def _clean_match_value(value):
    tokens = [token for token in norm_text(value).split() if token not in NOISE_TOKENS]
    return " ".join(tokens)


def find_audius_match(track, require_download=True):
    """Ищет совпадение в Audius и не пропускает закрытые для загрузки/стриминга версии."""
    from difflib import SequenceMatcher

    if not isinstance(track, dict):
        return None
    original_title = str(track.get("title") or "").strip()
    original_artist = str(track.get("uploader") or track.get("artist") or "").strip()
    if not original_title:
        return None
    parsed_artist, clean_title = split_artist_title(original_title, original_artist)
    wanted_title = clean_title or original_title
    wanted_artist = parsed_artist or original_artist

    queries = []
    for value in (f"{wanted_artist} {wanted_title}".strip(), wanted_title, *query_variants(wanted_title)):
        value = " ".join(str(value or "").split())
        if value and value.casefold() not in {q.casefold() for q in queries}:
            queries.append(value)
    queries = queries[:4]

    raw_items = []
    seen_ids = set()
    for query in queries:
        try:
            entries = _audius_search_raw(query, 50)
        except Exception as e:
            print(f"Audius: резервный поиск по «{query}» не удался: {e!r}")
            continue
        for item in entries:
            item_id = str(item.get("id") or "")
            if item_id and item_id not in seen_ids:
                seen_ids.add(item_id)
                raw_items.append(item)

    target_title = _clean_match_value(wanted_title)
    target_artist = _clean_match_value(wanted_artist)
    try:
        target_duration = int(float(track.get("duration"))) if track.get("duration") is not None else None
    except (TypeError, ValueError):
        target_duration = None

    ranked = []
    for item in raw_items:
        if not audius_track_allowed(item, require_download=require_download):
            continue
        candidate = audius_candidate(item)
        if not candidate:
            continue
        candidate_title = _clean_match_value(candidate["title"])
        candidate_artist = _clean_match_value(candidate["uploader"])
        title_ratio = SequenceMatcher(None, target_title, candidate_title).ratio() if target_title else 0.0
        artist_ratio = SequenceMatcher(None, target_artist, candidate_artist).ratio() if target_artist and candidate_artist else 0.0
        target_tokens = set(target_title.split())
        candidate_tokens = set(candidate_title.split())
        token_ratio = len(target_tokens & candidate_tokens) / max(1, len(target_tokens | candidate_tokens))
        title_score = title_ratio * 0.75 + token_ratio * 0.25

        # Не принимаем просто случайно похожие названия; артист учитывается, но не требует буквального совпадения.
        if title_score < 0.68:
            continue
        if target_artist and candidate_artist and artist_ratio < 0.25 and title_score < 0.88:
            continue

        duration_penalty = 0.0
        candidate_duration = candidate.get("duration")
        if target_duration and candidate_duration:
            difference_ratio = abs(target_duration - candidate_duration) / max(1, target_duration)
            if difference_ratio > 0.45 and title_score < 0.94:
                continue
            duration_penalty = min(25.0, difference_ratio * 30.0)

        score = title_score * 100 + artist_ratio * 22 - duration_penalty
        ranked.append((score, candidate))

    if not ranked:
        return None
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = ranked[0]
    if best_score < 70:
        return None
    return best


def _get_audius_track_details(track_id):
    payload = audius_api_json(f"/tracks/{track_id}")
    item = _audius_data_track(payload)
    if not isinstance(item, dict):
        raise RuntimeError("Audius не вернул данные трека")
    return item


def download_audius_track(track, workdir):
    track_id = str((track or {}).get("audius_id") or "").strip()
    if not track_id:
        raise RuntimeError("Не удалось определить ID трека Audius")
    raw = (track or {}).get("audius_raw")
    if not isinstance(raw, dict):
        raw = _get_audius_track_details(track_id)
    if not audius_track_allowed(raw, require_download=True):
        raise RuntimeError("Audius не разрешает скачивание этой версии трека")

    url = f"{AUDIUS_API_BASE}/tracks/{track_id}/download"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SoundMusicTelegramBot/1.0", "Accept": "audio/*,application/octet-stream;q=0.9,*/*;q=0.5"},
    )
    filename = os.path.join(workdir, f"audius_{re.sub(r'[^a-zA-Z0-9_-]+', '_', track_id)[:60]}.mp3")
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit() and int(content_length) > AUDIUS_MAX_FILE_BYTES:
                raise RuntimeError("Файл Audius слишком большой")
            first_chunk = response.read(2048)
            if ("json" in content_type or "text/html" in content_type
                    or first_chunk.lstrip().startswith((b"{", b"[", b"<!DOCTYPE", b"<html"))):
                raise RuntimeError("Audius не выдал аудиофайл для этой версии")
            extension = ".mp3"
            disposition = response.headers.get("Content-Disposition") or ""
            match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
            if match:
                ext = os.path.splitext(match.group(1).strip())[1].lower()
                if re.fullmatch(r"\.[a-z0-9]{1,6}", ext):
                    extension = ext
            if "audio/mp4" in content_type:
                extension = ".m4a"
            elif "audio/ogg" in content_type:
                extension = ".ogg"
            elif "audio/flac" in content_type:
                extension = ".flac"
            elif "audio/wav" in content_type or "audio/x-wav" in content_type:
                extension = ".wav"
            filename = os.path.splitext(filename)[0] + extension
            with open(filename, "wb") as output:
                if first_chunk:
                    output.write(first_chunk)
                    written += len(first_chunk)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > AUDIUS_MAX_FILE_BYTES:
                        raise RuntimeError("Файл Audius превысил допустимый размер")
                    output.write(chunk)
        if not os.path.exists(filename) or os.path.getsize(filename) < 1024:
            raise RuntimeError("Audius вернул пустой или неполный аудиофайл")
    except Exception:
        try:
            if os.path.exists(filename):
                os.remove(filename)
        except OSError:
            pass
        raise

    artist = audius_artist_name(raw)
    info = {
        "id": track_id,
        "title": str(raw.get("title") or track.get("title") or "Без названия")[:180],
        "uploader": artist,
        "duration": raw.get("duration"),
        "thumbnail": audius_artwork_url(raw),
        "webpage_url": str(raw.get("permalink") or track.get("url") or "https://audius.co"),
        "source": "audius",
        "copyright_line": str(raw.get("copyright_line") or raw.get("copyrightLine") or f"Автор: {artist}")[:250],
    }
    return info, filename


# --- ДОПОЛНИТЕЛЬНЫЙ ИСТОЧНИК: INTERNET ARCHIVE ---
# В поиск попадают только записи с явной открытой лицензией, допускающей коммерческое
# распространение неизменённого файла: CC0 и CC BY. Метаданные лицензии
# размещаются загрузчиком и не являются независимой юридической проверкой прав.
INTERNET_ARCHIVE_BASE = "https://archive.org"
INTERNET_ARCHIVE_TIMEOUT = 18
INTERNET_ARCHIVE_MAX_FILE_BYTES = 100 * 1024 * 1024
INTERNET_ARCHIVE_CACHE_TTL = 300
INTERNET_ARCHIVE_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"}
INTERNET_ARCHIVE_ALLOWED_LICENSES = {
    "publicdomain/zero/1.0",
    "licenses/by/2.0",
    "licenses/by/2.5",
    "licenses/by/3.0",
    "licenses/by/4.0",
}
internet_archive_metadata_cache = {}
internet_archive_metadata_lock = threading.Lock()


def _internet_archive_text(value, default=""):
    if isinstance(value, (list, tuple)):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
        return default
    if isinstance(value, dict):
        value = value.get("name") or value.get("value") or ""
    text = str(value or "").strip()
    return text or default


def internet_archive_license_allowed(license_url):
    """True only for explicit CC0 / CC BY licenses; NC, ND and SA are excluded."""
    parsed = urlparse(str(license_url or "").strip())
    host = (parsed.hostname or "").lower()
    license_path = parsed.path.strip("/").lower()
    return (
        parsed.scheme in ("http", "https")
        and host in ("creativecommons.org", "www.creativecommons.org")
        and license_path in INTERNET_ARCHIVE_ALLOWED_LICENSES
    )


def internet_archive_json(path, params=None, max_bytes=8 * 1024 * 1024):
    url = f"{INTERNET_ARCHIVE_BASE}/{str(path).lstrip('/')}"
    if params:
        url += "?" + urlencode(params, doseq=True)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SoundMusicTelegramBot/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=INTERNET_ARCHIVE_TIMEOUT) as response:
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise RuntimeError("Internet Archive вернул слишком большой ответ")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Internet Archive вернул некорректный ответ")
    return payload


def internet_archive_metadata(identifier, use_cache=True):
    identifier = str(identifier or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", identifier):
        raise RuntimeError("Некорректный идентификатор Internet Archive")

    now = time.time()
    if use_cache:
        with internet_archive_metadata_lock:
            cached = internet_archive_metadata_cache.get(identifier)
            if cached and now - cached[0] < INTERNET_ARCHIVE_CACHE_TTL:
                return cached[1]

    payload = internet_archive_json(f"metadata/{quote(identifier, safe='')}")
    if not isinstance(payload.get("metadata"), dict) or not isinstance(payload.get("files"), list):
        raise RuntimeError("Internet Archive не вернул метаданные файлов")

    with internet_archive_metadata_lock:
        internet_archive_metadata_cache[identifier] = (now, payload)
        if len(internet_archive_metadata_cache) > 250:
            oldest = min(internet_archive_metadata_cache, key=lambda key: internet_archive_metadata_cache[key][0])
            internet_archive_metadata_cache.pop(oldest, None)
    return payload


def internet_archive_pick_audio_file(files):
    if not isinstance(files, list):
        return None
    ranked = []
    extension_scores = {
        ".mp3": 100, ".m4a": 90, ".aac": 85, ".ogg": 80,
        ".opus": 78, ".flac": 75, ".wav": 60,
    }
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or "/" in name or "\\" in name or name.startswith("."):
            continue
        if _internet_archive_flag_is_true(item.get("private")):
            continue
        extension = os.path.splitext(name)[1].lower()
        if extension not in INTERNET_ARCHIVE_AUDIO_EXTENSIONS:
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > INTERNET_ARCHIVE_MAX_FILE_BYTES:
            continue
        if any(part in name.lower() for part in ("_thumb", "thumbnail", "sample")):
            continue
        ranked.append((extension_scores.get(extension, 0), size, item))
    if not ranked:
        return None
    # MP3/M4A получают приоритет; при одинаковом формате берём самый качественный по размеру.
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return ranked[0][2]


def _internet_archive_flag_is_true(value):
    return value is True or str(value or "").strip().lower() in ("1", "true", "yes")


def internet_archive_item_allowed(payload):
    if not isinstance(payload, dict):
        return False
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return False
    if _internet_archive_text(metadata.get("mediatype")).lower() != "audio":
        return False
    if _internet_archive_flag_is_true(metadata.get("access-restricted-item")) or _internet_archive_flag_is_true(metadata.get("is_dark")):
        return False
    license_url = _internet_archive_text(metadata.get("licenseurl"))
    if not internet_archive_license_allowed(license_url):
        return False
    rights = _internet_archive_text(metadata.get("rights")).casefold()
    if any(term in rights for term in ("all rights reserved", "no redistribution", "not for redistribution")):
        return False
    return internet_archive_pick_audio_file(payload.get("files")) is not None


def _internet_archive_duration(value):
    value = _internet_archive_text(value)
    if not value:
        return None
    try:
        if ":" in value:
            parts = [float(part) for part in value.split(":")]
            seconds = 0.0
            for part in parts:
                seconds = seconds * 60 + part
        else:
            seconds = float(value)
        return int(seconds) if 0 < seconds < 24 * 60 * 60 else None
    except (TypeError, ValueError):
        return None


def internet_archive_candidate(identifier, payload):
    if not internet_archive_item_allowed(payload):
        return None
    metadata = payload["metadata"]
    audio_file = internet_archive_pick_audio_file(payload.get("files"))
    if not audio_file:
        return None
    identifier = str(metadata.get("identifier") or identifier or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", identifier):
        return None
    license_url = _internet_archive_text(metadata.get("licenseurl"))
    title = _internet_archive_text(metadata.get("title"), "Без названия")
    artist = _internet_archive_text(
        metadata.get("creator") or metadata.get("artist") or metadata.get("contributor"),
        "Неизвестный автор",
    )
    duration = _internet_archive_duration(metadata.get("runtime") or audio_file.get("length"))
    return {
        "url": f"{INTERNET_ARCHIVE_BASE}/details/{identifier}",
        "title": title[:180],
        "uploader": artist[:120],
        "thumbnail": f"{INTERNET_ARCHIVE_BASE}/services/img/{quote(identifier, safe='')}",
        "duration": duration,
        "source": "internet_archive",
        "ia_identifier": identifier,
        "ia_file_name": str(audio_file.get("name"))[:240],
        "license_url": license_url[:500],
    }


def _internet_archive_search_raw(query, count=30):
    cleaned = re.sub(r"[^\w\s&'’-]", " ", str(query or ""), flags=re.UNICODE)
    cleaned = " ".join(cleaned.split())[:120]
    if not cleaned:
        return []
    # Поиск идёт только среди audio items. Права всё равно перепроверяются по полным метаданным.
    search_expression = f'mediatype:audio AND "{cleaned.replace(chr(34), " ")}"'
    params = [
        ("q", search_expression),
        ("fl[]", "identifier"),
        ("fl[]", "title"),
        ("fl[]", "creator"),
        ("fl[]", "licenseurl"),
        ("rows", str(min(50, max(1, int(count))))),
        ("page", "1"),
        ("output", "json"),
    ]
    payload = internet_archive_json("advancedsearch.php", params)
    response = payload.get("response") or {}
    docs = response.get("docs") or []
    return [doc for doc in docs if isinstance(doc, dict) and doc.get("identifier")] if isinstance(docs, list) else []


def _internet_archive_flat_search(query, count=20):
    try:
        docs = _internet_archive_search_raw(query, count)
    except Exception as e:
        print(f"Internet Archive: поиск по запросу «{query}» не удался: {e!r}")
        return []
    identifiers = list(dict.fromkeys(str(doc.get("identifier") or "") for doc in docs))
    identifiers = [value for value in identifiers if re.fullmatch(r"[A-Za-z0-9._-]{1,120}", value)]
    if not identifiers:
        return []

    found = []
    def load_candidate(identifier):
        try:
            payload = internet_archive_metadata(identifier)
            return internet_archive_candidate(identifier, payload)
        except Exception as e:
            print(f"Internet Archive: не удалось проверить «{identifier}»: {e!r}")
            return None

    with ThreadPoolExecutor(max_workers=min(5, len(identifiers))) as pool:
        for candidate in pool.map(load_candidate, identifiers[:min(30, len(identifiers))]):
            if candidate:
                found.append(candidate)
    return found


def find_internet_archive_match(track):
    """Ищет достаточно точное совпадение в Internet Archive среди записей с открытой лицензией."""
    from difflib import SequenceMatcher

    if not isinstance(track, dict):
        return None
    original_title = str(track.get("title") or "").strip()
    original_artist = str(track.get("uploader") or track.get("artist") or "").strip()
    if not original_title:
        return None
    parsed_artist, clean_title = split_artist_title(original_title, original_artist)
    wanted_title = _clean_match_value(clean_title or original_title)
    wanted_artist = _clean_match_value(parsed_artist or original_artist)
    queries = []
    for value in (f"{wanted_artist} {wanted_title}".strip(), wanted_title, *query_variants(wanted_title)):
        value = " ".join(str(value or "").split())
        if value and value.casefold() not in {item.casefold() for item in queries}:
            queries.append(value)
    candidates = []
    seen = set()
    for query in queries[:2]:
        for candidate in _internet_archive_flat_search(query, 15):
            identifier = candidate.get("ia_identifier")
            if identifier and identifier not in seen:
                seen.add(identifier)
                candidates.append(candidate)

    try:
        wanted_duration = int(float(track.get("duration"))) if track.get("duration") is not None else None
    except (TypeError, ValueError):
        wanted_duration = None

    ranked = []
    for candidate in candidates:
        candidate_title = _clean_match_value(candidate.get("title"))
        candidate_artist = _clean_match_value(candidate.get("uploader"))
        title_ratio = SequenceMatcher(None, wanted_title, candidate_title).ratio() if wanted_title and candidate_title else 0.0
        title_tokens = set(wanted_title.split())
        candidate_tokens = set(candidate_title.split())
        token_ratio = len(title_tokens & candidate_tokens) / max(1, len(title_tokens | candidate_tokens))
        title_score = title_ratio * 0.75 + token_ratio * 0.25
        artist_ratio = SequenceMatcher(None, wanted_artist, candidate_artist).ratio() if wanted_artist and candidate_artist else 0.0
        if title_score < 0.76:
            continue
        if wanted_artist and candidate_artist and artist_ratio < 0.25 and title_score < 0.92:
            continue
        try:
            candidate_duration = int(candidate.get("duration")) if candidate.get("duration") is not None else None
        except (TypeError, ValueError):
            candidate_duration = None
        duration_penalty = 0.0
        if wanted_duration and candidate_duration:
            difference = abs(wanted_duration - candidate_duration) / max(1, wanted_duration)
            if difference > 0.40 and title_score < 0.94:
                continue
            duration_penalty = min(25.0, difference * 30.0)
        score = title_score * 100 + artist_ratio * 20 - duration_penalty
        ranked.append((score, candidate))
    if not ranked:
        return None
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return ranked[0][1] if ranked[0][0] >= 76 else None


def download_internet_archive_track(track, workdir):
    identifier = str((track or {}).get("ia_identifier") or "").strip()
    if not identifier:
        parsed = urlparse(str((track or {}).get("url") or ""))
        parts = [part for part in parsed.path.split("/") if part]
        identifier = parts[1] if len(parts) >= 2 and parts[0] == "details" else ""
    payload = internet_archive_metadata(identifier, use_cache=False)
    candidate = internet_archive_candidate(identifier, payload)
    if not candidate:
        raise RuntimeError("Internet Archive не подтвердил открытую лицензию или доступный аудиофайл")

    requested_name = str((track or {}).get("ia_file_name") or "").strip()
    audio_file = None
    if requested_name:
        audio_file = next(
            (item for item in payload.get("files", [])
             if isinstance(item, dict) and item.get("name") == requested_name
             and item.get("name") == candidate.get("ia_file_name")),
            None,
        )
    if not audio_file:
        audio_file = internet_archive_pick_audio_file(payload.get("files"))
    if not audio_file:
        raise RuntimeError("В Internet Archive не найден подходящий аудиофайл")

    file_name = str(audio_file.get("name") or "")
    if not file_name or "/" in file_name or "\\" in file_name:
        raise RuntimeError("Некорректное имя файла Internet Archive")
    extension = os.path.splitext(file_name)[1].lower()
    if extension not in INTERNET_ARCHIVE_AUDIO_EXTENSIONS:
        raise RuntimeError("Формат файла Internet Archive не поддерживается")
    url = (
        f"{INTERNET_ARCHIVE_BASE}/download/{quote(identifier, safe='')}/"
        f"{quote(file_name, safe='')}"
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SoundMusicTelegramBot/1.0", "Accept": "audio/*,application/octet-stream;q=0.9,*/*;q=0.5"},
    )
    filename = os.path.join(workdir, f"internet_archive_{identifier[:50]}{extension}")
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit() and int(content_length) > INTERNET_ARCHIVE_MAX_FILE_BYTES:
                raise RuntimeError("Файл Internet Archive слишком большой")
            first_chunk = response.read(2048)
            if ("text/html" in content_type or "json" in content_type
                    or first_chunk.lstrip().startswith((b"{", b"[", b"<!DOCTYPE", b"<html"))):
                raise RuntimeError("Internet Archive не выдал аудиофайл")
            with open(filename, "wb") as output:
                if first_chunk:
                    output.write(first_chunk)
                    written += len(first_chunk)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > INTERNET_ARCHIVE_MAX_FILE_BYTES:
                        raise RuntimeError("Файл Internet Archive превысил допустимый размер")
                    output.write(chunk)
        if not os.path.exists(filename) or os.path.getsize(filename) < 1024:
            raise RuntimeError("Internet Archive вернул пустой или неполный аудиофайл")
    except Exception:
        try:
            if os.path.exists(filename):
                os.remove(filename)
        except OSError:
            pass
        raise

    metadata = payload.get("metadata") or {}
    artist = candidate.get("uploader") or "Неизвестный автор"
    info = {
        "id": identifier,
        "title": candidate.get("title") or "Без названия",
        "uploader": artist,
        "duration": candidate.get("duration"),
        "thumbnail": candidate.get("thumbnail"),
        "webpage_url": candidate.get("url"),
        "source": "internet_archive",
        "license_url": candidate.get("license_url"),
        "copyright_line": f"Автор: {artist}; лицензия: {candidate.get('license_url', '')}"[:400],
    }
    return info, filename


def get_internet_archive_stream_url(identifier, expected_track=None):
    payload = internet_archive_metadata(identifier, use_cache=False)
    candidate = internet_archive_candidate(identifier, payload)
    if not candidate:
        raise RuntimeError("Internet Archive не подтвердил открытую лицензию для воспроизведения")
    _validate_stream_metadata(expected_track, candidate, "Internet Archive")
    file_name = candidate.get("ia_file_name")
    if not file_name:
        raise RuntimeError("Internet Archive не вернул аудиофайл")
    return (
        f"{INTERNET_ARCHIVE_BASE}/download/{quote(identifier, safe='')}/"
        f"{quote(file_name, safe='')}"
    )


def prepare_internet_archive_track(track, workdir, effect="none"):
    info, audio_filename = download_internet_archive_track(track, workdir)
    _original_thumb, thumbnail_filename = get_track_thumbnail(info, audio_filename)
    if effect != "none":
        audio_filename = apply_audio_effect(audio_filename, effect)
    else:
        audio_filename = ensure_telegram_audio(audio_filename)
    audio_filename = compress_audio_if_needed(audio_filename, thumbnail_filename)
    return info, audio_filename, thumbnail_filename


def audius_attribution_caption(info, max_length=900):
    if not isinstance(info, dict):
        return None
    source = str(info.get("source", "")).lower()
    artist = str(info.get("uploader") or "Неизвестный автор").strip()
    permalink = str(info.get("webpage_url") or "").strip()
    if source == "audius":
        permalink = permalink or "https://audius.co"
        return f"Источник: Audius\nИсполнитель: {artist}\nОригинал: {permalink}"[:max_length]
    if source == "internet_archive":
        license_url = str(info.get("license_url") or "").strip()
        permalink = permalink or "https://archive.org"
        return (
            f"Источник: Internet Archive\nАвтор: {artist}\n"
            f"Лицензия: {license_url}\nОригинал: {permalink}"
        )[:max_length]
    return None


def prepare_audius_track(track, workdir, effect="none"):
    info, audio_filename = download_audius_track(track, workdir)
    _original_thumb, thumbnail_filename = get_track_thumbnail(info, audio_filename)
    if effect != "none":
        audio_filename = apply_audio_effect(audio_filename, effect)
    else:
        audio_filename = ensure_telegram_audio(audio_filename)
    audio_filename = compress_audio_if_needed(audio_filename, thumbnail_filename)
    return info, audio_filename, thumbnail_filename


def prepare_track_with_fallback(track, workdir, effect="none"):
    """Пробует исходный источник, затем Audius и Internet Archive с проверкой лицензии."""
    source = str((track or {}).get("source") or "soundcloud").lower()
    errors = []

    if source == "soundcloud":
        try:
            return prepare_track_file(track["url"], workdir, effect)
        except Exception as e:
            errors.append(f"SoundCloud: {e}")
            print(f"SoundCloud не смог скачать «{track.get('title', '')}»: {e!r}")
    elif source == "audius":
        try:
            return prepare_audius_track(track, workdir, effect)
        except Exception as e:
            errors.append(f"Audius: {e}")
            print(f"Audius не смог скачать «{track.get('title', '')}»: {e!r}")
    elif source == "internet_archive":
        try:
            return prepare_internet_archive_track(track, workdir, effect)
        except Exception as e:
            errors.append(f"Internet Archive: {e}")
            print(f"Internet Archive не смог скачать «{track.get('title', '')}»: {e!r}")
    else:
        errors.append(f"Неизвестный источник: {source}")

    if source != "audius":
        try:
            candidate = find_audius_match(track, require_download=True)
            if candidate:
                print(f"Audius: найден доступный вариант «{candidate['title']}» — {candidate['uploader']}")
                return prepare_audius_track(candidate, workdir, effect)
            errors.append("Audius: точное совпадение с разрешённым скачиванием не найдено")
        except Exception as e:
            errors.append(f"Audius: {e}")
            print(f"Резервный поиск Audius завершился ошибкой: {e!r}")

    if source != "internet_archive":
        try:
            candidate = find_internet_archive_match(track)
            if candidate:
                print(
                    f"Internet Archive: найден вариант «{candidate['title']}» — "
                    f"{candidate['uploader']} ({candidate.get('license_url', '')})"
                )
                return prepare_internet_archive_track(candidate, workdir, effect)
            errors.append("Internet Archive: точное совпадение с подходящей открытой лицензией не найдено")
        except Exception as e:
            errors.append(f"Internet Archive: {e}")
            print(f"Резервный поиск Internet Archive завершился ошибкой: {e!r}")

    raise RuntimeError("Не удалось получить аудио из доступных источников. " + " | ".join(errors[-3:]))



def _fallback_track_match_score(reference, candidate):
    """Оценивает, насколько кандидат похож на выбранный трек; слабые совпадения отбрасываются."""
    from difflib import SequenceMatcher

    if not isinstance(reference, dict) or not isinstance(candidate, dict):
        return None
    ref_title = str(reference.get("title") or "").strip()
    candidate_title = str(candidate.get("title") or "").strip()
    if not ref_title or not candidate_title:
        return None

    ref_artist, ref_song = split_artist_title(ref_title, str(reference.get("uploader") or ""))
    candidate_artist, candidate_song = split_artist_title(
        candidate_title, str(candidate.get("uploader") or "")
    )
    ref_song = _clean_match_value(ref_song or ref_title)
    candidate_song = _clean_match_value(candidate_song or candidate_title)
    if not ref_song or not candidate_song:
        return None

    title_ratio = SequenceMatcher(None, ref_song, candidate_song).ratio()
    ref_tokens = set(ref_song.split())
    candidate_tokens = set(candidate_song.split())
    coverage = len(ref_tokens & candidate_tokens) / max(1, len(ref_tokens))
    exact_title = ref_song == candidate_song

    ref_artist = _clean_match_value(ref_artist or reference.get("uploader") or "")
    candidate_artist = _clean_match_value(candidate_artist or candidate.get("uploader") or "")
    artist_ratio = (
        SequenceMatcher(None, ref_artist, candidate_artist).ratio()
        if ref_artist and candidate_artist else 0.0
    )

    def duration_of(item):
        try:
            value = int(float(item.get("duration")))
            return value if value > 0 else None
        except (TypeError, ValueError):
            return None

    ref_duration = duration_of(reference)
    candidate_duration = duration_of(candidate)
    duration_penalty = 0.0
    if ref_duration and candidate_duration:
        difference = abs(ref_duration - candidate_duration) / max(ref_duration, candidate_duration)
        # Значительно отличающиеся по длительности версии не считаем одной песней.
        if difference > 0.35 and not (exact_title and difference <= 0.50):
            return None
        duration_penalty = min(25.0, difference * 35.0)

    # Для неидентичных названий требуется сильное текстовое совпадение.
    if not exact_title and (title_ratio < 0.86 or coverage < 0.75):
        return None
    # Если оба артиста известны и сильно отличаются, принимаем только точное название.
    if ref_artist and candidate_artist and artist_ratio < 0.25 and not exact_title:
        return None

    return title_ratio * 100 + coverage * 12 + artist_ratio * 8 - duration_penalty


def find_related_fallback_tracks(reference, candidates=None, max_candidates=3):
    """Выбирает только близкие результаты; при отсутствии списка делает свежий поиск по названию."""
    source_candidates = list(candidates or [])
    ranked = []
    seen_urls = {str((reference or {}).get("url") or "").split("?")[0].rstrip("/")}

    def add_ranked(items):
        for item in items or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").split("?")[0].rstrip("/")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            score = _fallback_track_match_score(reference, item)
            if score is not None:
                ranked.append((score, item))

    add_ranked(source_candidates)

    # Mini App передаёт выбранный трек, но не всю выдачу. В таком случае ищем ещё раз
    # по точному названию, чтобы использовать тот же общий поиск по источникам.
    if candidates is None or len(ranked) < max_candidates:
        title = str((reference or {}).get("title") or "").strip()
        artist, song = split_artist_title(title, str((reference or {}).get("uploader") or ""))
        queries = []
        for query in (" ".join(part for part in (artist, song or title) if part).strip(), song or title):
            query = " ".join(str(query or "").split())
            if query and query.casefold() not in {item.casefold() for item in queries}:
                queries.append(query)
        for query in queries[:2]:
            try:
                fresh = soundcloud_search(query, 25)
                add_ranked(fresh)
            except Exception as e:
                print(f"Резервный поиск похожих версий по «{query}» не удался: {e!r}")

    ranked.sort(key=lambda row: row[0], reverse=True)
    return [item for _, item in ranked[:max_candidates]]


def prepare_track_with_alternates(track, workdir, effect="none", fallback_tracks=None, max_attempts=4):
    """Сначала пробует выбранную запись; похожие варианты ищет только при её неудаче."""
    attempts = [(track, False)]
    errors = []
    candidate_dir = os.path.join(workdir, "candidate_1")
    os.makedirs(candidate_dir, exist_ok=True)
    try:
        info, audio_filename, thumbnail_filename = prepare_track_with_fallback(
            track, candidate_dir, effect
        )
        return track, info, audio_filename, thumbnail_filename
    except Exception as e:
        errors.append(f"выбранный вариант: {e}")
        print(f"Выбранный вариант «{track.get('title', '')}» не скачался: {e!r}")

    related = find_related_fallback_tracks(
        track,
        candidates=fallback_tracks,
        max_candidates=max(0, min(5, int(max_attempts) - 1)),
    )
    for index, candidate in enumerate(related, start=2):
        candidate_dir = os.path.join(workdir, f"candidate_{index}")
        os.makedirs(candidate_dir, exist_ok=True)
        try:
            info, audio_filename, thumbnail_filename = prepare_track_with_fallback(
                candidate, candidate_dir, effect
            )
            return candidate, info, audio_filename, thumbnail_filename
        except Exception as e:
            label = str(candidate.get("source") or "неизвестный источник")
            errors.append(f"похожий вариант {index - 1} ({label}): {e}")
            print(
                f"Не удалось подготовить похожий вариант {index - 1} "
                f"«{candidate.get('title', '')}»: {e!r}"
            )

    details = " | ".join(errors[-4:])
    raise RuntimeError(
        "Не удалось скачать выбранный трек или похожие доступные версии. " + details
    )


def _sc_flat_search(query, count):
    ydl_opts = {
        "extract_flat": True, "quiet": True, "no_warnings": True,
        "socket_timeout": 10, "retries": 1, "extractor_retries": 1,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(f"scsearch{count}:{query}", download=False)
    tracks = []
    for item in ((result or {}).get("entries") or []):
        if not item:
            continue
        url = entry_url(item)
        if not url:
            continue
        normalized = normalize_miniapp_track({
            "url": url,
            "title": item.get("title"),
            "uploader": item.get("uploader") or item.get("artist"),
            "thumbnail": best_soundcloud_thumbnail(item),
            "duration": item.get("duration"),
        })
        if normalized:
            tracks.append(normalized)
    return tracks


def soundcloud_search(query, count=30, search_type="track"):
    """Объединяет источники и ранжирует выдачу; для треков фильтрует слабые совпадения."""
    query = " ".join((query or "").split())
    if not query:
        return []

    variants = query_variants(query)
    queries = [query] + variants
    raw_tracks = []
    errors = []
    jobs = []
    with ThreadPoolExecutor(max_workers=min(10, len(queries) * 2 + 3)) as pool:
        for value in queries:
            jobs.append((value, "soundcloud", pool.submit(_sc_flat_search, value, count)))
        for value in queries[:3]:
            jobs.append((value, "audius", pool.submit(_audius_flat_search, value, min(count, 50), True)))
        # Internet Archive ищем по исходной фразе, чтобы не перегружать API.
        jobs.append((queries[0], "internet_archive", pool.submit(_internet_archive_flat_search, queries[0], min(count, 15))))
        for value, source_name, future in jobs:
            try:
                raw_tracks.extend(future.result() or [])
            except Exception as e:
                errors.append(e)
                print(f"Поиск {source_name} по запросу «{value}» не удался: {e!r}")

    merged = dedupe_tracks(raw_tracks)
    scored = score_tracks(merged, queries)
    if not scored and errors:
        raise RuntimeError("Поиск не удался ни в SoundCloud, ни в Audius, ни в Internet Archive") from errors[0]

    if str(search_type).lower() != "author":
        scored = [
            (score, track) for score, track in scored
            if _track_matches_track_query(track, query)
        ]
        scored = dedupe_ranked_titles(scored)

    return [track for _, track in scored]


def cached_soundcloud_search(query, count=30, search_type="track"):
    """Общий TTL-кэш для поиска в чате, inline-режиме и Mini App."""
    normalized_query = norm_text(query)
    count = max(1, min(100, int(count)))
    search_type = "author" if str(search_type).lower() == "author" else "track"
    cache_key = (normalized_query, count, search_type)
    now = time.time()

    with search_cache_lock:
        cached = search_results_cache.get(cache_key)
        if cached and now - cached[0] < SEARCH_CACHE_TTL:
            return cached[1]
        if cached:
            search_results_cache.pop(cache_key, None)

    tracks = soundcloud_search(query, count, search_type=search_type)
    with search_cache_lock:
        search_results_cache[cache_key] = (time.time(), tracks)
        while len(search_results_cache) > 100:
            oldest_key = min(search_results_cache, key=lambda key: search_results_cache[key][0])
            search_results_cache.pop(oldest_key, None)
    return tracks




# ============================================================
#             ЕДИНЫЙ ПОИСК: НАЗВАНИЕ / СЛОВА / ВИДЕО
# ============================================================
VIDEO_SEARCH_HOSTS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
    "x.com", "twitter.com", "vimeo.com", "dailymotion.com",
    "rutube.ru", "vk.com", "ok.ru", "twitch.tv"
)
VIDEO_SEARCH_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
VIDEO_QUERY_CACHE = {}
VIDEO_QUERY_CACHE_LOCK = threading.Lock()
VIDEO_QUERY_CACHE_TTL = 900


def extract_video_search_url(value):
    """Возвращает ссылку на поддерживаемый видеосервис, если она есть в запросе."""
    for candidate in VIDEO_SEARCH_URL_RE.findall(str(value or "")):
        candidate = candidate.rstrip(".,!?;:)]}>")
        try:
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme not in ("http", "https") or not host:
                continue
            if any(host == domain or host.endswith("." + domain) for domain in VIDEO_SEARCH_HOSTS):
                return candidate
        except Exception:
            continue
    return ""


class _SupportedVideoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Не разрешает короткой ссылке перенаправить запрос на посторонний домен."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        if not extract_video_search_url(target):
            raise urllib.error.HTTPError(
                req.full_url, code, "Перенаправление на неподдерживаемый адрес заблокировано", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, target)


def _expand_short_video_url(video_url):
    """Раскрывает короткие TikTok-ссылки вида vt.tiktok.com/... в адрес самого ролика."""
    original = str(video_url or "").strip()
    try:
        parsed = urlparse(original)
        host = (parsed.hostname or "").lower().rstrip(".")
    except Exception:
        return original

    if host not in {"vt.tiktok.com", "vm.tiktok.com", "t.tiktok.com"}:
        return original

    request = urllib.request.Request(
        original,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.tiktok.com/",
            "Range": "bytes=0-0",
        },
    )
    try:
        opener = urllib.request.build_opener(_SupportedVideoRedirectHandler())
        with opener.open(request, timeout=8) as response:
            final_url = response.geturl()
        if extract_video_search_url(final_url):
            print(f"TikTok short link expanded: {original} -> {final_url}")
            return final_url
    except Exception as e:
        # Не обрываем поиск: yt-dlp иногда сам умеет обработать короткую ссылку.
        print(f"Не удалось раскрыть короткую TikTok-ссылку {original}: {e!r}")
    return original


def _shazam_result_query(result):
    """Извлекает подтверждённые Shazam название и исполнителя из ответа."""
    track = result.get("track") if isinstance(result, dict) else getattr(result, "track", None)
    if not track:
        return ""

    def field(obj, name):
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)

    title = str(field(track, "title") or "").strip()
    artist = str(field(track, "subtitle") or field(track, "artist") or "").strip()
    if not title or _is_generic_music_label(title):
        return ""
    return f"{artist} - {title}".strip(" -") if artist else title


def _recognize_video_audio_query(video_url):
    """Быстро распознаёт один 12-секундный фрагмент аудио видео через Shazam."""
    if Shazam is None:
        print("Shazam не установлен — перехожу к музыкальным метаданным видео")
        return ""

    expanded_url = _expand_short_video_url(video_url)
    workdir = tempfile.mkdtemp(prefix="video_music_recognition_")
    sample_path = None
    try:
        # Не запускаем длинные повторы: TikTok иногда зависает на выдаче метаданных.
        probe_options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 10,
            "retries": 0,
            "extractor_retries": 0,
            "fragment_retries": 0,
            "playlistend": 1,
        }
        print("🎵 TikTok: получаю информацию о видео для распознавания звука...")
        with yt_dlp.YoutubeDL(probe_options) as ydl:
            info = ydl.extract_info(expanded_url, download=False)
        if not isinstance(info, dict):
            return ""

        try:
            duration = float(info.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration and duration < 5:
            print("Видео слишком короткое для надёжного распознавания музыки")
            return ""

        # Shazam использует окно около 12 секунд. Загружаем только один фрагмент,
        # а не два: раньше оба скачивались до первого обращения к Shazam, из-за
        # чего пользователь мог долго видеть только сообщение «Проверяю видео».
        clip_end = min(12.0, duration) if duration else 12.0
        if clip_end < 5:
            return ""

        # Запускаем yt-dlp в отдельном процессе. У yt-dlp socket_timeout
        # ограничивает отдельные сетевые операции, но не гарантирует общий
        # предел времени для всей загрузки. subprocess.run(timeout=...) позволяет
        # принудительно остановить зависшую загрузку, не блокируя поток бота.
        output_template = os.path.join(workdir, "audio_sample_%(id)s.%(ext)s")
        download_command = [
            sys.executable, "-m", "yt_dlp",
            "--format", "bestaudio/best",
            "--output", output_template,
            "--no-playlist",
            "--quiet",
            "--no-warnings",
            "--socket-timeout", "8",
            "--retries", "0",
            "--extractor-retries", "0",
            "--fragment-retries", "0",
            "--file-access-retries", "0",
            "--download-sections", f"*0-{clip_end:.1f}",
            "--no-part",
            expanded_url,
        ]
        print("🎵 TikTok: загружаю короткий аудиофрагмент (лимит 30 секунд)...")
        try:
            download_result = subprocess.run(
                download_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print("⏱️ TikTok: загрузка аудиофрагмента остановлена — превышен лимит 30 секунд")
            return ""

        if download_result.returncode != 0:
            details = (download_result.stderr or download_result.stdout or "").strip()
            print(f"TikTok: yt-dlp не смог скачать аудиофрагмент: {details[-1200:]}")
            return ""
        print("✅ TikTok: аудиофрагмент загружен, передаю его на распознавание")

        media_extensions = (".mp4", ".m4a", ".mp3", ".webm", ".mov", ".mkv", ".ogg", ".wav", ".opus", ".aac")
        media_files = []
        for filename in os.listdir(workdir):
            full_path = os.path.join(workdir, filename)
            if filename.startswith("audio_sample_") and filename.lower().endswith(media_extensions):
                if os.path.isfile(full_path) and os.path.getsize(full_path) > 1000:
                    media_files.append(full_path)
        if not media_files:
            print("TikTok: yt-dlp не сохранил аудиофрагмент")
            return ""
        sample_path = max(media_files, key=os.path.getsize)

        async def recognize_one_sample():
            async with Shazam() as shazam:
                try:
                    # Жёсткий тайм-аут, чтобы запрос Shazam не оставлял поиск
                    # навсегда в состоянии ожидания при проблемах сети.
                    result = await asyncio.wait_for(
                        shazam.recognize(sample_path, max_skew=1e-3),
                        timeout=12,
                    )
                    return _shazam_result_query(result)
                except asyncio.TimeoutError:
                    print("Shazam не ответил за 12 секунд")
                    return ""
                except Exception as e:
                    print(f"Ошибка распознавания Shazam: {e!r}")
                    return ""

        print("🎵 TikTok: распознаю музыку через Shazam...")
        recognized_query = asyncio.run(recognize_one_sample())
        if recognized_query:
            print(f"Shazam распознал музыку по звуку: {recognized_query}")
            return recognized_query

        print("Shazam не подтвердил композицию по аудиофрагменту; проверяю музыкальные метаданные")
        return ""
    except Exception as e:
        print(f"Не удалось распознать музыку по звуку видео: {e!r}")
        return ""
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _is_generic_music_label(value):
    value = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    value = re.sub(r"\s*[-–—]\s*.*$", "", value)
    value = re.sub(r"\s+by\s+.*$", "", value)
    generic_labels = (
        "original sound", "original audio", "original music", "promoted music",
        "unknown", "неизвестный звук", "оригинальный звук", "исходный звук",
    )
    return any(value == label or value.startswith(label + " ") for label in generic_labels)


def _video_url_metadata_queries(video_url):
    """Получает несколько поисковых вариантов из музыкальных метаданных и описания ролика."""
    if not extract_video_search_url(video_url):
        raise ValueError("Ссылка не относится к поддерживаемому видеосервису.")

    expanded_url = _expand_short_video_url(video_url)
    cache_key = expanded_url or video_url
    now = time.time()
    with VIDEO_QUERY_CACHE_LOCK:
        cached = VIDEO_QUERY_CACHE.get(cache_key)
        if cached and now - cached[0] < VIDEO_QUERY_CACHE_TTL:
            cached_queries = cached[1]
            return list(cached_queries) if isinstance(cached_queries, (list, tuple)) else [cached_queries]
        if cached:
            VIDEO_QUERY_CACHE.pop(cache_key, None)

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 10,
        "retries": 0,
        "extractor_retries": 0,
        "fragment_retries": 0,
        "playlistend": 1,
        "ignoreerrors": False,
    }

    urls_to_try = [expanded_url]
    if video_url != expanded_url:
        urls_to_try.append(video_url)

    info = None
    last_error = None
    for candidate_url in urls_to_try:
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                candidate_info = ydl.extract_info(candidate_url, download=False)
            if isinstance(candidate_info, dict):
                info = candidate_info
                # Короткая ссылка раскрылась: используем только результат canonical URL.
                break
        except Exception as e:
            last_error = e
            print(f"Не удалось получить метаданные видео {candidate_url}: {e!r}")

    if not isinstance(info, dict):
        raise RuntimeError(
            "Не удалось открыть TikTok-видео. Возможно, оно закрыто или TikTok временно ограничил доступ."
        ) from last_error

    music_info = info.get("music_info") if isinstance(info.get("music_info"), dict) else {}
    track_name = (
        info.get("track")
        or info.get("music_title")
        or info.get("release_title")
        or music_info.get("title")
        or music_info.get("song")
    )
    track_name = str(track_name or "").strip()
    if _is_generic_music_label(track_name):
        track_name = ""

    artist_name = (
        info.get("artist")
        or info.get("artist_name")
        or info.get("album_artist")
        or music_info.get("artist")
        or music_info.get("author")
    )
    if not artist_name:
        artists = info.get("artists") or music_info.get("artists") or []
        if isinstance(artists, str):
            artists = [artists]
        if isinstance(artists, (list, tuple)):
            clean_artists = [
                str(value).strip() for value in artists
                if value and not str(value).strip().lower().startswith("md5:")
            ]
            artist_name = ", ".join(clean_artists[:3])
    artist_name = str(artist_name or "").strip()

    video_title = str(info.get("title") or info.get("alt_title") or "").strip()
    description = str(info.get("description") or "")[:8000]
    queries = []

    def add_query(value):
        value = unescape(str(value or ""))
        value = re.sub(r"https?://\S+", " ", value)
        value = re.sub(r"#[\w\u0400-\u04ff]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip(" -–—|,.;:#")
        value = value[:180]
        if len(value) < 2 or _is_generic_music_label(value):
            return
        if value.casefold() not in {item.casefold() for item in queries}:
            queries.append(value)

    # Сначала используем музыкальную атрибуцию TikTok, а не заголовок ролика.
    if track_name and artist_name:
        add_query(f"{artist_name} - {track_name}")
    if track_name:
        add_query(track_name)

    # Некоторые авторы вручную указывают название композиции в описании.
    for line in description.splitlines():
        match = re.search(
            r"(?i)(?:song|track|music|audio|sound|песня|трек|музыка)"
            r"\s*[:：—-]\s*(.{3,160})",
            line,
        )
        if match:
            add_query(match.group(1))
            if len(queries) >= 4:
                break

    # Не ищем по обычной подписи ролика: она часто не имеет отношения к музыке
    # и приводила к случайным песням. Используем только музыкальные метаданные
    # или явно подписанное название песни в описании.

    if not queries:
        raise RuntimeError(
            "В видео не нашлось названия композиции. Попробуй отправить другую ссылку "
            "или несколько слов из песни."
        )

    queries = queries[:4]
    with VIDEO_QUERY_CACHE_LOCK:
        VIDEO_QUERY_CACHE[cache_key] = (time.time(), tuple(queries))
        while len(VIDEO_QUERY_CACHE) > 200:
            oldest_key = min(VIDEO_QUERY_CACHE, key=lambda key: VIDEO_QUERY_CACHE[key][0])
            VIDEO_QUERY_CACHE.pop(oldest_key, None)

    print(f"Ссылка на видео: варианты поиска музыки: {queries}")
    return queries


def _video_url_metadata_query(video_url):
    """Совместимая функция: возвращает самый точный вариант из метаданных видео."""
    queries = _video_url_metadata_queries(video_url)
    return queries[0] if queries else ""

def _fetch_lyricscom_html(url, timeout=10, max_bytes=2 * 1024 * 1024):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; DevMusicSearch/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        }
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("Страница текстов песен слишком большая")
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _lyricscom_song_page_contains_phrase(song_url, phrase):
    """Проверяет совпадение внутри текста на странице, но никогда не отдаёт текст пользователю."""
    try:
        page = _fetch_lyricscom_html(song_url, timeout=8, max_bytes=1024 * 1024)
        match = re.search(
            r"<(?:pre|div)\b[^>]*id=[\"']lyric-body-text[\"'][^>]*>(.*?)</(?:pre|div)>",
            page,
            re.I | re.S
        )
        if not match:
            return False
        lyrics_text = re.sub(r"<[^>]+>", " ", match.group(1))
        lyrics_text = unescape(re.sub(r"\s+", " ", lyrics_text))
        return norm_text(phrase) in norm_text(lyrics_text)
    except Exception as e:
        print(f"Не удалось проверить совпадение в тексте песни: {e!r}")
        return False


def search_song_titles_by_lyric_fragment(phrase, limit=4):
    """
    Ищет фрагмент непосредственно в базе текстов Lyrics.com.
    На выходе только названия песен; полный текст не копируется в результаты бота.
    """
    phrase = " ".join(str(phrase or "").split()).strip()[:160]
    if len(phrase) < 8 or len(phrase.split()) < 2:
        return []

    search_url = "https://www.lyrics.com/serp.php?" + urlencode({
        "st": phrase,
        "qtype": "2",
    })

    try:
        page = _fetch_lyricscom_html(search_url, timeout=12, max_bytes=3 * 1024 * 1024)
    except Exception as e:
        print(f"Поиск фразы в Lyrics.com временно недоступен: {e!r}")
        return []

    link_pattern = re.compile(
        r"<a\b[^>]*href=[\"'](?P<href>[^\"']*/lyric/\d+[^\"']*)[\"'][^>]*>"
        r"(?P<title>.*?)</a>",
        re.I | re.S
    )

    matches = []
    seen_urls = set()
    for item in link_pattern.finditer(page):
        href = unescape(item.group("href"))
        song_url = urljoin("https://www.lyrics.com", href)
        title = re.sub(r"<[^>]+>", " ", item.group("title"))
        title = re.sub(r"\s+", " ", unescape(title)).strip(" -–—|")
        if not title or len(title) < 2:
            continue
        if song_url in seen_urls:
            continue
        seen_urls.add(song_url)
        matches.append((title[:160], song_url))
        if len(matches) >= 8:
            break

    found_titles = []
    for title, song_url in matches:
        if _lyricscom_song_page_contains_phrase(song_url, phrase):
            found_titles.append(title)
        if len(found_titles) >= max(1, min(8, int(limit))):
            break

    return list(dict.fromkeys(found_titles))


def unified_music_search(query, count=30):
    """
    Один поиск для бота, inline-режима и Mini App:
    - название/исполнитель: стандартный поиск;
    - фраза из песни: стандартный поиск плюс поиск совпадения в текстах песен;
    - ссылка на видео: извлечение названия/исполнителя из метаданных и поиск композиции.
    """
    query = " ".join(str(query or "").split()).strip()
    count = max(1, min(100, int(count)))
    if not query:
        return []

    video_url = extract_video_search_url(query)
    if video_url:
        # Сначала распознаём именно аудио. По названию ролика больше не пытаемся
        # угадать песню: подпись TikTok часто не связана с музыкой в видео.
        recognized_query = _recognize_video_audio_query(video_url)
        if recognized_query:
            song_queries = [recognized_query]
        else:
            song_queries = _video_url_metadata_queries(video_url)

        video_tracks = []
        search_errors = []
        with ThreadPoolExecutor(max_workers=min(4, len(song_queries))) as pool:
            jobs = [
                pool.submit(cached_soundcloud_search, song_query, count, "track")
                for song_query in song_queries
            ]
            for future in as_completed(jobs):
                try:
                    video_tracks.extend(future.result() or [])
                except Exception as e:
                    search_errors.append(e)
                    print(f"Поиск музыки по данным видео не удался: {e!r}")

        video_tracks = dedupe_tracks(video_tracks)
        if not video_tracks and search_errors:
            print(f"Ни один вариант поиска по видео не дал результатов: {search_errors[0]!r}")
        if video_tracks:
            ranked_video_tracks = score_tracks(video_tracks, song_queries)
            ranked_video_tracks = dedupe_ranked_titles(ranked_video_tracks)
            if recognized_query:
                # Не показываем похожие по одному случайному слову композиции.
                ranked_video_tracks = [
                    (score, track) for score, track in ranked_video_tracks
                    if _track_matches_track_query(track, recognized_query)
                ]
            return [track for _, track in ranked_video_tracks[:count]]
        return []

    # Обычный поиск по названию всегда выполняется первым.
    title_tracks = cached_soundcloud_search(query, count, "track")

    # Поиск в текстах нужен только для достаточно длинных фраз, а не для обычных коротких названий.
    words = query.split()
    lyric_titles = []
    if len(query) >= 8 and len(words) >= 2:
        lyric_titles = search_song_titles_by_lyric_fragment(query, limit=4)

    if not lyric_titles:
        return title_tracks[:count]

    lyric_tracks = []
    with ThreadPoolExecutor(max_workers=min(3, len(lyric_titles))) as pool:
        jobs = [
            pool.submit(cached_soundcloud_search, song_title, 15, "track")
            for song_title in lyric_titles
        ]
        for future in as_completed(jobs):
            try:
                lyric_tracks.extend(future.result() or [])
            except Exception as e:
                print(f"Не удалось найти аудио по найденному названию песни: {e!r}")

    # Треки, найденные по подтверждённым совпадениям в тексте, показываем раньше.
    # Обычные совпадения по названию остаются в списке как резерв.
    lyric_ranked = score_tracks(dedupe_tracks(lyric_tracks), lyric_titles)
    lyric_ranked = dedupe_ranked_titles(lyric_ranked)
    lyric_tracks_sorted = [track for _, track in lyric_ranked]
    return dedupe_tracks(lyric_tracks_sorted + title_tracks)[:count]


def cached_unified_music_search(query, count=30):
    """TTL-кэш единого поиска: одинаковый запрос не запускает повторный парсинг текстов."""
    normalized_query = norm_text(query)
    count = max(1, min(100, int(count)))
    cache_key = (normalized_query, count, "unified")
    now = time.time()

    with search_cache_lock:
        cached = search_results_cache.get(cache_key)
        if cached and now - cached[0] < SEARCH_CACHE_TTL:
            return cached[1]
        if cached:
            search_results_cache.pop(cache_key, None)

    tracks = unified_music_search(query, count)
    with search_cache_lock:
        search_results_cache[cache_key] = (time.time(), tracks)
        while len(search_results_cache) > 100:
            oldest_key = min(search_results_cache, key=lambda key: search_results_cache[key][0])
            search_results_cache.pop(oldest_key, None)
    return tracks



def store_tracks(tracks):
    """Сохраняет конкретную выдачу под коротким id, чтобы старые кнопки не качали чужие треки."""
    sid = os.urandom(4).hex()
    with tracks_cache_lock:
        tracks_cache[sid] = tracks
        while len(tracks_cache) > 300:
            tracks_cache.pop(next(iter(tracks_cache)))
    return sid


def qid(query, base=None):
    """Запрос не лезет в callback_data (лимит 64 байта) — храним на сервере, в кнопке только id."""
    base = base or query
    key = hashlib.md5(f"{query}\n{base}".encode("utf-8")).hexdigest()[:10]
    with query_store_lock:
        query_store[key] = (query, base)
        while len(query_store) > 3000:
            query_store.pop(next(iter(query_store)))
    return key


def get_query(key):
    with query_store_lock:
        return query_store.get(key)


# --- ИНЛАЙН-РЕЖИМ ---
@bot.inline_handler(func=lambda query: True)
def inline_query(query):
    search_text = query.query.strip()
    if not search_text:
        return

    def worker():
        try:
            tracks = cached_unified_music_search(search_text, 20)[:10]

            inline_tracks_cache[query.from_user.id] = tracks
            if len(inline_tracks_cache) > 500:
                inline_tracks_cache.pop(next(iter(inline_tracks_cache)))

            results = []
            for i, track in enumerate(tracks):
                results.append(
                    telebot.types.InlineQueryResultArticle(
                        id=str(i),
                        title=track["title"][:50],
                        description=f"Автор: {track['uploader']} | Нажми для отправки",
                        input_message_content=telebot.types.InputTextMessageContent(
                            message_text=f"⏳ Загружаю трек: {track['title'][:40]}..."
                        )
                    )
                )
            bot.answer_inline_query(query.id, results, cache_time=1)
        except Exception as e:
            print(f"Ошибка инлайн-поиска: {e}")

    threading.Thread(target=worker, daemon=True).start()


@bot.chosen_inline_handler(func=lambda chosen: True)
def handle_chosen_inline(chosen):
    user_id = chosen.from_user.id
    register_user(user_id)

    def worker():
        try:
            result_id = int(chosen.result_id)
        except ValueError:
            return
        user_tracks = inline_tracks_cache.get(user_id, [])
        if result_id >= len(user_tracks):
            return

        track = user_tracks[result_id]
        track_url = track["url"]
        title = track["title"]
        uploader = track["uploader"]
        inline_msg_id = chosen.inline_message_id

        cached = audio_cache.get(track_url)
        if cached and not str(cached).startswith("http"):
            try:
                bot.edit_message_media(
                    media=InputMediaAudio(media=cached, title=title, performer=uploader),
                    inline_message_id=inline_msg_id
                )
                count_download()
                return
            except Exception as e:
                print(f"Ошибка отправки из кэша (инлайн): {e}")

        workdir = tempfile.mkdtemp(prefix="inl_")
        try:
            with download_slots:
                delivered_track, info, audio_filename, thumbnail_filename = prepare_track_with_alternates(
                    track, workdir, fallback_tracks=user_tracks
                )

            with open(audio_filename, "rb") as audio:
                thumb_file = open(thumbnail_filename, "rb") if thumbnail_filename and os.path.exists(thumbnail_filename) else None
                try:
                    backup_msg = bot.send_audio(
                        chat_id=BACKUP_CHANNEL_ID,
                        audio=audio,
                        title=info.get('title', title),
                        performer=info.get('uploader', uploader),
                        thumbnail=thumb_file,
                        caption=audius_attribution_caption(info)
                    )
                finally:
                    if thumb_file:
                        thumb_file.close()
            file_id = backup_msg.audio.file_id
            audio_cache[track_url] = file_id

            bot.edit_message_media(
                media=InputMediaAudio(
                    media=file_id,
                    title=info.get('title', title),
                    performer=info.get('uploader', uploader)
                ),
                inline_message_id=inline_msg_id
            )
            count_download()
        except Exception as e:
            report_error(f"Ошибка скачивания (инлайн) {track_url}", e)
            try:
                bot.edit_message_text("❌ Не удалось скачать трек.", inline_message_id=inline_msg_id)
            except Exception:
                pass
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    threading.Thread(target=worker, daemon=True).start()


@bot.message_handler(commands=['stats'])
def stats_command(message):
    chat_id = message.chat.id
    register_user(chat_id)

    start_ping = time.time()
    bot.get_me()
    ping_ms = int((time.time() - start_ping) * 1000)

    now = time.time()
    month_ago = now - (28 * 24 * 60 * 60)
    real_users_count = sum(1 for uid, last_seen in list(active_users.items()) if last_seen >= month_ago)
    recent_users_count = max(0, real_users_count + users_offset)

    uptime_seconds = int(time.time() - bot_start_time)
    hours = uptime_seconds // 3600
    minutes = (uptime_seconds % 3600) // 60

    stats_text = (
        f"📊 **Статистика бота:**\n\n"
        f"👥 Активных пользователей: `{recent_users_count}`\n"
        f"⏱ Время работы: `{hours}ч {minutes}м`\n"
        f"📥 Скачано треков: `{stats_data['total_downloads']}`\n"
        f"⚡️ Пинг сервера: `{ping_ms} мс`\n\n"
        f"🟢 Статус: `Онлайн (Render)`"
    )
    bot.reply_to(message, stats_text, parse_mode="Markdown")


@bot.message_handler(commands=['donate'])
def donate_command(message):
    chat_id = message.chat.id
    register_user(chat_id)
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton(text="⭐ 5 звёзд", callback_data="donate_5"),
        InlineKeyboardButton(text="⭐ 10 звёзд", callback_data="donate_10"),
        InlineKeyboardButton(text="👑 VIP Неделя (50⭐)", callback_data="buy_vip_7"),
        InlineKeyboardButton(text="👑 VIP Месяц (150⭐)", callback_data="buy_vip_30"),
        InlineKeyboardButton(text="✍️ Своё количество", callback_data="donate_custom")
    )
    bot.send_message(
        chat_id,
        "💖 Спасибо за желание поддержать проект!\nВыбери сумму в звёздах или купи VIP статус:",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith(("donate_", "buy_vip_")))
def handle_donate_callback(call):
    chat_id = call.message.chat.id
    action = call.data

    if action == "donate_custom":
        waiting_for_custom_stars.add(chat_id)
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, "✍️ Напиши число — сколько звёзд отправить (например: `50`):", parse_mode="Markdown")
        return

    if action.startswith("buy_vip_"):
        try:
            days = int(action.replace("buy_vip_", ""))
        except ValueError:
            return bot.answer_callback_query(call.id)
        plan = VIP_PLANS.get(days)
        if not plan:
            return bot.answer_callback_query(call.id, "Тариф не найден.")
        send_invoice_stars(chat_id, plan["stars"], plan["title"], f"vip_{days}")
    else:
        try:
            stars_count = int(action.replace("donate_", ""))
        except ValueError:
            return bot.answer_callback_query(call.id)
        send_invoice_stars(chat_id, stars_count, "Поддержка проекта", "donate")

    bot.answer_callback_query(call.id)


def send_invoice_stars(chat_id, amount, title="Поддержка проекта", payload="donate"):
    prices = [LabeledPrice(label=title, amount=amount)]
    bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=f'Сумма: {amount} ⭐',
        invoice_payload=payload,
        provider_token='',
        currency='XTR',
        prices=prices,
        start_parameter='vip'
    )


@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    payload = message.successful_payment.invoice_payload
    user_id = message.from_user.id
    if payload.startswith("vip_"):
        try:
            days = int(payload.replace("vip_", ""))
        except ValueError:
            return
        if days not in VIP_PLANS:
            return
        set_vip_status(user_id, duration_days=days)
        bot.reply_to(message, f"🎉 Поздравляем! VIP выдан на {days} дн.\n{format_vip_status(user_id)}\n\nВсе функции разблокированы ❤️")
        try:
            bot.send_message(ADMIN_ID, f"💰 Покупка VIP: user {user_id}, {days} дн., {message.successful_payment.total_amount} ⭐")
        except Exception:
            pass
    else:
        bot.reply_to(message, "🎉 Ура! Огромное спасибо за донат! Твоя поддержка бесценна ❤️")


@bot.message_handler(commands=['m', 'play', 'music'])
def handle_group_music(message):
    chat_id = message.chat.id
    register_user(chat_id)

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(message, "❗️ Напиши название трека после команды.\nПример: `/m terranova`", parse_mode="Markdown")
        return

    search_music_by_query(message, query=args[1], user_id=message.from_user.id, page=1, is_new=True)


@bot.message_handler(commands=['author', 'artist', 'a'])
def handle_author_music(message):
    chat_id = message.chat.id
    if not check_user_subscription(message.from_user.id):
        send_subscription_prompt(chat_id)
        return
    register_user(chat_id)

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        bot.reply_to(message, "❗️ Напиши имя исполнителя после команды.\nПример: `/author Miyagi`", parse_mode="Markdown")
        return

    search_music_by_query(message, query=args[1], user_id=message.from_user.id, page=1, is_new=True)


@bot.message_handler(commands=['discover', 'genres', 'd'])
def handle_discover(message):
    chat_id = message.chat.id
    if not check_user_subscription(message.from_user.id):
        send_subscription_prompt(chat_id)
        return
    register_user(chat_id)

    text = (
        "🧭 **Не знаешь, что послушать? Выбирай направление:**\n\n"
        "• **Поп:** Мелодичные песни с простым ритмом.\n"
        "• **Рок:** Энергичный стиль с электрогитарами.\n"
        "• **Хип-хоп:** Ритмичный речитатив под бит.\n"
        "• **Джаз:** Импровизационные композиции.\n"
        "• **Phonk:** Агрессивный бас и качающий ритм.\n"
        "• **TikTok Hits:** Треки из коротких видео.\n\n"
        "👇 *Нажми на кнопку ниже, и я подберу музыку!*"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton(text="🕺 Поп", callback_data="genre_Pop hits"),
        InlineKeyboardButton(text="🎸 Рок", callback_data="genre_Rock hits"),
        InlineKeyboardButton(text="🎤 Хип-хоп", callback_data="genre_Rap Hip-Hop"),
        InlineKeyboardButton(text="🎷 Джаз", callback_data="genre_Jazz"),
        InlineKeyboardButton(text="🏎 Phonk", callback_data="genre_Phonk"),
        InlineKeyboardButton(text="📱 TikTok Hits", callback_data="genre_TikTok viral music")
    )

    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)


RANDOM_MODIFIERS = ["mix", "2024", "2025", "2026", "playlist", "hits", "top", "remix", "vibes",
                    "chill", "night", "club", "party", "bass", "popular", "edit"]


def random_query(base):
    return f"{base} {random.choice(RANDOM_MODIFIERS)} {random.choice(RANDOM_MODIFIERS)}".strip()


@bot.callback_query_handler(func=lambda call: call.data.startswith("genre_"))
def handle_genre_selection(call):
    chat_id = call.message.chat.id
    if not check_user_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Сначала подпишись на группу", show_alert=True)
        send_subscription_prompt(chat_id)
        return
    register_user(chat_id)

    genre_base = call.data.replace("genre_", "")
    bot.answer_callback_query(call.id, "🎲 Собираю случайные треки...")
    search_music_by_query(
        call.message, query=random_query(genre_base), user_id=call.from_user.id,
        page=1, is_new=True, is_random=True, base_query=genre_base
    )


# --- ОБРАБОТЧИК ТЕКСТОВОГО ПОИСКА С ПРОВЕРКОЙ ПОДПИСКИ ---
@bot.message_handler(func=lambda message: message.chat.type == 'private' and bool(message.text) and not message.text.startswith('/'))
def text_handler(message):
    chat_id = message.chat.id

    register_user(chat_id)

    if chat_id in waiting_for_custom_stars:
        waiting_for_custom_stars.discard(chat_id)
        text = message.text.strip()
        if text.isdigit() and 0 < int(text) <= 10000:
            send_invoice_stars(chat_id, int(text), "Поддержка проекта", "donate")
        else:
            bot.reply_to(message, "❌ Пожалуйста, введи корректное число. Попробуй снова через /donate")
        return

    if not check_user_subscription(message.from_user.id):
        send_subscription_prompt(chat_id)
        return

    search_music_by_query(message, query=message.text, user_id=message.from_user.id, page=1, is_new=True)


def search_music_by_query(message, query, user_id, page=1, is_new=False, is_filter=False,
                          is_random=False, base_query=None):
    chat_id = message.chat.id
    base_query = base_query or query
    original_queries[chat_id] = base_query

    if is_new:
        if extract_video_search_url(query):
            status_text = "🔗 Получаю звук из видео и распознаю музыку..."
        elif len(query.split()) >= 2 and len(query) >= 8:
            status_text = "🔎 Ищу по названию и словам песни..."
        else:
            status_text = "🔍 Ищу варианты..."
        msg = bot.reply_to(message, status_text)
    else:
        msg = message

    def worker():
        try:
            # ЛИМИТЫ: VIP до 50, обычный до 25 — по user_id, не по chat_id
            max_possible = max_search_limit(user_id)
            user_limit = int((user_settings.get(str(user_id)) or {}).get("search_limit", 10))
            effective_limit = max(5, min(max_possible, user_limit))

            # На первой странице используем тот же размер выдачи, что и Mini App.
            # Для следующих страниц увеличиваем выборку, чтобы работала пагинация.
            limit_ydl = 50 if is_random else min(100, max(30, effective_limit * max(2, page)))
            if is_random or is_filter:
                all_tracks = cached_soundcloud_search(query, limit_ydl, "track")
            else:
                all_tracks = cached_unified_music_search(query, limit_ydl)

            if is_random:
                if len(all_tracks) >= effective_limit:
                    tracks = random.sample(all_tracks, effective_limit)
                else:
                    tracks = all_tracks.copy()
                    random.shuffle(tracks)
            else:
                start = (page - 1) * effective_limit
                tracks = all_tracks[start:start + effective_limit]

            if not tracks:
                bot.edit_message_text("❌ Больше ничего не найдено.", chat_id, msg.message_id, reply_markup=None)
                return

            sid = store_tracks(tracks)
            markup = InlineKeyboardMarkup(row_width=1)

            for i, track in enumerate(tracks):
                title = track["title"][:35]
                markup.add(InlineKeyboardButton(text=f"🎵 {i+1}. {title}", callback_data=f"dl_{sid}_{i}"))

            nav_buttons = []
            if is_random:
                nav_buttons.append(InlineKeyboardButton(
                    text="🎲 Ещё случайных", callback_data=f"randpage_{page+1}_{qid(base_query)}"))
            else:
                if page > 1:
                    nav_buttons.append(InlineKeyboardButton(
                        text="⬅️ Назад", callback_data=f"page_{page-1}_{qid(query, base_query)}"))
                nav_buttons.append(InlineKeyboardButton(
                    text="🔄 Ещё", callback_data=f"page_{page+1}_{qid(query, base_query)}"))

            markup.row(*nav_buttons)

            if is_filter:
                markup.row(InlineKeyboardButton(text="🔙 Назад к обычному", callback_data=f"back_{qid(base_query)}"))
            else:
                base_id = qid(base_query)
                markup.row(
                    InlineKeyboardButton(text="⚡ Speed Up", callback_data=f"filter_{base_id}_speedup"),
                    InlineKeyboardButton(text="🐢 Slowed", callback_data=f"filter_{base_id}_slowed")
                )

            prefix = "🎲 Случайная подборка." if is_random else f"🎧 Страница {page}."
            bot.edit_message_text(f"{prefix} Запрос: {query}", chat_id, msg.message_id, reply_markup=markup)

        except Exception as e:
            report_error(f"Ошибка поиска «{query}»", e)
            try:
                bot.edit_message_text("❌ Ошибка поиска.", chat_id, msg.message_id)
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()


# --- КНОПКА «К ПОИСКУ» (отдельный callback, не пересекается с back_) ---
@bot.callback_query_handler(func=lambda call: call.data in ("tosearch", "back_to_search"))
def handle_back_to_search(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    query = original_queries.get(chat_id, "музыка")
    search_music_by_query(call.message, query=query, user_id=call.from_user.id, page=1, is_new=True)


@bot.callback_query_handler(func=lambda call: call.data.startswith(("page_", "randpage_", "filter_", "back_")))
def handle_navigation(call):
    data = call.data.split("_")
    user_id = call.from_user.id
    kind = data[0]

    key = data[2] if kind in ("page", "randpage") and len(data) >= 3 else (data[1] if len(data) >= 2 else "")
    stored = get_query(key)
    if not stored:
        return bot.answer_callback_query(call.id, "⌛ Поиск устарел. Отправь запрос заново.", show_alert=True)
    bot.answer_callback_query(call.id)
    query, base = stored

    try:
        if kind == "page":
            page = int(data[1])
            search_music_by_query(call.message, query=query, user_id=user_id, page=page,
                                  is_filter=(query != base), base_query=base)
        elif kind == "randpage":
            page = int(data[1])
            search_music_by_query(call.message, query=random_query(base), user_id=user_id, page=page,
                                  is_random=True, base_query=base)
        elif kind == "filter":
            filter_type = data[2] if len(data) >= 3 else "slowed"
            if filter_type not in ("speedup", "slowed"):
                return
            search_music_by_query(call.message, query=f"{query} {filter_type}", user_id=user_id, page=1,
                                  is_filter=True, base_query=query)
        elif kind == "back":
            search_music_by_query(call.message, query=query, user_id=user_id, page=1)
    except (ValueError, IndexError):
        pass


# --- ОТПРАВКА ТРЕКА В ЧАТ (общая для бота и Mini App) ---
def send_track(chat_id, track, effect="none", status_msg_id=None, user_id=None, fallback_tracks=None):
    """Скачивает выбранный трек; при отказе источника перебирает похожие результаты."""
    user_id = user_id or chat_id
    if effect not in VIP_EFFECTS:
        effect = "none"
    if effect != "none" and not is_user_vip(user_id):
        effect = "none"

    track_url = track["url"]
    title = track["title"]
    uploader = track["uploader"]
    workdir = None

    def finish_ok(shown_title, shown_uploader, info=None, delivered_track=None):
        alternate_used = bool(
            delivered_track
            and str(delivered_track.get("url") or "") != track_url
        )
        if status_msg_id:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(text="🔍 Найти другой трек", callback_data="tosearch"))
            try:
                if alternate_used:
                    text = (
                        "✅ Первый вариант оказался недоступен.\n"
                        "Подобрана похожая доступная версия:\n\n"
                    )
                else:
                    text = ""
                text += f"▶️ **Плеер:**\n🎵 {shown_title}\n🧑‍🎤 {shown_uploader}"
                attribution = audius_attribution_caption(info) if info else None
                if attribution:
                    text += "\n\n" + attribution
                bot.edit_message_text(
                    text=text,
                    chat_id=chat_id, message_id=status_msg_id,
                    parse_mode=None if attribution else "Markdown", reply_markup=markup
                )
            except Exception:
                pass
        elif alternate_used:
            try:
                bot.send_message(
                    chat_id,
                    f"✅ Первый вариант недоступен. Отправлена похожая версия: {shown_title} — {shown_uploader}"
                )
            except Exception:
                pass
        count_download()

    try:
        # Кэш выбранного результата проверяем первым, как и раньше.
        cached_file_id = audio_cache.get(track_url)
        if effect == "none" and cached_file_id and not str(cached_file_id).startswith("http"):
            try:
                bot.send_audio(chat_id, cached_file_id, title=title, performer=uploader)
                finish_ok(title, uploader, delivered_track=track)
                return
            except Exception as e:
                print(f"file_id из кэша не сработал, качаем заново: {e}")
                audio_cache.pop(track_url, None)

        with download_slots:
            workdir = tempfile.mkdtemp(prefix="trk_")
            delivered_track, info, audio_filename, thumbnail_filename = prepare_track_with_alternates(
                track,
                workdir,
                effect,
                fallback_tracks=fallback_tracks,
                max_attempts=4,
            )

            out_title = info.get("title", delivered_track.get("title", title))
            if effect != "none":
                out_title = f"{out_title} ({EFFECT_NAMES[effect]})"
            out_uploader = info.get("uploader", delivered_track.get("uploader", uploader))

            with open(audio_filename, "rb") as audio:
                thumb_file = open(thumbnail_filename, "rb") if thumbnail_filename and os.path.exists(thumbnail_filename) else None
                try:
                    sent = bot.send_audio(
                        chat_id, audio, title=out_title,
                        performer=out_uploader, thumbnail=thumb_file,
                        caption=audius_attribution_caption(info)
                    )
                finally:
                    if thumb_file:
                        thumb_file.close()

        if effect == "none":
            audio_cache[track_url] = sent.audio.file_id
            if delivered_track.get("url"):
                audio_cache[delivered_track["url"]] = sent.audio.file_id
        finish_ok(out_title, out_uploader, info, delivered_track=delivered_track)
    except Exception as e:
        report_error(f"Ошибка загрузки трека {track_url}", e)
        reason = explain_error(e)
        text = f"❌ Не удалось скачать «{title[:80]}» и не нашлось достаточно похожей доступной версии."
        if reason:
            text += f"\n{reason}"
        try:
            if status_msg_id:
                bot.edit_message_text(text, chat_id, status_msg_id)
            else:
                bot.send_message(chat_id, text)
        except Exception as send_error:
            print(f"Не удалось сообщить об ошибке: {send_error}")
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


# --- VIP: ВЫБОР ЭФФЕКТА ПРИ СКАЧИВАНИИ В БОТЕ ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("dlfx_"))
def handle_download_effect_callback(call):
    chat_id = call.message.chat.id
    parts = call.data.split("_")  # dlfx_<sid>_<index>_<effect>
    if len(parts) != 4 or not parts[2].isdigit():
        return bot.answer_callback_query(call.id)
    sid, index, effect = parts[1], int(parts[2]), parts[3]

    if not check_user_subscription(call.from_user.id):
        return bot.answer_callback_query(call.id, "❌ Сначала подпишись на группу", show_alert=True)
    if effect != "none" and effect not in VIP_EFFECTS:
        return bot.answer_callback_query(call.id)
    if effect != "none" and not is_user_vip(call.from_user.id):
        return bot.answer_callback_query(call.id, "👑 Эффекты доступны только с VIP (/vip)", show_alert=True)

    user_tracks = tracks_cache.get(sid, [])
    if index >= len(user_tracks):
        return bot.answer_callback_query(call.id, "❌ Трек не найден в кэше. Повтори поиск.", show_alert=True)

    track = user_tracks[index]
    bot.answer_callback_query(call.id, "⏳ Обрабатываю трек...")
    try:
        bot.edit_message_text(
            f"⏳ Готовлю трек: {track['title'][:60]}\nЭффект: {EFFECT_NAMES.get(effect, 'обычный звук')}",
            chat_id, call.message.message_id
        )
    except Exception:
        pass
    threading.Thread(
        target=send_track,
        args=(chat_id, track, effect, call.message.message_id, call.from_user.id),
        kwargs={"fallback_tracks": user_tracks},
        daemon=True
    ).start()


@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def handle_download_callback(call):
    chat_id = call.message.chat.id
    register_user(chat_id)

    # Проверка подписки (раньше её здесь не было)
    if not check_user_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Сначала подпишись на группу", show_alert=True)
        send_subscription_prompt(chat_id)
        return

    parts = call.data.split("_")  # dl_<sid>_<index>
    if len(parts) != 3 or not parts[2].isdigit():
        return bot.answer_callback_query(call.id, "⌛ Кнопка устарела. Повтори поиск.", show_alert=True)
    sid, index = parts[1], int(parts[2])
    user_tracks = tracks_cache.get(sid, [])

    if index >= len(user_tracks):
        bot.answer_callback_query(call.id, "❌ Трек не найден в кэше. Повтори поиск.", show_alert=True)
        return

    track = user_tracks[index]
    title = track["title"]
    uploader = track["uploader"]

    # VIP: предлагаем выбрать эффект перед скачиванием
    if is_user_vip(call.from_user.id):
        bot.answer_callback_query(call.id)
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("🔊 Обычный", callback_data=f"dlfx_{sid}_{index}_none"),
            InlineKeyboardButton("🔉 Bass Boost", callback_data=f"dlfx_{sid}_{index}_bassboost"),
            InlineKeyboardButton("🎧 8D", callback_data=f"dlfx_{sid}_{index}_8d"),
            InlineKeyboardButton("🐢 Slowed", callback_data=f"dlfx_{sid}_{index}_slowed"),
            InlineKeyboardButton("⚡ Nightcore", callback_data=f"dlfx_{sid}_{index}_nightcore"),
        )
        markup.row(InlineKeyboardButton("🔙 К поиску", callback_data="tosearch"))
        bot.send_message(chat_id, f"🎚 **Выбери звук для:**\n🎵 {title}", parse_mode="Markdown", reply_markup=markup)
        return

    bot.answer_callback_query(call.id, f"📥 Загружаю: {title[:30]}...")

    status_msg = bot.send_message(
        chat_id,
        f"⏳ **Скачиваю трек:**\n🎵 {title}\n🧑‍🎤 {uploader}\n\n*Подожди пару секунд...*",
        parse_mode="Markdown"
    )
    threading.Thread(
        target=send_track,
        args=(chat_id, track, "none", status_msg.message_id, call.from_user.id),
        kwargs={"fallback_tracks": user_tracks},
        daemon=True
    ).start()


# --- API MINI APP: AUTH, SEARCH, FAVORITES, HISTORY, PLAYLISTS, STREAM ---

def validate_miniapp_request(handler, raw_init_data=None):
    """Проверяет подпись initData Telegram. Для audio src данные передаются в query auth."""
    raw = raw_init_data if raw_init_data is not None else handler.headers.get("X-Telegram-Init-Data", "")
    if not raw or len(raw) > 8192:
        return None
    try:
        fields = dict(parse_qsl(raw, keep_blank_values=True))
        received_hash = fields.pop("hash", None)
        if not received_hash or "user" not in fields or "auth_date" not in fields:
            return None
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret_key = hmac.new(b"WebAppData", API_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        auth_date = int(fields["auth_date"])
        if auth_date > time.time() + 60 or time.time() - auth_date > 86400:
            return None
        user = json.loads(fields["user"])
        if not isinstance(user, dict) or not user.get("id"):
            return None
        return user
    except Exception as e:
        print(f"Ошибка проверки Telegram initData: {e}")
        return None


def api_json(handler, status, payload):
    handler._send_json(status, payload)


def get_user_settings(user_id):
    user_key = str(user_id)
    if user_key not in user_settings or not isinstance(user_settings[user_key], dict):
        user_settings[user_key] = {"search_limit": 10}
    settings = user_settings[user_key]
    settings.setdefault("search_limit", 10)
    settings.setdefault("favorites", [])
    settings.setdefault("history", [])
    settings.setdefault("playlists", [])
    settings.setdefault("theme", "default")
    return settings


def best_soundcloud_thumbnail(info):
    if not isinstance(info, dict):
        return ""
    candidates = []
    for key in ("thumbnail", "artwork_url"):
        if info.get(key):
            candidates.append(info[key])
    for item in (info.get("thumbnails") or []):
        if isinstance(item, dict) and item.get("url"):
            candidates.append(item["url"])
    for candidate in reversed(candidates):
        url = str(candidate).strip()
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname and ("sndcdn.com" in parsed.hostname or "soundcloud.com" in parsed.hostname):
            url = url.replace("-large.", "-t500x500.")
            return url[:1000]
    return ""


def normalize_miniapp_track(raw):
    if not isinstance(raw, dict):
        return None
    track_url = str(raw.get("url", "")).strip()
    parsed = urlparse(track_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return None
    is_soundcloud = host == "soundcloud.com" or host.endswith(".soundcloud.com")
    is_audius = host == "audius.co" or host.endswith(".audius.co")
    is_archive = host in ("archive.org", "www.archive.org") and parsed.path.startswith("/details/")
    inferred_source = "internet_archive" if is_archive else "audius" if is_audius else "soundcloud" if is_soundcloud else ""
    source = str(raw.get("source") or inferred_source).lower()
    if source == "soundcloud" and not is_soundcloud:
        return None
    if source == "audius" and not is_audius:
        return None
    if source == "internet_archive" and not is_archive:
        return None
    if source not in ("soundcloud", "audius", "internet_archive"):
        return None
    thumbnail = str(raw.get("thumbnail") or "").strip()[:1000]
    thumb_parsed = urlparse(thumbnail)
    if thumbnail and not (thumb_parsed.scheme == "https" and thumb_parsed.hostname):
        thumbnail = ""
    try:
        duration = int(float(raw.get("duration"))) if raw.get("duration") is not None else None
    except (TypeError, ValueError):
        duration = None
    result = {
        "url": track_url,
        "title": str(raw.get("title") or "Без названия")[:180],
        "uploader": str(raw.get("uploader") or "Неизвестен")[:120],
        "thumbnail": thumbnail,
        "duration": duration,
        "source": source,
    }
    if source == "audius":
        result["audius_id"] = str(raw.get("audius_id") or raw.get("id") or "")[:100]
    elif source == "internet_archive":
        parts = [part for part in parsed.path.split("/") if part]
        identifier = str(raw.get("ia_identifier") or (parts[1] if len(parts) >= 2 else "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", identifier):
            return None
        result["ia_identifier"] = identifier
        result["ia_file_name"] = str(raw.get("ia_file_name") or "")[:240]
        result["license_url"] = str(raw.get("license_url") or "")[:500]
    return result


def resolve_soundcloud_metadata(track_url):
    options = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "socket_timeout": 12, "retries": 1,
        "extractor_retries": 1
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(track_url, download=False)
    return info or {}


def fill_missing_track_covers(tracks, max_items=12):
    missing = [i for i, track in enumerate(tracks)
               if isinstance(track, dict) and not track.get("thumbnail") and track.get("url")
               and str(track.get("source") or "soundcloud") == "soundcloud"][:max_items]
    if not missing:
        return False
    changed = False
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {pool.submit(resolve_soundcloud_metadata, tracks[i]["url"]): i for i in missing}
        for job in as_completed(jobs):
            index = jobs[job]
            try:
                info = job.result()
                cover = best_soundcloud_thumbnail(info)
                if cover:
                    tracks[index]["thumbnail"] = cover
                    changed = True
                if info.get("title") and tracks[index].get("title") in ("", "Без названия"):
                    tracks[index]["title"] = str(info["title"])[:180]
                    changed = True
                if info.get("uploader") and tracks[index].get("uploader") in ("", "Неизвестен"):
                    tracks[index]["uploader"] = str(info["uploader"])[:120]
                    changed = True
            except Exception as e:
                print(f"Не удалось обновить обложку трека: {e}")
    return changed


def miniapp_is_subscribed(user_id):
    return check_user_subscription(user_id)


def add_to_user_history(settings, track):
    history = settings.setdefault("history", [])
    history[:] = [item for item in history if item.get("url") != track["url"]]
    history.insert(0, track)
    del history[50:]


def handle_miniapp_get_data(handler):
    user = validate_miniapp_request(handler)
    if not user:
        return api_json(handler, 401, {"error": "Открой приложение через Telegram, чтобы авторизоваться."})
    uid = user["id"]
    settings = get_user_settings(uid)
    changed = False
    changed |= fill_missing_track_covers(settings.get("favorites", []), max_items=10)
    changed |= fill_missing_track_covers(settings.get("history", []), max_items=10)

    vip = is_user_vip(uid)
    # Если VIP закончился — сбрасываем VIP-тему и лимит
    if not vip:
        if settings.get("theme", "default") != "default":
            settings["theme"] = "default"
            changed = True
        if int(settings.get("search_limit", 10)) > 25:
            settings["search_limit"] = 25
            changed = True
    if changed:
        request_save()

    api_json(handler, 200, {
        "user": {"id": uid, "first_name": user.get("first_name", "Музыка")},
        "search_limit": int(settings.get("search_limit", 10)),
        "favorites": settings.get("favorites", []),
        "history": settings.get("history", []),
        "playlists": settings.get("playlists", []),
        "is_vip": vip,
        "vip_expires": get_vip_expires(uid),
        "theme": settings.get("theme", "default")
    })
    favs = settings.get("favorites", [])
    if favs:
        threading.Thread(target=prewarm_tracks_stream_urls, args=(favs[:20],), daemon=True).start()


def handle_miniapp_search(handler, query_params):
    user = validate_miniapp_request(handler)
    if not user:
        return api_json(handler, 401, {"error": "Открой приложение через Telegram."})
    if not miniapp_is_subscribed(user["id"]):
        return api_json(handler, 403, {
            "error": "Сначала вступи в нашу группу (или купи VIP).",
            "subscription_required": True,
            "invite_url": "https://t.me/+8JYfdTy2YhE0NDJi"
        })
    query = str((query_params.get("q") or [""])[0]).strip()[:1200]
    search_type = str((query_params.get("type") or ["track"])[0]).strip().lower()
    if not query:
        return api_json(handler, 400, {"error": "Введи имя автора или название трека."})
    settings = get_user_settings(user["id"])

    # VIP ЛИМИТ: до 50, обычный до 25
    max_possible = max_search_limit(user["id"])
    limit = max(5, min(max_possible, int(settings.get("search_limit", 10))))

    try:
        fetch_count = min(100, max(30, limit * 3) if search_type == "author" else max(30, limit * 2))
        if search_type == "author":
            tracks = cached_soundcloud_search(query, fetch_count, "author")
        else:
            tracks = cached_unified_music_search(query, fetch_count)

        if search_type == "author":
            q_low = query.lower()
            exact_author = [t for t in tracks if q_low in (t.get("uploader") or "").lower()]
            other_author = [t for t in tracks if q_low not in (t.get("uploader") or "").lower()]
            tracks = (exact_author + other_author)[:limit]
        else:
            tracks = tracks[:limit]

        missing = [i for i, track in enumerate(tracks)
                   if not track.get("thumbnail") and str(track.get("source") or "soundcloud") == "soundcloud"][:min(8, len(tracks))]
        if missing:
            with ThreadPoolExecutor(max_workers=3) as pool:
                jobs = {pool.submit(resolve_soundcloud_metadata, tracks[i]["url"]): i for i in missing}
                for job in as_completed(jobs):
                    idx = jobs[job]
                    try:
                        metadata = job.result()
                        cover = best_soundcloud_thumbnail(metadata)
                        if cover:
                            tracks[idx]["thumbnail"] = cover
                        if metadata.get("duration") and not tracks[idx].get("duration"):
                            tracks[idx]["duration"] = int(metadata["duration"])
                    except Exception as e:
                        print(f"Не удалось получить метаданные трека: {e}")
        if tracks:
            threading.Thread(target=prewarm_tracks_stream_urls, args=(tracks[:2],), daemon=True).start()
        api_json(handler, 200, {"tracks": tracks, "query": query})
    except Exception as e:
        report_error(f"Ошибка поиска Mini App «{query}»", e)
        api_json(handler, 500, {"error": "Не удалось выполнить поиск. Попробуй ещё раз."})


def _duration_seconds(value):
    try:
        result = float(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def _validate_stream_metadata(expected_track, actual_info, source_name):
    """Не отдаёт аудио, если метаданные страницы не соответствуют выбранной карточке."""
    expected_track = expected_track or {}
    actual_info = actual_info or {}

    expected_duration = _duration_seconds(expected_track.get("duration"))
    actual_duration = _duration_seconds(actual_info.get("duration"))
    if expected_duration and actual_duration:
        allowed_difference = max(12.0, expected_duration * 0.20)
        difference = abs(actual_duration - expected_duration)
        if difference > allowed_difference:
            raise RuntimeError(
                f"{source_name} вернул аудио другой длительности: "
                f"в карточке {int(expected_duration)} сек., источник {int(actual_duration)} сек."
            )

    expected_title = str(expected_track.get("title") or "").strip()
    actual_title = str(actual_info.get("title") or "").strip()
    if expected_title and actual_title:
        # Используем тот же строгий матчинг, что и в поиске: совпадения
        # только общих слов вроде «I / go / to» недостаточно.
        actual_candidate = {
            "title": actual_title,
            "uploader": actual_info.get("uploader") or actual_info.get("artist") or actual_info.get("creator") or "",
        }
        if not _track_matches_track_query(actual_candidate, expected_title):
            raise RuntimeError(
                f"{source_name} вернул другую композицию: «{actual_info.get('title', '')}»"
            )

    expected_artist = norm_text(expected_track.get("uploader"))
    actual_artist = norm_text(
        actual_info.get("uploader") or actual_info.get("artist") or actual_info.get("creator")
    )
    if expected_artist and actual_artist and expected_artist not in ("неизвестен", "неизвестный автор"):
        from difflib import SequenceMatcher
        expected_words = set(expected_artist.split())
        actual_words = set(actual_artist.split())
        artist_coverage = len(expected_words & actual_words) / max(1, len(expected_words))
        artist_similarity = SequenceMatcher(None, expected_artist, actual_artist).ratio()
        if artist_similarity < 0.35 and artist_coverage < 0.5:
            raise RuntimeError(
                f"{source_name} вернул другого исполнителя: «{actual_info.get('uploader') or actual_info.get('artist') or actual_info.get('creator')}»"
            )


def get_soundcloud_stream_url(track):
    """Получает поток именно выбранной страницы SoundCloud и проверяет её метаданные."""
    if isinstance(track, str):
        track = {"url": track}
    track_url = str((track or {}).get("url") or "").strip()
    if not track_url:
        raise RuntimeError("Не указана ссылка SoundCloud")

    now = time.time()
    with stream_cache_lock:
        cached = stream_url_cache.get(track_url)
        if cached and len(cached) >= 3 and now - cached[1] < STREAM_CACHE_TTL:
            _validate_stream_metadata(track, cached[2], "SoundCloud")
            return cached[0]
        if cached:
            stream_url_cache.pop(track_url, None)

    options = {
        "format": "bestaudio[protocol^=http]/bestaudio/best",
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "skip_download": True, "socket_timeout": 15,
        "retries": 1, "extractor_retries": 1
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(track_url, download=False)
    if not isinstance(info, dict):
        raise RuntimeError("SoundCloud не вернул метаданные выбранного трека")

    _validate_stream_metadata(track, info, "SoundCloud")

    direct_url = info.get("url")
    if not direct_url:
        formats = info.get("requested_formats") or info.get("formats") or []
        audio_formats = [f for f in formats if f.get("url") and f.get("acodec") not in (None, "none")]
        if audio_formats:
            audio_formats.sort(key=lambda f: (f.get("vcodec") == "none", f.get("abr") or 0), reverse=True)
            direct_url = audio_formats[0]["url"]
    if not direct_url or urlparse(direct_url).scheme not in ("http", "https"):
        raise RuntimeError("SoundCloud не вернул прямую ссылку на аудио")

    cache_meta = {
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("artist"),
        "duration": info.get("duration"),
    }
    with stream_cache_lock:
        stream_url_cache[track_url] = (direct_url, now, cache_meta)
        if len(stream_url_cache) > 1000:
            stream_url_cache.pop(next(iter(stream_url_cache)))
    return direct_url


def get_audius_stream_url(track_id, expected_track=None):
    track_id = str(track_id or "").strip()
    if not track_id:
        raise RuntimeError("Не удалось определить ID трека Audius")
    raw = _get_audius_track_details(track_id)
    if not audius_track_allowed(raw, require_download=False):
        raise RuntimeError("Audius не разрешает воспроизведение этой версии")
    candidate = audius_candidate(raw)
    if candidate:
        _validate_stream_metadata(expected_track, candidate, "Audius")
    # Этот официальный endpoint отдаёт поток только выбранного ID трека.
    return f"{AUDIUS_API_BASE}/tracks/{track_id}/stream"


def _get_track_stream_url_single(track):
    """Возвращает поток только выбранной записи. Никаких похожих песен вместо неё."""
    source = str((track or {}).get("source") or "soundcloud").lower()

    if source == "internet_archive":
        return get_internet_archive_stream_url(
            (track or {}).get("ia_identifier"), expected_track=track
        )
    if source == "audius":
        return get_audius_stream_url(
            (track or {}).get("audius_id"), expected_track=track
        )
    if source == "soundcloud":
        return get_soundcloud_stream_url(track)
    raise RuntimeError(f"Неизвестный источник аудио: {source}")



def _hydrate_stream_track(track):
    """Восстанавливает метаданные по ссылке, когда Mini App передал URL без полей трека."""
    enriched = dict(track or {})
    if enriched.get("title") and enriched.get("title") != "Без названия":
        return enriched
    url = str(enriched.get("url") or "").strip()
    if not url:
        return enriched

    source = str(enriched.get("source") or "soundcloud").lower()
    try:
        if source == "internet_archive":
            identifier = str(enriched.get("ia_identifier") or "").strip()
            if not identifier:
                parsed = urlparse(url)
                parts = [part for part in parsed.path.split("/") if part]
                identifier = parts[1] if len(parts) >= 2 and parts[0] == "details" else ""
            payload = internet_archive_metadata(identifier)
            candidate = internet_archive_candidate(identifier, payload)
            if candidate:
                enriched.update(candidate)
                return enriched
        elif source == "audius":
            payload = audius_api_json("/resolve", {"url": url})
            raw = _audius_data_track(payload)
            candidate = audius_candidate(raw) if raw else None
            if candidate:
                enriched.update(candidate)
                return enriched
        else:
            info = resolve_soundcloud_metadata(url)
            if info:
                enriched["title"] = str(info.get("title") or enriched.get("title") or "Без названия")[:180]
                enriched["uploader"] = str(
                    info.get("uploader") or info.get("artist") or enriched.get("uploader") or "Неизвестен"
                )[:120]
                if info.get("duration"):
                    enriched["duration"] = int(float(info["duration"]))
                enriched["thumbnail"] = best_soundcloud_thumbnail(info) or enriched.get("thumbnail", "")
    except Exception as e:
        print(f"Mini App: не удалось получить метаданные для резервного поиска: {e!r}")
    return enriched


def get_track_stream_url(track):
    """Получает поток строго выбранного трека; при ошибке возвращает ошибку, а не замену."""
    enriched = _hydrate_stream_track(track)
    try:
        return _get_track_stream_url_single(enriched)
    except Exception as e:
        print(
            f"Mini App: не удалось получить именно выбранный трек "
            f"«{enriched.get('title', '')}» — {enriched.get('uploader', '')}: {e!r}"
        )
        raise RuntimeError(
            f"Не удалось воспроизвести выбранный трек «{enriched.get('title', 'Без названия')}" 
            f"— {enriched.get('uploader', 'Неизвестен')}». Другой трек вместо него не включён."
        ) from e



def prewarm_tracks_stream_urls(tracks, max_count=20):
    def _warm_one(item):
        if isinstance(item, dict) and item.get("url"):
            try:
                get_track_stream_url(item)
            except Exception as e:
                print(f"Предзагрузка аудио пропущена для {item.get('url')}: {e!r}")

    items = tracks[:max_count] if tracks else []
    if not items:
        return
    with ThreadPoolExecutor(max_workers=min(5, max(1, len(items)))) as executor:
        list(executor.map(_warm_one, items))


def handle_miniapp_stream(handler, query_params):
    auth_data = (query_params.get("auth") or [""])[0]
    user = validate_miniapp_request(handler, auth_data)
    if not user:
        return api_json(handler, 401, {"error": "Авторизация истекла. Закрой и снова открой приложение."})
    if not miniapp_is_subscribed(user["id"]):
        return api_json(handler, 403, {"error": "Сначала вступи в нашу группу."})

    raw_track = {
        "url": (query_params.get("url") or [""])[0],
        "title": (query_params.get("title") or [""])[0],
        "uploader": (query_params.get("artist") or [""])[0],
        "duration": (query_params.get("duration") or [""])[0],
        "source": (query_params.get("source") or [""])[0],
        "audius_id": (query_params.get("audius_id") or [""])[0],
        "ia_identifier": (query_params.get("ia_identifier") or [""])[0],
        "ia_file_name": (query_params.get("ia_file_name") or [""])[0],
        "license_url": (query_params.get("license_url") or [""])[0],
    }
    track = normalize_miniapp_track(raw_track)
    if not track:
        return api_json(handler, 400, {"error": "Некорректная ссылка на трек."})
    track = _hydrate_stream_track(track)
    try:
        direct_url = get_track_stream_url(track)
        handler.send_response(302)
        handler.send_header("Location", direct_url)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
    except Exception as e:
        print(f"Ошибка получения аудиопотока: {e!r}")
        api_json(handler, 502, {"error": "Не удалось получить аудио из доступных источников."})


def handle_miniapp_lyrics(handler, query_params):
    user = validate_miniapp_request(handler)
    if not user:
        return api_json(handler, 401, {"error": "Открой приложение через Telegram."})
    if not is_user_vip(user["id"]):
        return api_json(handler, 403, {"error": "👑 Тексты песен доступны только с VIP подпиской."})
    title = str((query_params.get("title") or [""])[0])[:200]
    artist = str((query_params.get("artist") or [""])[0])[:120]
    lyrics = fetch_lyrics(title, artist)
    if lyrics:
        return api_json(handler, 200, {"lyrics": lyrics[:12000]})
    api_json(handler, 200, {"lyrics": f"Текст песни «{title}» не найден в базе."})


def download_track_for_miniapp(user_id, track, effect="none"):
    """Совместимость: Mini App шлёт трек в личку пользователя."""
    send_track(user_id, track, effect, status_msg_id=None, user_id=user_id)


def handle_miniapp_post(handler, path, payload):
    user = validate_miniapp_request(handler)
    if not user:
        return api_json(handler, 401, {"error": "Открой приложение через Telegram."})
    user_id = user["id"]
    settings = get_user_settings(user_id)

    if path == "/api/settings":
        try:
            count = int(payload.get("count", settings.get("search_limit", 10)))
        except (TypeError, ValueError):
            return api_json(handler, 400, {"error": "Некорректное количество треков."})

        # Лимит: VIP до 50, обычный до 25
        max_possible = max_search_limit(user_id)
        if count < 5 or count > max_possible:
            return api_json(handler, 400, {"error": f"Лимит для вашего статуса: от 5 до {max_possible}."})

        theme = str(payload.get("theme", settings.get("theme", "default")))
        if theme not in ALL_THEMES:
            return api_json(handler, 400, {"error": "Неизвестная тема."})
        if theme != "default" and not is_user_vip(user_id):
            return api_json(handler, 403, {"error": "👑 Эта тема доступна только с VIP."})

        settings["search_limit"] = count
        settings["theme"] = theme
        request_save()
        return api_json(handler, 200, {"ok": True, "search_limit": count, "theme": theme})

    if path == "/api/invoice":
        # Создаёт ссылку на оплату Stars, которую Mini App открывает через tg.openInvoice
        plan = str(payload.get("plan", ""))
        days = {"week": 7, "month": 30}.get(plan)
        if not days:
            return api_json(handler, 400, {"error": "Неизвестный тариф."})
        info = VIP_PLANS[days]
        try:
            link = bot.create_invoice_link(
                title=info["title"],
                description=f"VIP подписка на {days} дн.: без обязательной подписки, до 50 треков, тексты, эффекты, темы.",
                payload=f"vip_{days}",
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label=info["title"], amount=info["stars"])]
            )
            return api_json(handler, 200, {"ok": True, "invoice_url": link})
        except Exception as e:
            print(f"Ошибка создания счёта: {e}")
            return api_json(handler, 500, {"error": "Не удалось создать счёт. Купи VIP командой /vip в боте."})

    if path == "/api/favorite":
        track = normalize_miniapp_track(payload.get("track"))
        if not track:
            return api_json(handler, 400, {"error": "Некорректный трек."})
        favorites = settings.setdefault("favorites", [])
        existing = next((item for item in favorites if item.get("url") == track["url"]), None)
        if existing:
            favorites.remove(existing)
            saved = False
        else:
            favorites.insert(0, track)
            del favorites[100:]
            saved = True
            threading.Thread(target=prewarm_tracks_stream_urls, args=([track],), daemon=True).start()
        request_save()
        return api_json(handler, 200, {"ok": True, "saved": saved, "favorites": favorites})

    if path == "/api/history":
        tracks = payload.get("tracks") or []
        if not isinstance(tracks, list):
            return api_json(handler, 400, {"error": "Некорректный список треков."})
        for raw in reversed(tracks[:25]):
            track = normalize_miniapp_track(raw)
            if track:
                add_to_user_history(settings, track)
        request_save()
        return api_json(handler, 200, {"ok": True, "history": settings["history"]})

    if path == "/api/playlist":
        action = payload.get("action")
        playlists = settings.setdefault("playlists", [])
        if action == "create":
            name = str(payload.get("name", "")).strip()[:40]
            if not name:
                return api_json(handler, 400, {"error": "Введи название плейлиста."})
            if any(p.get("name", "").casefold() == name.casefold() for p in playlists):
                return api_json(handler, 409, {"error": "Такой плейлист уже существует."})
            playlists.insert(0, {"id": str(int(time.time() * 1000)), "name": name, "tracks": []})
        elif action == "delete":
            playlist_id = str(payload.get("playlist_id", ""))
            settings["playlists"] = [p for p in playlists if str(p.get("id")) != playlist_id]
        elif action == "add_track":
            track = normalize_miniapp_track(payload.get("track"))
            if not track:
                return api_json(handler, 400, {"error": "Некорректный трек."})
            playlist_name = str(payload.get("playlist_name", "")).strip()[:40]
            if not playlist_name:
                return api_json(handler, 400, {"error": "Введи название плейлиста."})
            playlist = next((p for p in playlists if p.get("name", "").casefold() == playlist_name.casefold()), None)
            if not playlist:
                playlist = {"id": str(int(time.time() * 1000)), "name": playlist_name, "tracks": []}
                playlists.insert(0, playlist)
            if not any(item.get("url") == track["url"] for item in playlist.setdefault("tracks", [])):
                playlist["tracks"].insert(0, track)
        else:
            return api_json(handler, 400, {"error": "Неизвестное действие с плейлистом."})
        del settings["playlists"][100:]
        request_save()
        return api_json(handler, 200, {"ok": True, "playlists": settings["playlists"]})

    if path == "/api/download":
        if not miniapp_is_subscribed(user_id):
            return api_json(handler, 403, {
                "error": "Сначала вступи в нашу группу (или купи VIP).",
                "subscription_required": True,
                "invite_url": "https://t.me/+8JYfdTy2YhE0NDJi"
            })
        track = normalize_miniapp_track(payload.get("track"))
        if not track:
            return api_json(handler, 400, {"error": "Некорректный трек."})

        effect = str(payload.get("effect", "none"))
        if effect != "none" and effect not in VIP_EFFECTS:
            return api_json(handler, 400, {"error": "Неизвестный эффект."})
        if effect != "none" and not is_user_vip(user_id):
            return api_json(handler, 403, {"error": "👑 Аудио-эффекты доступны только с VIP подпиской!"})

        add_to_user_history(settings, track)
        request_save()
        threading.Thread(target=download_track_for_miniapp, args=(user_id, track, effect), daemon=True).start()
        return api_json(handler, 202, {
            "ok": True,
            "message": "Отправляю трек в Telegram. Он появится в чате с ботом."
        })

    return api_json(handler, 404, {"error": "Неизвестный API-адрес."})


# --- ОБРАБОТКА ДАННЫХ ИЗ WEB APP ---
@bot.message_handler(content_types=['web_app_data'])
def handle_web_app_data(message):
    try:
        data = json.loads(message.web_app_data.data)
        if data.get("action") == "set_tracks_per_page":
            count = int(data.get("count", 10))
            user_id_str = str(message.from_user.id)

            if user_id_str not in user_settings:
                user_settings[user_id_str] = {}

            user_settings[user_id_str]["search_limit"] = max(5, min(max_search_limit(message.from_user.id), count))
            request_save()

            bot.send_message(message.chat.id, f"✅ Настройки успешно сохранены!\nВыдача: {user_settings[user_id_str]['search_limit']} треков на страницу.")
    except Exception as e:
        print(f"Ошибка при получении данных из Web App: {e}")


# --- ОСНОВНОЙ БЛОК ЗАПУСКА С ЗАЩИТОЙ ОТ 409 ОШИБКИ ---
if __name__ == '__main__':
    print("🚀 Запуск бота...")
    try:
        bot.set_my_commands([
            telebot.types.BotCommand("app", "🎧 Открыть плеер"),
            telebot.types.BotCommand("vip", "👑 VIP подписка"),
            telebot.types.BotCommand("lyrics", "📜 Текст песни (VIP)"),
            telebot.types.BotCommand("settings", "⚙️ Настройки поиска"),
            telebot.types.BotCommand("donate", "Поддержка разработчика")
        ])
        print("✅ Меню команд обновлено.")
    except Exception as e:
        print(f"⚠️ Не удалось обновить меню команд: {e}")
    while True:
        try:
            bot.remove_webhook()
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=10)
        except ApiTelegramException as e:
            if e.error_code == 409:
                print("⚠️ Конфликт сессий (409), повтор через 5 секунд...")
                time.sleep(5)
            else:
                print(f"❌ Ошибка Telegram API: {e}")
                time.sleep(5)
        except Exception as e:
            print(f"❌ Ошибка: {e}")
            time.sleep(5)
