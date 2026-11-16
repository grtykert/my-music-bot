import os
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
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode, urljoin, quote, unquote
from html.parser import HTMLParser
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
# Метаданные последних треков Mini App нужны, чтобы при DRM найти точное название/исполнителя.
miniapp_track_cache = {}
miniapp_track_cache_lock = threading.Lock()
MINIAPP_TRACK_CACHE_LIMIT = 1500
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
        "👋 Привет! Напиши название песни, и я попробую её найти. 🎵\n\n"
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


def soundcloud_search(query, count=30):
    # Умный поиск: основной запрос, при слабом результате — варианты запроса, затем ранжирование.
    query = " ".join((query or "").split())
    if not query:
        return []
    first_error = None
    tracks = []
    try:
        tracks = _sc_flat_search(query, count)
    except Exception as e:
        first_error = e
        print(f"Основной поиск не удался: {e!r}")

    scored = score_tracks(dedupe_tracks(tracks), [query])
    if not scored or scored[0][0] < WEAK_SCORE:
        variants = query_variants(query)
        if variants:
            with ThreadPoolExecutor(max_workers=len(variants)) as pool:
                futures = [pool.submit(_sc_flat_search, v, count) for v in variants]
                for f in futures:
                    try:
                        tracks.extend(f.result())
                    except Exception as e:
                        print(f"Поиск по варианту запроса не удался: {e!r}")
            scored = score_tracks(dedupe_tracks(tracks), [query] + variants)

    if not scored and first_error:
        raise first_error
    return [t for _, t in scored]



# ============================================================
#       РЕЗЕРВНЫЙ ПОИСК ПО МУЗЫКАЛЬНЫМ САЙТАМ (ЛИЦЕНЗИОННЫЙ)
# ============================================================
# Резервный поиск не обходит DRM и не скачивает треки, если право на
# распространение/скачивание нельзя подтвердить лицензией источника.
FALLBACK_SEARCH_ENABLED = os.environ.get("FALLBACK_SEARCH_ENABLED", "1").strip().lower() not in ("0", "false", "no")
FALLBACK_SEARCH_LIMIT = 12
FALLBACK_DOWNLOAD_ATTEMPTS = 3
FALLBACK_ALLOWED_HOSTS = (
    "archive.org",
    "bandcamp.com",
    "jamendo.com",
    "freemusicarchive.org",
    "ccmixter.org",
    "musopen.org",
    "commons.wikimedia.org",
)


class _DuckDuckGoResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        data = dict(attrs)
        classes = (data.get("class") or "").split()
        href = data.get("href")
        if href and "result__a" in classes:
            self.links.append(href)


def _safe_http_json(url, timeout=10, max_bytes=2 * 1024 * 1024):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SoundMusicBot/1.0)"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("Ответ поискового сервиса слишком большой")
    return json.loads(raw.decode("utf-8", errors="replace"))


def _is_fallback_host(url):
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in FALLBACK_ALLOWED_HOSTS)


def _duckduckgo_music_urls(query, limit=FALLBACK_SEARCH_LIMIT):
    """Получает ссылки из HTML-выдачи поисковика DuckDuckGo."""
    if not FALLBACK_SEARCH_ENABLED or not query:
        return []
    search_url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query, "kl": "wt-wt"})
    request = urllib.request.Request(
        search_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8"
        }
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        html = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
    parser = _DuckDuckGoResultParser()
    parser.feed(html)
    found = []
    seen = set()
    for raw_url in parser.links:
        url = raw_url
        parsed = urlparse(url)
        if "duckduckgo.com" in (parsed.hostname or ""):
            wrapped = parse_qs(parsed.query).get("uddg")
            if wrapped:
                url = unquote(wrapped[0])
        url = urljoin("https://duckduckgo.com", url)
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or not _is_fallback_host(url):
            continue
        # Оставляем только страницы; прямые рекламные/трекерные URL исключены.
        clean_url = parsed._replace(fragment="").geturl()
        if clean_url not in seen:
            seen.add(clean_url)
            found.append(clean_url)
        if len(found) >= limit:
            break
    return found


