import os
import re
import sys
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, InputMediaAudio
from telebot.apihelper import ApiTelegramException
import yt_dlp
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, parse_qsl, urlencode
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
tracks_cache = {}
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

# Переменные хранения данных и мьютекс для потокобезопасности
active_users = {}
stats_data = {"total_downloads": 0}
audio_cache = {}
user_settings = {}  # Хранение настроек пользователей
backup_msg_id = None
data_lock = threading.Lock()
users_offset = 0  # Поправка для ручной корректировки цифры в статистике

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
    save_all_data()
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


# --- СИСТЕМА ЕДИНОГО БЭКАПА ЧЕРЕЗ ЗАКРЕП ---
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


def save_all_data():
    global backup_msg_id
    with data_lock:
        try:
            active_users[str(ADMIN_ID)] = time.time()
            data = {
                "users": active_users,
                "stats": stats_data,
                "cache": audio_cache,
                "settings": user_settings,
                "users_offset": users_offset
            }
            with open("data.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)

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


restore_all_data()
bot_start_time = time.time()


def register_user(chat_id):
    global active_users
    active_users[str(chat_id)] = time.time()
    save_all_data()


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
        except:
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


# --- НАСТРОЙКИ И ПОЛЗУНОК ---
def get_settings_keyboard(chat_id):
    chat_id_str = str(chat_id)
    if chat_id_str not in user_settings:
        user_settings[chat_id_str] = {"search_limit": 10}

    max_limit = max_search_limit(chat_id)
    limit = min(max_limit, int(user_settings[chat_id_str].get("search_limit", 10)))
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
    chat_id = message.chat.id
    register_user(chat_id)
    note = "до 50 треков (VIP)" if is_user_vip(chat_id) else "до 25 треков (с VIP — до 50)"
    bot.reply_to(
        message,
        f"⚙️ **Настройки поиска**\n\nС помощью ползунка ниже выбери количество треков на одной странице: {note}",
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard(chat_id)
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("setslider_"))
def handle_slider_callback(call):
    chat_id_str = str(call.message.chat.id)
    if chat_id_str not in user_settings:
        user_settings[chat_id_str] = {"search_limit": 10}

    max_limit = max_search_limit(call.from_user.id)
    current_limit = min(max_limit, int(user_settings[chat_id_str].get("search_limit", 10)))
    action = call.data.split("_")[1]

    if action == "minus":
        new_limit = max(5, current_limit - 5)
    else:
        new_limit = min(max_limit, current_limit + 5)

    if new_limit != user_settings[chat_id_str].get("search_limit"):
        user_settings[chat_id_str]["search_limit"] = new_limit
        save_all_data()
        try:
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=get_settings_keyboard(call.message.chat.id)
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
        global audio_cache
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


# --- ИНЛАЙН-РЕЖИМ ---
@bot.inline_handler(func=lambda query: True)
def inline_query(query):
    user_id = query.from_user.id
    search_text = query.query.strip()
    if not search_text:
        return

    def worker():
        try:
            ydl_opts = {"extract_flat": True, "quiet": True}
            search_query = f"scsearch10:{search_text}"
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(search_query, download=False)
                tracks = result.get("entries", [])

            results = []
            inline_tracks_cache[query.from_user.id] = tracks

            for i, track in enumerate(tracks):
                title = track.get("title", "Без названия")
                uploader = track.get("uploader", "Неизвестен")

                results.append(
                    telebot.types.InlineQueryResultArticle(
                        id=str(i),
                        title=title[:50],
                        description=f"Автор: {uploader} | Нажми для отправки",
                        input_message_content=telebot.types.InputTextMessageContent(
                            message_text=f"⏳ Загружаю трек: {title[:40]}..."
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
        result_id = int(chosen.result_id)
        user_tracks = inline_tracks_cache.get(user_id, [])

        if result_id >= len(user_tracks):
            return

        track = user_tracks[result_id]
        track_url = track['url']
        title = track.get('title', 'Трек')
        uploader = track.get('uploader', 'Музыка')
        inline_msg_id = chosen.inline_message_id

        if track_url in audio_cache and not audio_cache[track_url].startswith("http"):
            try:
                bot.edit_message_media(
                    media=InputMediaAudio(
                        media=audio_cache[track_url],
                        title=title,
                        performer=uploader
                    ),
                    inline_message_id=inline_msg_id
                )
                stats_data["total_downloads"] += 1
                save_all_data()
                return
            except Exception as e:
                print(f"Ошибка отправки из кэша (инлайн): {e}")

        audio_filename = None
        thumbnail_filename = None
        original_thumb = None

        try:
            ydl_opts = {
                "format": "ba[abr<=128]/ba[ext=m4a]/ba/best",
                "outtmpl": f"song_inline_{user_id}_%(id)s.%(ext)s",
                "writethumbnail": True,
                "quiet": True,
                "noprogress": True,
                "socket_timeout": 15,
                "postprocessors": [
                    {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
                    {"key": "EmbedThumbnail"}
                ]
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(track_url, download=True)
                audio_filename = ydl.prepare_filename(info)
                original_thumb, thumbnail_filename = get_track_thumbnail(info, audio_filename)

            audio_filename = compress_audio_if_needed(audio_filename, thumbnail_filename)

            with open(audio_filename, "rb") as audio:
                thumb_file = open(thumbnail_filename, "rb") if thumbnail_filename and os.path.exists(thumbnail_filename) else None

                backup_msg = bot.send_audio(
                    chat_id=BACKUP_CHANNEL_ID,
                    audio=audio,
                    title=info.get('title', title),
                    performer=info.get('uploader', uploader),
                    thumbnail=thumb_file
                )
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

            stats_data["total_downloads"] += 1
            save_all_data()
        except Exception as e:
            print(f"Ошибка скачивания (инлайн): {e}")
            try:
                bot.edit_message_text("❌ Не удалось скачать трек.", inline_message_id=inline_msg_id)
            except:
                pass
        finally:
            for file_p in [audio_filename, thumbnail_filename, original_thumb]:
                if file_p and os.path.exists(file_p):
                    try: os.remove(file_p)
                    except: pass

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
    real_users_count = sum(1 for uid, last_seen in active_users.items() if last_seen >= month_ago)
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
        stars_count = int(action.replace("donate_", ""))
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

    query = args[1]
    original_queries[chat_id] = query
    search_music_by_query(message, query=query, page=1, is_new=True, is_random=False)


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

    author_name = args[1]
    original_queries[chat_id] = author_name
    search_music_by_query(message, query=author_name, page=1, is_new=True, is_random=False)


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


@bot.callback_query_handler(func=lambda call: call.data.startswith("genre_"))
def handle_genre_selection(call):
    chat_id = call.message.chat.id
    if not check_user_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Сначала подпишись на группу", show_alert=True)
        send_subscription_prompt(chat_id)
        return
    register_user(chat_id)

    genre_base = call.data.replace("genre_", "")
    modifiers = ["mix", "2024", "2025", "2026", "playlist", "hits", "top", "remix", "vibes", "chill", "night", "club", "party", "bass", "popular", "edit"]
    genre_query = f"{genre_base} {random.choice(modifiers)} {random.choice(modifiers)}".strip()

    original_queries[chat_id] = genre_base
    bot.answer_callback_query(call.id, "🎲 Собираю случайные треки...")
    search_music_by_query(call.message, query=genre_query, page=1, is_new=True, is_filter=False, is_random=True)


# --- ОБРАБОТЧИК ТЕКСТОВОГО ПОИСКА С ПРОВЕРКОЙ ПОДПИСКИ ---
@bot.message_handler(func=lambda message: message.chat.type == 'private' and bool(message.text) and not message.text.startswith('/'))
def text_handler(message):
    chat_id = message.chat.id

    register_user(chat_id)

    if chat_id in waiting_for_custom_stars:
        waiting_for_custom_stars.remove(chat_id)
        text = message.text.strip()
        if text.isdigit() and 0 < int(text) <= 10000:
            stars_count = int(text)
            send_invoice_stars(chat_id, stars_count, "Поддержка проекта", "donate")
        else:
            bot.reply_to(message, "❌ Пожалуйста, введи корректное число. Попробуй снова через /donate")
        return

    if not check_user_subscription(message.from_user.id):
        send_subscription_prompt(chat_id)
        return

    original_queries[chat_id] = message.text
    search_music_by_query(message, query=message.text, page=1, is_new=True, is_random=False)


def search_music_by_query(message, query, page=1, is_new=False, is_filter=False, is_random=False):
    chat_id = message.chat.id
    if is_new:
        msg = bot.reply_to(message, "🔍 Ищу варианты...")
    else:
        msg = message

    def worker():
        try:
            # ЛИМИТЫ: VIP до 50, обычный до 25 (в приватном чате chat_id == user_id)
            max_possible = max_search_limit(chat_id)
            user_limit = int(user_settings.get(str(chat_id), {}).get("search_limit", 10))
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
                ydl_opts = {
                    "extract_flat": True,
                    "quiet": True,
                    "no_warnings": True,
                    "socket_timeout": 10,
                    "retries": 1,
                    "extractor_retries": 1,
                    "ignoreerrors": True,
                }
                search_query = f"scsearch{limit_ydl}:{query}"
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    result = ydl.extract_info(search_query, download=False)
                    all_tracks = result.get("entries", []) or []
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

            tracks_cache[chat_id] = tracks
            markup = InlineKeyboardMarkup(row_width=1)

            for i, track in enumerate(tracks):
                title = track.get("title", "Без названия")[:35]
                markup.add(InlineKeyboardButton(text=f"🎵 {i+1}. {title}", callback_data=f"dl_{i}"))

            nav_buttons = []
            if is_random:
                base_q = original_queries.get(chat_id, query)
                nav_buttons.append(InlineKeyboardButton(text="🎲 Ещё случайных", callback_data=f"randpage_{page+1}_{base_q}"))
            else:
                if page > 1:
                    nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page_{page-1}_{query}"))
                nav_buttons.append(InlineKeyboardButton(text="🔄 Ещё", callback_data=f"page_{page+1}_{query}"))

            markup.row(*nav_buttons)

            orig_q = original_queries.get(chat_id, query)
            if is_filter:
                markup.row(InlineKeyboardButton(text="🔙 Назад к обычному", callback_data=f"back_{orig_q}"))
            else:
                markup.row(
                    InlineKeyboardButton(text="⚡ Speed Up", callback_data=f"filter_{orig_q}_speedup"),
                    InlineKeyboardButton(text="🐢 Slowed", callback_data=f"filter_{orig_q}_slowed")
                )

            prefix = "🎲 Случайная подборка." if is_random else f"🎧 Страница {page}."
            text_content = f"{prefix} Запрос: {query}"
            bot.edit_message_text(text_content, chat_id, msg.message_id, reply_markup=markup)

        except Exception as e:
            print(f"Ошибка поиска: {e}")
            try:
                bot.edit_message_text("❌ Ошибка поиска.", chat_id, msg.message_id)
            except:
                pass

    threading.Thread(target=worker, daemon=True).start()


@bot.callback_query_handler(func=lambda call: call.data.startswith(("page_", "randpage_", "filter_", "back_")))
def handle_navigation(call):
    bot.answer_callback_query(call.id)
    data = call.data.split("_")

    if data[0] == "page":
        page = int(data[1])
        query = "_".join(data[2:])
        is_filt = "speedup" in query or "slowed" in query
        search_music_by_query(call.message, query=query, page=page, is_new=False, is_filter=is_filt, is_random=False)

    elif data[0] == "randpage":
        page = int(data[1])
        base_query = "_".join(data[2:])
        modifiers = ["mix", "2024", "2025", "2026", "playlist", "hits", "top", "remix", "vibes", "chill", "night", "club", "party", "bass", "popular", "edit"]
        new_rand_query = f"{base_query} {random.choice(modifiers)} {random.choice(modifiers)}".strip()
        search_music_by_query(call.message, query=new_rand_query, page=page, is_new=False, is_filter=False, is_random=True)

    elif data[0] == "filter":
        filter_type = data[-1]
        query = "_".join(data[1:-1])
        new_query = f"{query} {filter_type}"
        search_music_by_query(call.message, query=new_query, page=1, is_new=False, is_filter=True, is_random=False)

    elif data[0] == "back":
        query = "_".join(data[1:])
        search_music_by_query(call.message, query=query, page=1, is_new=False, is_filter=False, is_random=False)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_search")
def handle_back_to_search(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    query = original_queries.get(chat_id, "музыка")
    search_music_by_query(call.message, query=query, page=1, is_new=False, is_filter=False, is_random=False)


# --- VIP: ВЫБОР ЭФФЕКТА ПРИ СКАЧИВАНИИ В БОТЕ ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("dlfx_"))
def handle_download_effect_callback(call):
    chat_id = call.message.chat.id
    parts = call.data.split("_")  # dlfx_<index>_<effect>
    if len(parts) != 3 or not parts[1].isdigit():
        return bot.answer_callback_query(call.id)
    index, effect = int(parts[1]), parts[2]
    if effect != "none" and effect not in VIP_EFFECTS:
        return bot.answer_callback_query(call.id)
    if effect != "none" and not is_user_vip(call.from_user.id):
        return bot.answer_callback_query(call.id, "👑 Эффекты доступны только с VIP (/vip)", show_alert=True)

    user_tracks = tracks_cache.get(chat_id, [])
    if index >= len(user_tracks):
        return bot.answer_callback_query(call.id, "❌ Трек не найден в кэше. Повтори поиск.", show_alert=True)

    raw = user_tracks[index]
    track = normalize_miniapp_track({
        "url": raw.get("url"), "title": raw.get("title"), "uploader": raw.get("uploader"),
    })
    if not track:
        return bot.answer_callback_query(call.id, "❌ Некорректный трек.")

    bot.answer_callback_query(call.id, "⏳ Обрабатываю трек...")
    try:
        bot.edit_message_text(
            f"⏳ Готовлю трек: {track['title'][:60]}\nЭффект: {EFFECT_NAMES.get(effect, 'обычный звук')}",
            chat_id, call.message.message_id
        )
    except Exception:
        pass
    threading.Thread(target=download_track_for_miniapp, args=(chat_id, track, effect), daemon=True).start()


@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def handle_download_callback(call):
    chat_id = call.message.chat.id
    register_user(chat_id)

    index = int(call.data.replace("dl_", ""))
    user_tracks = tracks_cache.get(chat_id, [])

    if index >= len(user_tracks):
        bot.answer_callback_query(call.id, "❌ Трек не найден в кэше.")
        return

    track = user_tracks[index]
    track_url = track['url']
    title = track.get('title', 'Трек')
    uploader = track.get('uploader', 'Музыка')

    # VIP: предлагаем выбрать эффект перед скачиванием
    if is_user_vip(call.from_user.id):
        bot.answer_callback_query(call.id)
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("🔊 Обычный", callback_data=f"dlfx_{index}_none"),
            InlineKeyboardButton("🔉 Bass Boost", callback_data=f"dlfx_{index}_bassboost"),
            InlineKeyboardButton("🎧 8D", callback_data=f"dlfx_{index}_8d"),
            InlineKeyboardButton("🐢 Slowed", callback_data=f"dlfx_{index}_slowed"),
            InlineKeyboardButton("⚡ Nightcore", callback_data=f"dlfx_{index}_nightcore"),
        )
        markup.row(InlineKeyboardButton("🔙 К поиску", callback_data="back_to_search"))
        bot.send_message(chat_id, f"🎚 **Выбери звук для:**\n🎵 {title}", parse_mode="Markdown", reply_markup=markup)
        return

    bot.answer_callback_query(call.id, f"📥 Загружаю: {title[:30]}...")

    status_msg = bot.send_message(
        chat_id,
        f"⏳ **Скачиваю трек:**\n🎵 {title}\n🧑‍🎤 {uploader}\n\n*Подожди пару секунд...*",
        parse_mode="Markdown"
    )

    def worker():
        audio_filename = None
        thumbnail_filename = None
        original_thumb = None

        try:
            if track_url in audio_cache and not audio_cache[track_url].startswith("http"):
                file_id = audio_cache[track_url]
                bot.send_audio(chat_id, file_id, title=title, performer=uploader)

                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton(text="🔍 Найти другой трек", callback_data="back_to_search"))

                bot.edit_message_text(
                    text=f"▶️ **Плеер:**\n🎵 {title}\n🧑‍🎤 {uploader}",
                    chat_id=chat_id,
                    message_id=status_msg.message_id,
                    parse_mode="Markdown",
                    reply_markup=markup
                )
                stats_data["total_downloads"] += 1
                save_all_data()
                return

            ydl_opts = {
                "format": "ba[abr<=128]/ba[ext=m4a]/ba/best",
                "outtmpl": f"song_{chat_id}_%(id)s.%(ext)s",
                "writethumbnail": True,
                "quiet": True,
                "noprogress": True,
                "socket_timeout": 15,
                "postprocessors": [
                    {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
                    {"key": "EmbedThumbnail"}
                ]
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(track_url, download=True)
                audio_filename = ydl.prepare_filename(info)
                original_thumb, thumbnail_filename = get_track_thumbnail(info, audio_filename)

            audio_filename = compress_audio_if_needed(audio_filename, thumbnail_filename)

            with open(audio_filename, "rb") as audio:
                thumb_file = open(thumbnail_filename, "rb") if thumbnail_filename and os.path.exists(thumbnail_filename) else None

                sent_msg = bot.send_audio(
                    chat_id,
                    audio,
                    title=info.get('title', title),
                    performer=info.get('uploader', uploader),
                    thumbnail=thumb_file
                )
                if thumb_file:
                    thumb_file.close()
                file_id = sent_msg.audio.file_id
                audio_cache[track_url] = file_id

            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(text="🔍 Найти другой трек", callback_data="back_to_search"))

            bot.edit_message_text(
                text=f"▶️ **Плеер:**\n🎵 {info.get('title', title)}\n🧑‍🎤 {info.get('uploader', uploader)}",
                chat_id=chat_id,
                message_id=status_msg.message_id,
                parse_mode="Markdown",
                reply_markup=markup
            )

            stats_data["total_downloads"] += 1
            save_all_data()
        except Exception as e:
            print(f"Ошибка при скачивании трека: {e}")
            try:
                bot.edit_message_text("❌ Не удалось скачать выбранный трек.", chat_id, status_msg.message_id)
            except:
                pass
        finally:
            for file_p in [audio_filename, thumbnail_filename, original_thumb]:
                if file_p and os.path.exists(file_p):
                    try: os.remove(file_p)
                    except: pass

    threading.Thread(target=worker, daemon=True).start()


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
        save_all_data()

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
    query = str((query_params.get("q") or [""])[0]).strip()[:100]
    search_type = str((query_params.get("type") or ["track"])[0]).strip().lower()
    if not query:
        return api_json(handler, 400, {"error": "Введи имя автора или название трека."})
    settings = get_user_settings(user["id"])

    # VIP ЛИМИТ: до 50, обычный до 25
    max_possible = max_search_limit(user["id"])
    limit = max(5, min(max_possible, int(settings.get("search_limit", 10))))

    try:
        ydl_opts = {
            "extract_flat": True, "quiet": True, "no_warnings": True,
            "socket_timeout": 12, "retries": 1, "extractor_retries": 1,
            "ignoreerrors": True
        }
        fetch_count = min(100, max(30, limit * 3) if search_type == 'author' else max(20, limit * 2))
        search_query = f"scsearch{fetch_count}:{query}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(search_query, download=False)
        entries = (result or {}).get("entries", []) or []
        tracks = []
        for item in entries:
            if not item:
                continue
            track_url = item.get("webpage_url") or item.get("original_url") or item.get("url")
            if not track_url:
                continue
            normalized = normalize_miniapp_track({
                "url": track_url,
                "title": item.get("title"),
                "uploader": item.get("uploader") or item.get("artist"),
                "thumbnail": best_soundcloud_thumbnail(item),
                "duration": item.get("duration")
            })
            if normalized:
                tracks.append(normalized)

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
            threading.Thread(target=prewarm_tracks_stream_urls, args=(tracks[:2],), daemon=True).start()
        api_json(handler, 200, {"tracks": tracks, "query": query})
    except Exception as e:
        print(f"Ошибка поиска Mini App: {e}")
        api_json(handler, 500, {"error": "Не удалось выполнить поиск. Попробуй ещё раз."})


def get_soundcloud_stream_url(track_url):
    now = time.time()
    with stream_cache_lock:
        cached = stream_url_cache.get(track_url)
        if cached and (now - cached[1] < STREAM_CACHE_TTL):
            return cached[0]

    options = {
        "format": "bestaudio[protocol^=http]/bestaudio/best",
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "skip_download": True, "socket_timeout": 15,
        "retries": 1, "extractor_retries": 1
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(track_url, download=False)
    direct_url = info.get("url")
    if not direct_url:
        formats = info.get("requested_formats") or info.get("formats") or []
        audio_formats = [f for f in formats if f.get("url") and f.get("acodec") not in (None, "none")]
        if audio_formats:
            audio_formats.sort(key=lambda f: (f.get("vcodec") == "none", f.get("abr") or 0), reverse=True)
            direct_url = audio_formats[0]["url"]
    if not direct_url or urlparse(direct_url).scheme not in ("http", "https"):
        raise RuntimeError("SoundCloud не вернул прямую ссылку на аудио")

    with stream_cache_lock:
        stream_url_cache[track_url] = (direct_url, now)
    return direct_url


def prewarm_tracks_stream_urls(tracks, max_count=20):
    def _warm_one(item):
        url = item.get("url") if isinstance(item, dict) else None
        if url:
            try:
                get_soundcloud_stream_url(url)
            except Exception:
                pass

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
    try:
        direct_url = get_soundcloud_stream_url(track_url)
        handler.send_response(302)
        handler.send_header("Location", direct_url)
        handler.send_header("Cache-Control", "public, max-age=1800")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
    except Exception as e:
        print(f"Ошибка получения аудиопотока: {e}")
        api_json(handler, 502, {"error": "Не удалось получить аудио для этого трека."})


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
    """Скачивает трек и отправляет в Telegram. Для VIP может применить аудиоэффект."""
    if effect not in VIP_EFFECTS:
        effect = "none"
    if effect != "none" and not is_user_vip(user_id):
        effect = "none"

    track_url = track["url"]
    title = track["title"]
    uploader = track["uploader"]
    audio_filename = thumbnail_filename = original_thumb = None
    try:
        # Кэш используем только для обычного звука
        cached_file_id = audio_cache.get(track_url)
        if effect == "none" and cached_file_id and not str(cached_file_id).startswith("http"):
            bot.send_audio(user_id, cached_file_id, title=title, performer=uploader)
            stats_data["total_downloads"] += 1
            save_all_data()
            return
        ydl_opts = {
            "format": "ba[abr<=128]/ba[ext=m4a]/ba/best",
            "outtmpl": f"song_mini_{user_id}_%(id)s.%(ext)s",
            "writethumbnail": True, "quiet": True, "noprogress": True,
            "socket_timeout": 15,
            "postprocessors": [
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
                {"key": "EmbedThumbnail"}
            ]
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(track_url, download=True)
            audio_filename = ydl.prepare_filename(info)
            original_thumb, thumbnail_filename = get_track_thumbnail(info, audio_filename)

        out_title = info.get("title", title)
        if effect != "none":
            audio_filename = apply_audio_effect(audio_filename, effect)
            out_title = f"{out_title} ({EFFECT_NAMES[effect]})"

        audio_filename = compress_audio_if_needed(audio_filename, thumbnail_filename)
        with open(audio_filename, "rb") as audio:
            thumb_file = open(thumbnail_filename, "rb") if thumbnail_filename and os.path.exists(thumbnail_filename) else None
            try:
                sent = bot.send_audio(
                    user_id, audio,
                    title=out_title,
                    performer=info.get("uploader", uploader),
                    thumbnail=thumb_file
                )
            finally:
                if thumb_file:
                    thumb_file.close()
        if effect == "none":
            audio_cache[track_url] = sent.audio.file_id
        stats_data["total_downloads"] += 1
        save_all_data()
    except Exception as e:
        print(f"Ошибка загрузки трека из Mini App: {e}")
        try:
            bot.send_message(user_id, f"❌ Не удалось скачать «{title[:80]}». Попробуй выбрать другой результат.")
        except Exception as send_error:
            print(f"Не удалось отправить сообщение об ошибке Mini App: {send_error}")
    finally:
        # Чистим все временные файлы этого трека (включая версии с эффектом)
        base = None
        if audio_filename:
            base = os.path.splitext(audio_filename)[0]
        candidates = [audio_filename, thumbnail_filename, original_thumb]
        if base:
            root = base.split("_fx_")[0].split("_compressed")[0]
            for ext in (".m4a", ".mp3", ".opus", ".webm", ".ogg", ".jpg", ".png", ".webp"):
                candidates.append(root + ext)
        for file_path in set(candidates):
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass


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
        save_all_data()
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
        save_all_data()
        return api_json(handler, 200, {"ok": True, "saved": saved, "favorites": favorites})

    if path == "/api/history":
        tracks = payload.get("tracks") or []
        if not isinstance(tracks, list):
            return api_json(handler, 400, {"error": "Некорректный список треков."})
        for raw in reversed(tracks[:25]):
            track = normalize_miniapp_track(raw)
            if track:
                add_to_user_history(settings, track)
        save_all_data()
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
        save_all_data()
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
        save_all_data()
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
            save_all_data()

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