def _license_is_redistributable(*values):
    """Разрешаем только явно указанную CC0/Public Domain/CC BY/CC BY-SA.

    Лицензии NonCommercial и NoDerivatives не принимаем: бот может
    преобразовывать аудио и распространять его пользователям.
    """
    text = " ".join(str(v or "") for v in values).casefold().replace("_", "-")
    text = re.sub(r"\s+", " ", text)
    if not text.strip():
        return False
    denied = ("noncommercial", "non-commercial", "by-nc", "no derivatives", "no-derivatives", "by-nd")
    if any(marker in text for marker in denied):
        return False
    allowed = (
        "public domain", "public-domain", "creativecommons.org/publicdomain/zero",
        "creativecommons.org/licenses/by/",
        "cc0", "cc by", "cc-by",
    )
    return any(marker in text for marker in allowed)


def _permission_from_extractor(info):
    if not isinstance(info, dict):
        return None
    license_values = [
        info.get("license"), info.get("license_url"), info.get("licenseurl"),
        info.get("license_name"), info.get("license_text")
    ]
    if _license_is_redistributable(*license_values):
        return " / ".join(str(x) for x in license_values if x)
    return None


def _extractor_has_audio_download(info):
    if not isinstance(info, dict):
        return False
    direct = info.get("url") and info.get("acodec") not in (None, "none")
    if direct:
        return True
    for fmt in info.get("formats") or []:
        if fmt.get("url") and fmt.get("acodec") not in (None, "none"):
            return True
    return False


def _token_coverage(expected, actual):
    expected_tokens = [t for t in _query_tokens(expected) if len(t) > 1]
    actual_tokens = set(norm_text(actual).split())
    if not expected_tokens:
        return 0.0
    matched = sum(1 for token in expected_tokens if token in actual_tokens or any(x.startswith(token) for x in actual_tokens))
    return matched / len(expected_tokens)


def _fallback_candidate_matches(info, expected_title, expected_artist, expected_duration=None):
    """Строго сверяет название и исполнителя; сомнительные совпадения отбрасывает."""
    if not isinstance(info, dict):
        return False
    candidate_title = info.get("track") or info.get("title") or ""
    candidate_artist = (
        info.get("artist") or info.get("artists") or info.get("creator")
        or info.get("creators") or info.get("album_artist") or info.get("uploader") or ""
    )
    if isinstance(candidate_artist, (list, tuple)):
        candidate_artist = " ".join(str(x) for x in candidate_artist)
    title_coverage = _token_coverage(expected_title, candidate_title)
    if title_coverage < 0.65:
        return False
    if expected_artist:
        artist_coverage = _token_coverage(expected_artist, candidate_artist)
        combined_coverage = _token_coverage(expected_artist, candidate_title)
        if max(artist_coverage, combined_coverage) < 0.50:
            return False
    candidate_duration = info.get("duration")
    try:
        if expected_duration and candidate_duration:
            if abs(float(expected_duration) - float(candidate_duration)) > max(25, float(expected_duration) * 0.18):
                return False
    except (TypeError, ValueError):
        pass
    query = f"{expected_artist} {expected_title}".strip()
    candidate = {
        "title": candidate_title,
        "uploader": candidate_artist,
        "duration": candidate_duration,
    }
    return bool(score_tracks([candidate], query) and score_tracks([candidate], query)[0][0] >= 9.5)


def _archive_license_search(title, artist, duration=None, limit=5):
    """Ищет аудио в Internet Archive и принимает только разрешающие лицензии."""
    if not FALLBACK_SEARCH_ENABLED or not title:
        return []
    safe_title = re.sub(r'["\\]+', " ", title).strip()[:100]
    safe_artist = re.sub(r'["\\]+', " ", artist or "").strip()[:80]
    q = f'title:("{safe_title}") AND mediatype:audio'
    if safe_artist:
        q += f' AND creator:("{safe_artist}")'
    params = urlencode({
        "q": q,
        "fl[]": ["identifier", "title", "creator", "licenseurl"],
        "rows": min(10, max(1, limit)),
        "page": 1,
        "output": "json",
    }, doseq=True)
    try:
        search_data = _safe_http_json("https://archive.org/advancedsearch.php?" + params, timeout=12)
    except Exception as e:
        print(f"Резервный поиск Internet Archive недоступен: {e!r}")
        return []

    docs = ((search_data or {}).get("response") or {}).get("docs") or []
    results = []
    for doc in docs[:limit]:
        identifier = str(doc.get("identifier") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
            continue
        try:
            metadata_data = _safe_http_json(f"https://archive.org/metadata/{identifier}", timeout=12)
            metadata = metadata_data.get("metadata") or {}
            license_url = metadata.get("licenseurl") or doc.get("licenseurl") or ""
            license_name = metadata.get("license") or ""
            if not _license_is_redistributable(license_url, license_name):
                continue
            item_title = metadata.get("title") or doc.get("title") or ""
            creator = metadata.get("creator") or doc.get("creator") or ""
            if isinstance(creator, (list, tuple)):
                creator = " ".join(str(x) for x in creator)
            # Для альбомов заголовок записи может не совпадать с названием композиции,
            # поэтому окончательное совпадение проверяем также по имени аудиофайла.
            if artist and creator and _token_coverage(artist, creator) < 0.50:
                continue
            item_title_coverage = _token_coverage(title, item_title)

            audio_files = []
            for file_info in metadata_data.get("files") or []:
                name = str(file_info.get("name") or "")
                ext = os.path.splitext(name.lower())[1]
                if ext not in (".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav"):
                    continue
                try:
                    size = int(file_info.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                if size and size > 120 * 1024 * 1024:
                    continue
                file_title = os.path.splitext(os.path.basename(name))[0].replace("_", " ")
                coverage = _token_coverage(title, file_title)
                audio_files.append((coverage, name, file_info))
            if not audio_files:
                continue
            audio_files.sort(key=lambda item: item[0], reverse=True)
            best_coverage, filename, file_info = audio_files[0]
            if best_coverage < 0.65 and not (len(audio_files) == 1 and item_title_coverage >= 0.65):
                continue
            file_url = f"https://archive.org/download/{identifier}/{quote(filename, safe='/')}"
            results.append({
                "url": file_url,
                "title": title if best_coverage >= 0.65 else str(item_title)[:180],
                "uploader": str(creator or artist or "Internet Archive")[:120],
                "duration": duration,
                "thumbnail": "",
                "_fallback_verified": True,
                "_fallback_source": "Internet Archive",
                "_fallback_page": f"https://archive.org/details/{identifier}",
                "_fallback_license": str(license_url or license_name)[:300],
            })
        except Exception as e:
            print(f"Пропуск результата Internet Archive {identifier}: {e!r}")
    return results


def _web_music_site_search(title, artist, duration=None, limit=8):
    """Ищет страницы на нескольких музыкальных площадках через поисковик."""
    if not FALLBACK_SEARCH_ENABLED:
        return []
    query_text = f'"{artist}" "{title}"' if artist else f'"{title}"'
    sites = " OR ".join(
        f"site:{domain}" for domain in (
            "bandcamp.com", "jamendo.com", "freemusicarchive.org",
            "ccmixter.org", "musopen.org", "commons.wikimedia.org"
        )
    )
    search_queries = [f"{query_text} ({sites})", f'{query_text} "Creative Commons" download']
    page_urls = []
    seen_urls = set()
    for search_query in search_queries:
        try:
            for url in _duckduckgo_music_urls(search_query, limit=limit):
                if url not in seen_urls:
                    seen_urls.add(url)
                    page_urls.append(url)
        except Exception as e:
            print(f"Поисковик не ответил на резервный запрос: {e!r}")
    if not page_urls:
        return []

    def inspect_page(page_url):
        options = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "socket_timeout": 10,
            "retries": 1, "extractor_retries": 1,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(page_url, download=False)
            if not isinstance(info, dict) or info.get("availability") not in (None, "public"):
                return None
            if not _extractor_has_audio_download(info):
                return None
            license_value = _permission_from_extractor(info)
            if not license_value:
                return None
            candidate_url = info.get("webpage_url") or page_url
            if not _is_fallback_host(candidate_url):
                return None
            artist_value = info.get("artist") or info.get("creator") or info.get("uploader") or artist
            if isinstance(artist_value, (list, tuple)):
                artist_value = " ".join(str(x) for x in artist_value)
            if not _fallback_candidate_matches(info, title, artist, duration):
                return None
            return {
                "url": candidate_url,
                "title": str(info.get("track") or info.get("title") or title)[:180],
                "uploader": str(artist_value or artist or "Неизвестен")[:120],
                "thumbnail": str(info.get("thumbnail") or "")[:1000],
                "duration": info.get("duration") or duration,
                "_fallback_verified": True,
                "_fallback_source": info.get("extractor") or (urlparse(candidate_url).hostname or "Музыкальный сайт"),
                "_fallback_page": candidate_url,
                "_fallback_license": license_value[:300],
            }
        except Exception as e:
            print(f"Не удалось проверить музыкальную страницу {page_url}: {e!r}")
            return None

    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for item in pool.map(inspect_page, page_urls[:limit]):
            if item:
                results.append(item)
    return results


def search_licensed_fallback_tracks(title, uploader="", duration=None, limit=5):
    """Единая резервная выдача: поисковик + Internet Archive + строгая проверка лицензии."""
    if not FALLBACK_SEARCH_ENABLED:
        return []
    expected_artist, expected_title = split_artist_title(title or "", uploader or "")
    expected_title = expected_title or (title or "").strip()
    expected_artist = expected_artist or (uploader or "").strip()
    if not expected_title:
        return []
    print(f"🔎 Резервный поиск: artist={expected_artist!r}, title={expected_title!r}")
    found = []
    found.extend(_archive_license_search(expected_title, expected_artist, duration, limit=limit))
    found.extend(_web_music_site_search(expected_title, expected_artist, duration, limit=limit))
    found = dedupe_tracks([x for x in found if x.get("_fallback_verified")])
    ranked = score_tracks(found, f"{expected_artist} {expected_title}".strip())
    return [track for score, track in ranked if score >= 9.5][:limit]


def _should_try_fallback(error):
    text = str(error).casefold()
    markers = (
        "drm", "protected", "no video formats", "requested format is not available",
        "no suitable formats", "unable to download", "http error 403", "http error 401",
        "403 client error", "401 client error", "unsupported url", "not available",
        "forbidden", "signature extraction failed", "no formats found",
        "encrypted", "widevine", "decryption", "drm protected"
    )
    return any(marker in text for marker in markers)

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
            tracks = soundcloud_search(search_text, 20)[:10]

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
                info, audio_filename, thumbnail_filename = prepare_track_file(track_url, workdir)

            with open(audio_filename, "rb") as audio:
                thumb_file = open(thumbnail_filename, "rb") if thumbnail_filename and os.path.exists(thumbnail_filename) else None
                try:
                    backup_msg = bot.send_audio(
                        chat_id=BACKUP_CHANNEL_ID,
                        audio=audio,
                        title=info.get('title', title),
                        performer=info.get('uploader', uploader),
                        thumbnail=thumb_file
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
        msg = bot.reply_to(message, "🔍 Ищу варианты...")
    else:
        msg = message

    def worker():
        try:
            # ЛИМИТЫ: VIP до 50, обычный до 25 — по user_id, не по chat_id
            max_possible = max_search_limit(user_id)
            user_limit = int((user_settings.get(str(user_id)) or {}).get("search_limit", 10))
            effective_limit = max(5, min(max_possible, user_limit))

            limit_ydl = 50 if is_random else max(20, effective_limit * page)
            normalized_query = " ".join(query.casefold().split())
            cache_key = (normalized_query, limit_ydl)
            now = time.time()

            with search_cache_lock:
                cached = search_results_cache.get(cache_key)
                if cached and now - cached[0] < SEARCH_CACHE_TTL:
                    all_tracks = cached[1]
                else:
                    if cached:
                        search_results_cache.pop(cache_key, None)
                    all_tracks = None

            if all_tracks is None:
                try:
                    all_tracks = soundcloud_search(query, max(30, limit_ydl))
                except Exception as soundcloud_error:
                    print(f"SoundCloud не дал результаты, включаю резервный поиск: {soundcloud_error!r}")
                    all_tracks = []
                if not all_tracks:
                    all_tracks = search_licensed_fallback_tracks(query, limit=effective_limit)

                with search_cache_lock:
                    search_results_cache[cache_key] = (time.time(), all_tracks)
                    if len(search_results_cache) > 100:
                        oldest_key = min(search_results_cache, key=lambda key: search_results_cache[key][0])
                        search_results_cache.pop(oldest_key, None)

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
                source_mark = "🌐" if track.get("_fallback_verified") else "🎵"
                markup.add(InlineKeyboardButton(text=f"{source_mark} {i+1}. {title}", callback_data=f"dl_{sid}_{i}"))

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



def _try_send_licensed_fallback(chat_id, target_track, effect, status_msg_id, finish_ok):
    """Пробует несколько проверенных резервных вариантов, не обходя DRM."""
    if not FALLBACK_SEARCH_ENABLED or target_track.get("_fallback_verified"):
        return False
    try:
        candidates = search_licensed_fallback_tracks(
            target_track.get("title", ""),
            target_track.get("uploader", ""),
            target_track.get("duration"),
            limit=FALLBACK_DOWNLOAD_ATTEMPTS,
        )
    except Exception as search_error:
        print(f"Ошибка резервного поиска: {search_error!r}")
        return False

    for candidate in candidates[:FALLBACK_DOWNLOAD_ATTEMPTS]:
        fallback_dir = None
        try:
            if not candidate.get("_fallback_verified") or not candidate.get("_fallback_license"):
                continue
            with download_slots:
                fallback_dir = tempfile.mkdtemp(prefix="licensed_fallback_")
                info, audio_filename, thumbnail_filename = prepare_track_file(
                    candidate["url"], fallback_dir, effect
                )
                out_title = candidate.get("title") or info.get("title") or target_track.get("title", "Музыка")
                if effect != "none":
                    out_title = f"{out_title} ({EFFECT_NAMES[effect]})"
                out_artist = candidate.get("uploader") or info.get("artist") or info.get("uploader") or target_track.get("uploader", "")
                caption = (
                    f"Источник: {candidate.get('_fallback_source', 'музыкальная площадка')}\n"
                    f"Лицензия: {candidate.get('_fallback_license', '')}\n"
                    f"{candidate.get('_fallback_page', '')}"
                )[:950]
                with open(audio_filename, "rb") as audio:
                    thumb_file = open(thumbnail_filename, "rb") if thumbnail_filename and os.path.exists(thumbnail_filename) else None
                    try:
                        sent = bot.send_audio(
                            chat_id, audio, title=out_title, performer=out_artist,
                            thumbnail=thumb_file, caption=caption
                        )
                    finally:
                        if thumb_file:
                            thumb_file.close()
            if effect == "none":
                audio_cache[candidate["url"]] = sent.audio.file_id
            finish_ok(out_title, out_artist)
            print(f"✅ Отправлен резервный трек с лицензией: {candidate.get('_fallback_page')}")
            return True
        except Exception as fallback_error:
            print(f"Резервный вариант не подошёл ({candidate.get('_fallback_page')}): {fallback_error!r}")
        finally:
            if fallback_dir:
                shutil.rmtree(fallback_dir, ignore_errors=True)
    return False

# --- ОТПРАВКА ТРЕКА В ЧАТ (общая для бота и Mini App) ---
def send_track(chat_id, track, effect="none", status_msg_id=None, user_id=None):
    """Скачивает трек и отправляет в Telegram. Для VIP может применить аудиоэффект."""
    user_id = user_id or chat_id
    if effect not in VIP_EFFECTS:
        effect = "none"
    if effect != "none" and not is_user_vip(user_id):
        effect = "none"

    track_url = track["url"]
    title = track["title"]
    uploader = track["uploader"]
    workdir = None

    def finish_ok(shown_title, shown_uploader):
        if status_msg_id:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(text="🔍 Найти другой трек", callback_data="tosearch"))
            try:
                bot.edit_message_text(
                    text=f"▶️ **Плеер:**\n🎵 {shown_title}\n🧑‍🎤 {shown_uploader}",
                    chat_id=chat_id, message_id=status_msg_id,
                    parse_mode="Markdown", reply_markup=markup
                )
            except Exception:
                pass
        count_download()

    try:
        # Кэш используем только для обычного звука
        cached_file_id = audio_cache.get(track_url)
        if effect == "none" and cached_file_id and not str(cached_file_id).startswith("http"):
            try:
                bot.send_audio(chat_id, cached_file_id, title=title, performer=uploader)
                finish_ok(title, uploader)
                return
            except Exception as e:
                print(f"file_id из кэша не сработал, качаем заново: {e}")
                audio_cache.pop(track_url, None)

        with download_slots:
            workdir = tempfile.mkdtemp(prefix="trk_")
            info, audio_filename, thumbnail_filename = prepare_track_file(track_url, workdir, effect)

            is_verified_fallback = bool(track.get("_fallback_verified"))
            out_title = (track.get("title") if is_verified_fallback else info.get("title")) or title
            if effect != "none":
                out_title = f"{out_title} ({EFFECT_NAMES[effect]})"
            out_uploader = (track.get("uploader") if is_verified_fallback else info.get("uploader")) or uploader
            caption = None
            if is_verified_fallback:
                caption = (
                    f"Источник: {track.get('_fallback_source', 'музыкальная площадка')}\n"
                    f"Лицензия: {track.get('_fallback_license', '')}\n"
                    f"{track.get('_fallback_page', '')}"
                )[:950]

            with open(audio_filename, "rb") as audio:
                thumb_file = open(thumbnail_filename, "rb") if thumbnail_filename and os.path.exists(thumbnail_filename) else None
                try:
                    sent = bot.send_audio(chat_id, audio, title=out_title,
                                          performer=out_uploader, thumbnail=thumb_file,
                                          caption=caption)
                finally:
                    if thumb_file:
                        thumb_file.close()

        if effect == "none":
            audio_cache[track_url] = sent.audio.file_id
        finish_ok(out_title, out_uploader)
    except Exception as e:
        report_error(f"Ошибка загрузки трека {track_url}", e)
        if _should_try_fallback(e) and _try_send_licensed_fallback(
            chat_id, track, effect, status_msg_id, finish_ok
        ):
            return
        reason = explain_error(e)
        text = f"❌ Не удалось скачать «{title[:80]}»."
        if track.get("_fallback_verified"):
            text += "\nПроверенный источник сейчас не отдаёт аудиофайл. Выбери другой найденный вариант."
        elif _should_try_fallback(e):
            text += "\n🔎 Проверил другие музыкальные площадки, но не нашёл точную версию с подтверждённой разрешающей лицензией."
        else:
            text += " Попробуй выбрать другой результат."
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
    if parsed.scheme != "https" or not (host == "soundcloud.com" or host.endswith(".soundcloud.com")):
        return None
    thumbnail = str(raw.get("thumbnail") or "").strip()[:1000]
    thumb_parsed = urlparse(thumbnail)
    if thumbnail and not (thumb_parsed.scheme == "https" and thumb_parsed.hostname):
        thumbnail = ""
    try:
        duration = int(raw.get("duration")) if raw.get("duration") is not None else None
    except (TypeError, ValueError):
        duration = None
    return {
        "url": track_url,
        "title": str(raw.get("title") or "Без названия")[:180],
        "uploader": str(raw.get("uploader") or "Неизвестен")[:120],
        "thumbnail": thumbnail,
        "duration": duration
    }


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
               if isinstance(track, dict) and not track.get("thumbnail") and track.get("url")][:max_items]
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
    history = settings.get("history", [])
    with miniapp_track_cache_lock:
        for saved_track in (favs + history):
            if isinstance(saved_track, dict) and saved_track.get("url"):
                miniapp_track_cache[saved_track["url"]] = dict(saved_track)
        while len(miniapp_track_cache) > MINIAPP_TRACK_CACHE_LIMIT:
            miniapp_track_cache.pop(next(iter(miniapp_track_cache)))
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
    query = str((query_params.get("q") or [""])[0]).strip()[:100]
    search_type = str((query_params.get("type") or ["track"])[0]).strip().lower()
    if not query:
        return api_json(handler, 400, {"error": "Введи имя автора или название трека."})
    settings = get_user_settings(user["id"])

    # VIP ЛИМИТ: до 50, обычный до 25
    max_possible = max_search_limit(user["id"])
    limit = max(5, min(max_possible, int(settings.get("search_limit", 10))))

    try:
        fetch_count = min(100, max(30, limit * 3) if search_type == 'author' else max(30, limit * 2))
        tracks = soundcloud_search(query, fetch_count)

        if search_type == "author":
            q_low = query.lower()
            exact_author = [t for t in tracks if q_low in (t.get("uploader") or "").lower()]
            other_author = [t for t in tracks if q_low not in (t.get("uploader") or "").lower()]
            tracks = (exact_author + other_author)[:limit]
        else:
            tracks = tracks[:limit]

        missing = [i for i, track in enumerate(tracks) if not track.get("thumbnail")][:min(8, len(tracks))]
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
            with miniapp_track_cache_lock:
                for found_track in tracks:
                    if isinstance(found_track, dict) and found_track.get("url"):
                        miniapp_track_cache[found_track["url"]] = dict(found_track)
                while len(miniapp_track_cache) > MINIAPP_TRACK_CACHE_LIMIT:
                    miniapp_track_cache.pop(next(iter(miniapp_track_cache)))
            # Прогрев теперь тоже использует резервный поиск при DRM,
            # поэтому ошибка обнаруживается ещё до нажатия Play.
            threading.Thread(target=prewarm_tracks_stream_urls, args=(tracks[:2],), daemon=True).start()
        api_json(handler, 200, {"tracks": tracks, "query": query})
    except Exception as e:
        report_error(f"Ошибка поиска Mini App «{query}»", e)
        api_json(handler, 500, {"error": "Не удалось выполнить поиск. Попробуй ещё раз."})


def _extract_direct_audio_url(source_url):
    """Получает прямой URL аудио с SoundCloud или проверенной резервной площадки."""
    options = {
        "format": "bestaudio[protocol^=http]/bestaudio/best",
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "skip_download": True, "socket_timeout": 15,
        "retries": 1, "extractor_retries": 1
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(source_url, download=False)
    direct_url = info.get("url")
    if not direct_url:
        formats = info.get("requested_formats") or info.get("formats") or []
        audio_formats = [f for f in formats if f.get("url") and f.get("acodec") not in (None, "none")]
        if audio_formats:
            audio_formats.sort(key=lambda f: (f.get("vcodec") == "none", f.get("abr") or 0), reverse=True)
            direct_url = audio_formats[0]["url"]
    if not direct_url or urlparse(direct_url).scheme not in ("http", "https"):
        raise RuntimeError("Источник не вернул прямую ссылку на аудио")
    return direct_url


def _get_cached_miniapp_track(track_url):
    with miniapp_track_cache_lock:
        item = miniapp_track_cache.get(track_url)
        return dict(item) if isinstance(item, dict) else None


def get_soundcloud_stream_url(track_url, track_info=None):
    now = time.time()
    with stream_cache_lock:
        cached = stream_url_cache.get(track_url)
        if cached and (now - cached[1] < STREAM_CACHE_TTL):
            return cached[0]

    try:
        direct_url = _extract_direct_audio_url(track_url)
    except Exception as primary_error:
        if not _should_try_fallback(primary_error):
            raise

        track_info = track_info or _get_cached_miniapp_track(track_url)
        if not track_info or not track_info.get("title") or track_info.get("title") == "Без названия":
            print("⚠️ DRM SoundCloud, но метаданные трека не найдены — резервный поиск пропущен")
            raise

        print(
            f"🔎 Mini App: SoundCloud не отдаёт аудио (DRM). "
            f"Запускаю резерв для {track_info.get('uploader', '')} — {track_info.get('title', '')}"
        )
        candidates = search_licensed_fallback_tracks(
            track_info.get("title", ""),
            track_info.get("uploader", ""),
            track_info.get("duration"),
            limit=FALLBACK_DOWNLOAD_ATTEMPTS,
        )
        direct_url = None
        for candidate in candidates[:FALLBACK_DOWNLOAD_ATTEMPTS]:
            if not candidate.get("_fallback_verified") or not candidate.get("_fallback_license"):
                continue
            try:
                direct_url = _extract_direct_audio_url(candidate["url"])
                print(
                    f"✅ Mini App: резерв найден: {candidate.get('_fallback_page')} "
                    f"| лицензия: {candidate.get('_fallback_license')}"
                )
                break
            except Exception as fallback_error:
                print(f"Резервный поток не открылся ({candidate.get('_fallback_page')}): {fallback_error!r}")
        if not direct_url:
            print("❌ Mini App: резервный поиск не нашёл подходящий источник с подтверждённой лицензией")
            raise RuntimeError(
                "SoundCloud защищён DRM; резервный источник с точным совпадением и разрешающей лицензией не найден"
            ) from primary_error

    with stream_cache_lock:
        stream_url_cache[track_url] = (direct_url, time.time())
        if len(stream_url_cache) > 1000:
            stream_url_cache.pop(next(iter(stream_url_cache)))
    return direct_url


def prewarm_tracks_stream_urls(tracks, max_count=20):
    def _warm_one(item):
        if not isinstance(item, dict):
            return
        url = item.get("url")
        if url:
            try:
                get_soundcloud_stream_url(url, item)
            except Exception as e:
                print(f"Предварительная проверка аудио не прошла для {item.get('title', url)}: {e!r}")

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
    track_url = (query_params.get("url") or [""])[0]
    normalized = normalize_miniapp_track({"url": track_url})
    if not normalized:
        return api_json(handler, 400, {"error": "Некорректная ссылка на трек."})
    track_info = _get_cached_miniapp_track(track_url) or normalized
    try:
        direct_url = get_soundcloud_stream_url(track_url, track_info)
        handler.send_response(302)
        handler.send_header("Location", direct_url)
        handler.send_header("Cache-Control", "public, max-age=1800")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
    except Exception as e:
        print(f"Ошибка получения аудиопотока: {e!r}")
        api_json(handler, 502, {"error": "Не удалось получить аудио. SoundCloud может защищать трек DRM, а разрешённая резервная версия не найдена."})


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
