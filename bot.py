import os
import sys
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, InputMediaAudio
from telebot.apihelper import ApiTelegramException
import yt_dlp
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, parse_qsl, quote
import hmac
import hashlib
import time
import json
import random
import subprocess
import urllib.request
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image

# --- ПОДКЛЮЧЕНИЕ FFMPEG / FFPROBE ДЛЯ ОБЛОЖЕК И ОБРАБОТКИ АУДИО ---
import static_ffmpeg
static_ffmpeg.add_paths()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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

# Данные бота и бэкапа
active_users = {}
stats_data = {"total_downloads": 0}
audio_cache = {}
user_settings = {}
vip_users = {}  # { "user_id": expire_timestamp }
backup_msg_id = None
data_lock = threading.Lock()
users_offset = 0
bot_start_time = time.time()

# --- СИСТЕМА VIP СТАТУСА ---
def is_vip(user_id):
    uid = str(user_id)
    if uid == str(ADMIN_ID):
        return True
    return vip_users.get(uid, 0) > time.time()

def get_vip_expiry_str(user_id):
    uid = str(user_id)
    exp = vip_users.get(uid, 0)
    if exp > time.time():
        rem_days = max(1, int((exp - time.time()) / 86400))
        date_str = time.strftime("%d.%m.%Y", time.localtime(exp))
        return f"до {date_str} (осталось ~{rem_days} дн.)"
    return "Не активен"

def add_vip(user_id, days):
    uid = str(user_id)
    now = time.time()
    current_exp = max(now, vip_users.get(uid, 0))
    vip_users[uid] = current_exp + (days * 86400)
    save_all_data()

# --- ПРОВЕРКА ПОДПИСКИ С УЧЕТОМ VIP ---
def check_user_subscription(user_id):
    # ПРАВИЛО: для активных VIP подписка на канал НЕ ТРЕБУЕТСЯ
    if is_vip(user_id):
        return True
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        status = getattr(member, "status", "left")
        return status in ("creator", "administrator", "member")
    except Exception as e:
        print(f"Ошибка проверки подписки: {e}")
        return False

def send_subscription_prompt(chat_id):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("📢 Подписаться на группу", url="https://t.me/+8JYfdTy2YhE0NDJi"))
    markup.row(InlineKeyboardButton("🔄 Я подписался", callback_data="check_sub"))
    markup.row(InlineKeyboardButton("👑 Купить VIP (без обязательной подписки)", callback_data="open_vip"))
    bot.send_message(
        chat_id,
        "❌ **Доступ ограничен!**\n\nЧтобы пользоваться ботом и искать музыку, необходимо подписаться на нашу группу: https://t.me/+8JYfdTy2YhE0NDJi\n\nЛибо приобрети **VIP-статус**, который навсегда убирает обязательную подписку!",
        reply_markup=markup,
        parse_mode="Markdown"
    )

# --- ПОЛУЧЕНИЕ ТЕКСТА ПЕСНИ (LRCLIB API) ---
def fetch_song_lyrics(title, artist=""):
    try:
        clean_title = re.sub(r"\(.*?\) |\[.*?\]|feat\..*|ft\..*|prod\..*|remix|slowed", "", title, flags=re.IGNORECASE).strip()
        clean_artist = re.sub(r"\(.*?\) |\[.*?\]", "", artist).strip()
        url = f"https://lrclib.net/api/get?track_name={quote(clean_title)}"
        if clean_artist:
            url += f"&artist_name={quote(clean_artist)}"
        req = urllib.request.Request(url, headers={"User-Agent": "TelegramMusicBot/2.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data.get("plainLyrics") or data.get("syncedLyrics")
    except Exception:
        pass
    return None

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
                    image = image.resize((max(1, int(width * 0.85)), max(1, int(height * 0.85))), Image.LANCZOS)
                    quality = 70

        if os.path.exists(thumbnail_filename):
            os.remove(thumbnail_filename)
    except Exception as e:
        print(f"Ошибка подготовки обложки для Telegram: {e}")
        if os.path.exists(thumbnail_filename):
            try: os.remove(thumbnail_filename)
            except Exception: pass

    return None

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
            request = urllib.request.Request(thumbnail_url, headers={"User-Agent": "Mozilla/5.0"})
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
        except Exception:
            if os.path.exists(downloaded_thumb):
                try: os.remove(downloaded_thumb)
                except Exception: pass

    return None, None

# --- АУДИОЭФФЕКТЫ / FFMPEG ЭКВАЛАЙЗЕР И СЖАТИЕ ---
AUDIO_FILTERS = {
    "bassboost": "equalizer=f=60:width_type=h:width=60:g=12,bass=g=10",
    "8d": "apulsator=hz=0.125",
    "nightcore": "asetrate=44100*1.22,aresample=44100,atempo=1.05",
    "slowed": "asetrate=44100*0.84,aresample=44100,aecho=0.8:0.88:60:0.4"
}

def compress_or_process_audio(input_filename, thumbnail_filename=None, effect=None, max_size_mb=48):
    if not input_filename or not os.path.exists(input_filename):
        return input_filename

    file_size_mb = os.path.getsize(input_filename) / (1024 * 1024)
    need_compress = file_size_mb > max_size_mb
    has_effect = effect in AUDIO_FILTERS

    if not need_compress and not has_effect:
        return input_filename

    output_filename = os.path.splitext(input_filename)[0] + f"_{effect or 'comp'}.mp3"
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

    if has_effect:
        cmd += ["-af", AUDIO_FILTERS[effect]]

    cmd += [
        "-c:a", "libmp3lame",
        "-b:a", "128k" if need_compress else "256k",
        "-id3v2_version", "3",
        output_filename
    ]

    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        if os.path.exists(output_filename) and os.path.getsize(output_filename) > 0:
            if output_filename != input_filename and os.path.exists(input_filename):
                try: os.remove(input_filename)
                except Exception: pass
            return output_filename
    except Exception as e:
        print(f"Ошибка FFmpeg при обработке аудио: {e}")

    return input_filename

# --- СИСТЕМА ЕДИНОГО БЭКАПА ЧЕРЕЗ ЗАКРЕП ---
def restore_all_data():
    global active_users, stats_data, audio_cache, user_settings, vip_users, backup_msg_id, users_offset
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
                active_users = {str(uid): now for uid in raw_users}
            elif isinstance(raw_users, dict):
                active_users = {str(uid): t for uid, t in raw_users.items() if t >= month_ago}
            else:
                active_users = {}
                
            stats_data = data.get("stats", {"total_downloads": 0})
            audio_cache = data.get("cache", {})
            user_settings = data.get("settings", {})
            vip_users = data.get("vip", {})
            users_offset = data.get("users_offset", 0)
            
            active_users[str(ADMIN_ID)] = now
            print("✅ Все данные и VIP-подписки успешно восстановлены!")
    except Exception as e:
        print(f"⚠️ Ошибка автовосстановления: {e}")
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
                "vip": vip_users,
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
                            media=telebot.types.InputMediaDocument(f, caption="💾 Бэкап базы данных (VIP включён)")
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

def register_user(chat_id):
    active_users[str(chat_id)] = time.time()
    save_all_data()

# --- МЕНЮ VIP И ПОДПИСКИ ЗА STARS ---
def get_vip_menu(user_id):
    status = get_vip_expiry_str(user_id)
    is_act = is_vip(user_id)
    icon = "✅" if is_act else "❌"

    text = (
        "👑 **ПРЕМИУМ VIP ПОДПИСКА**\n\n"
        f"Твой статус: {icon} **{status}**\n\n"
        "✨ **Что даёт VIP:**\n"
        "1. 🚫 **Без обязательной подписки:** поиск и скачивание без проверки каналов!\n"
        "2. 🚀 **Увеличенная выдача:** до **50 треков** на странице (вместо 10-25).\n"
        "3. 📜 **Тексты всех песен:** кнопка просмотра слов в 1 клик прямо в боте.\n"
        "4. 🎛 **Премиум-звук:** Bass Boost, 8D Audio, Slowed & Reverb, Nightcore.\n"
        "5. 🎨 **VIP-темы Mini App:** Cyberpunk, Gold, Glassmorphism, OLED Black.\n\n"
        "👇 **Выбери тариф подписки в Telegram Stars:**"
    )
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("⭐ 1 Неделя — 50 Stars", callback_data="buy_vip_7"))
    markup.row(InlineKeyboardButton("⭐ 1 Месяц (30 дней) — 150 Stars", callback_data="buy_vip_30"))
    return text, markup

@bot.message_handler(commands=['vip', 'premium'])
def vip_command(message):
    chat_id = message.chat.id
    register_user(chat_id)
    text, markup = get_vip_menu(message.from_user.id)
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "open_vip")
def callback_open_vip(call):
    text, markup = get_vip_menu(call.from_user.id)
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_vip_"))
def callback_buy_vip(call):
    days = int(call.data.replace("buy_vip_", ""))
    amount = 50 if days == 7 else 150
    lbl = f"VIP подписка на {days} дней"
    prices = [LabeledPrice(label=lbl, amount=amount)]
    bot.send_invoice(
        chat_id=call.message.chat.id,
        title=f"👑 {lbl}",
        description="Отключение подписок на каналы, лимит 50 треков, эквалайзер, тексты песен и темы Mini App!",
        invoice_payload=f"vip_{days}",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="vip"
    )
    bot.answer_callback_query(call.id)

@bot.pre_checkout_query_handler(func=lambda query: True)
def process_pre_checkout(query):
    bot.answer_pre_checkout_query(query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def handle_payment_success(message):
    payload = message.successful_payment.invoice_payload
    user_id = message.from_user.id
    if payload.startswith("vip_"):
        days = int(payload.split("_")[1])
        add_vip(user_id, days)
        date_str = get_vip_expiry_str(user_id)
        bot.reply_to(
            message,
            f"🎉 **ПОЗДРАВЛЯЕМ! VIP УСПЕШНО АКТИВИРОВАН!**\n\n"
            f"📅 Статус: **{date_str}**\n\n"
            f"Теперь тебе доступны:\n"
            f"• Поиск без подписки на канал!\n"
            f"• Лимит до 50 треков в /settings\n"
            f"• Тексты песен и аудиоэффекты (Bass Boost, 8D)!\n"
            f"• Эксклюзивные темы в Mini App!",
            parse_mode="Markdown"
        )
    elif payload.startswith("donate_"):
        bot.reply_to(message, "🎉 Ура! Огромное спасибо за донат! Твоя поддержка бесценна ❤️")

# --- КОМАНДЫ БОТА ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    register_user(chat_id)

    text = (
        "👋 Привет! Напиши название песни, и я попробую её найти. 🎵\n\n"
        "💬 Наша группа: https://t.me/+8JYfdTy2YhE0NDJi\n"
        "👑 Премиум VIP (без обязательной подписки): /vip\n"
        "💖 Поддержка разработчика: /donate"
    )
    markup = InlineKeyboardMarkup()
    if message.chat.type == "private" and WEB_APP_URL:
        markup.add(InlineKeyboardButton(
            text="🎧 Открыть музыкальное приложение",
            web_app=telebot.types.WebAppInfo(url=WEB_APP_URL)
        ))
    markup.add(InlineKeyboardButton(text="👑 VIP Премиум", callback_data="open_vip"))
    bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)

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
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(
        text="🎧 Открыть музыкальное приложение",
        web_app=telebot.types.WebAppInfo(url=WEB_APP_URL)
    ))
    bot.send_message(chat_id, "👇 Нажми на кнопку ниже, чтобы открыть приложение:", reply_markup=markup)

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

# --- АДМИНСКАЯ ПОПРАВКА СТАТИСТИКИ ---
@bot.message_handler(commands=['setusers'])
def set_users_command(message):
    global users_offset
    if message.chat.id == ADMIN_ID:
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

# --- НАСТРОЙКИ С ПОДДЕРЖКОЙ VIP (ДО 50 ТРЕКОВ) ---
def get_settings_keyboard(chat_id, user_id):
    chat_id_str = str(chat_id)
    if chat_id_str not in user_settings:
        user_settings[chat_id_str] = {"search_limit": 10}
        
    limit = user_settings[chat_id_str].get("search_limit", 10)
    has_vip = is_vip(user_id)
    markup = InlineKeyboardMarkup()
    
    filled_blocks = min(5, limit // 5)
    slider_text = "🟦" * filled_blocks + "⬜️" * (5 - filled_blocks)
    
    markup.row(
        InlineKeyboardButton("➖", callback_data="setslider_minus"),
        InlineKeyboardButton(f"{limit} треков {'👑' if limit > 25 else ''}", callback_data="ignore"),
        InlineKeyboardButton("➕", callback_data="setslider_plus")
    )
    markup.row(InlineKeyboardButton(f"Шкала: {slider_text}", callback_data="ignore"))
    if not has_vip:
        markup.row(InlineKeyboardButton("🔒 Лимит 30-50 треков доступен в VIP (/vip)", callback_data="open_vip"))
    return markup

@bot.message_handler(commands=['settings'])
def settings_command(message):
    chat_id = message.chat.id
    register_user(chat_id)
    has_vip = is_vip(message.from_user.id)
    max_info = "до 50 треков (VIP)" if has_vip else "до 25 треков (до 50 с VIP)"
    bot.reply_to(
        message, 
        f"⚙️ **Настройки поиска**\n\nС помощью ползунка ниже выбери количество треков ({max_info}):",
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard(chat_id, message.from_user.id)
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("setslider_"))
def handle_slider_callback(call):
    chat_id_str = str(call.message.chat.id)
    user_id = call.from_user.id
    if chat_id_str not in user_settings:
        user_settings[chat_id_str] = {"search_limit": 10}
        
    current_limit = user_settings[chat_id_str].get("search_limit", 10)
    action = call.data.split("_")[1]
    has_vip = is_vip(user_id)
    max_limit = 50 if has_vip else 25
    
    if action == "minus":
        current_limit = max(5, current_limit - 5)
    elif action == "plus":
        if current_limit >= 25 and not has_vip:
            bot.answer_callback_query(call.id, "👑 Лимиты 30, 40 и 50 треков доступны только в VIP!", show_alert=True)
            return
        current_limit = min(max_limit, current_limit + 5)
        
    if current_limit != user_settings[chat_id_str]["search_limit"]:
        user_settings[chat_id_str]["search_limit"] = current_limit
        save_all_data()
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id, 
            message_id=call.message.message_id, 
            reply_markup=get_settings_keyboard(call.message.chat.id, user_id)
        )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "ignore")
def handle_ignore(call):
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=['clearcache'])
def clear_bot_cache(message):
    if message.chat.id == ADMIN_ID:
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

            audio_filename = compress_or_process_audio(audio_filename, thumbnail_filename)

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
            except Exception:
                pass
        finally:
            for file_p in [audio_filename, thumbnail_filename, original_thumb]:
                if file_p and os.path.exists(file_p):
                    try: os.remove(file_p)
                    except Exception: pass

    threading.Thread(target=worker, daemon=True).start()

# --- СТАТИСТИКА БОТА ---
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
    active_vips = sum(1 for exp in vip_users.values() if exp > now)
    
    uptime_seconds = int(time.time() - bot_start_time)
    hours = uptime_seconds // 3600
    minutes = (uptime_seconds % 3600) // 60
    
    user_status = "👑 Активен" if is_vip(message.from_user.id) else "Обычный пользователь"
    
    stats_text = (
        f"📊 **Статистика бота:**\n\n"
        f"👥 Активных пользователей: `{recent_users_count}`\n"
        f"👑 VIP-подписчиков: `{active_vips}`\n"
        f"⏱ Время работы: `{hours}ч {minutes}м`\n"
        f"📥 Скачано треков: `{stats_data['total_downloads']}`\n"
        f"⚡️ Пинг сервера: `{ping_ms} мс`\n\n"
        f"Твой статус: **{user_status}**\n"
        f"🟢 Статус: `Онлайн (Render)`"
    )
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("👑 Управление VIP подпиской", callback_data="open_vip"))
    bot.reply_to(message, stats_text, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(commands=['donate'])
def donate_command(message):
    chat_id = message.chat.id
    register_user(chat_id)
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton(text="⭐ 5 звёзд", callback_data="donate_5"),
        InlineKeyboardButton(text="⭐ 10 звёзд", callback_data="donate_10"),
        InlineKeyboardButton(text="⭐ 15 звёзд", callback_data="donate_15"),
        InlineKeyboardButton(text="⭐ 25 звёзд", callback_data="donate_25"),
        InlineKeyboardButton(text="👑 VIP на 1 нед (50 ⭐)", callback_data="buy_vip_7"),
        InlineKeyboardButton(text="👑 VIP на 1 мес (150 ⭐)", callback_data="buy_vip_30"),
        InlineKeyboardButton(text="✍️ Своё количество", callback_data="donate_custom")
    )
    bot.send_message(
        chat_id, 
        "💖 Спасибо за желание поддержать проект!\nВыбери сумму в звёздах или оформи VIP подписку:", 
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("donate_"))
def handle_donate_callback(call):
    chat_id = call.message.chat.id
    action = call.data.replace("donate_", "")

    if action == "custom":
        waiting_for_custom_stars.add(chat_id)
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, "✍️ Напиши число — сколько звёзд отправить (например: `50`):", parse_mode="Markdown")
        return

    try:
        stars_count = int(action)
        send_invoice_stars(chat_id, stars_count)
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"Ошибка инвойса: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка.")

def send_invoice_stars(chat_id, amount):
    prices = [LabeledPrice(label=f'Поддержка на {amount} ⭐', amount=amount)]
    bot.send_invoice(
        chat_id=chat_id,
        title='Поддержка проекта',
        description=f'Спасибо за донат в {amount} ⭐!',
        invoice_payload=f'donate_{amount}',
        provider_token='',
        currency='XTR',
        prices=prices,
        start_parameter='donate'
    )

# --- КОМАНДЫ ДЛЯ ГРУПП И ПОИСК ---
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

# --- ОБРАБОТЧИК ТЕКСТОВОГО ПОИСКА ---
@bot.message_handler(func=lambda message: message.chat.type == 'private' and not message.text.startswith('/'))
def text_handler(message):
    chat_id = message.chat.id
    if not check_user_subscription(message.from_user.id):
        send_subscription_prompt(chat_id)
        return

    register_user(chat_id)
    
    if chat_id in waiting_for_custom_stars:
        waiting_for_custom_stars.remove(chat_id)
        text = message.text.strip()
        if text.isdigit() and int(text) > 0:
            stars_count = int(text)
            send_invoice_stars(chat_id, stars_count)
        else:
            bot.reply_to(message, "❌ Пожалуйста, введи корректное число. Попробуй снова через /donate")
        return

    original_queries[chat_id] = message.text
    search_music_by_query(message, query=message.text, page=1, is_new=True, is_random=False)

def search_music_by_query(message, query, page=1, is_new=False, is_filter=False, is_random=False):
    chat_id = message.chat.id
    from_user_id = message.from_user.id if message.from_user else chat_id
    if is_new:
        msg = bot.reply_to(message, "🔍 Ищу варианты...")
    else:
        msg = message

    def worker():
        try:
            has_vip = is_vip(from_user_id)
            user_limit = user_settings.get(str(chat_id), {}).get("search_limit", 10)
            if not has_vip and user_limit > 25:
                user_limit = 25
            
            limit_ydl = 50 if is_random else max(20, user_limit * page)
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
                if len(all_tracks) >= user_limit:
                    tracks = random.sample(all_tracks, user_limit)
                else:
                    tracks = all_tracks.copy()
                    random.shuffle(tracks)
            else:
                start = (page - 1) * user_limit
                tracks = all_tracks[start:start + user_limit]
                
            if not tracks:
                bot.edit_message_text("❌ Больше ничего не найдено.", chat_id, msg.message_id, reply_markup=None)
                return
                
            tracks_cache[chat_id] = tracks
            markup = InlineKeyboardMarkup()
            
            for i, track in enumerate(tracks):
                title = track.get("title", "Без названия")[:30]
                markup.row(
                    InlineKeyboardButton(text=f"🎵 {i+1}. {title}", callback_data=f"dl_{i}_none"),
                    InlineKeyboardButton(text="📝", callback_data=f"lyr_{i}"),
                    InlineKeyboardButton(text="🎛", callback_data=f"fxmenu_{i}")
                )
            
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
            vip_label = " 👑 VIP" if has_vip else ""
            text_content = f"{prefix}{vip_label} Запрос: {query}"
            bot.edit_message_text(text_content, chat_id, msg.message_id, reply_markup=markup)
                
        except Exception as e:
            print(f"Ошибка поиска: {e}")
            try:
                bot.edit_message_text("❌ Ошибка поиска.", chat_id, msg.message_id)
            except Exception:
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

# --- ТЕКСТ ПЕСНИ (LYRICS) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("lyr_"))
def handle_lyrics_btn(call):
    chat_id = call.message.chat.id
    idx = int(call.data.replace("lyr_", ""))
    tracks = tracks_cache.get(chat_id, [])
    if idx >= len(tracks):
        bot.answer_callback_query(call.id, "Трек не найден.")
        return
    tr = tracks[idx]
    title = tr.get("title", "Без названия")
    uploader = tr.get("uploader", "")
    bot.answer_callback_query(call.id, "🔎 Ищу текст песни...")

    lyrics = fetch_song_lyrics(title, uploader)
    if lyrics:
        if len(lyrics) > 3800:
            lyrics = lyrics[:3800] + "\n\n...(сокращено)..."
        bot.send_message(chat_id, f"📝 **Текст песни {title}:**\n\n{lyrics}")
    else:
        bot.send_message(chat_id, f"❌ Текст для песни «{title}» не найден в базе.")

# --- МЕНЮ ЭКВАЛАЙЗЕРА / АУДИОЭФФЕКТОВ ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("fxmenu_"))
def handle_fx_menu(call):
    chat_id = call.message.chat.id
    idx = int(call.data.replace("fxmenu_", ""))
    has_vip = is_vip(call.from_user.id)
    vip_badge = "" if has_vip else " (VIP)"

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🔊 Обычный", callback_data=f"dl_{idx}_none"),
        InlineKeyboardButton(f"🔊 Bass Boost{vip_badge}", callback_data=f"dl_{idx}_bassboost"),
        InlineKeyboardButton(f"🎧 8D Audio{vip_badge}", callback_data=f"dl_{idx}_8d"),
        InlineKeyboardButton(f"🐢 Slowed{vip_badge}", callback_data=f"dl_{idx}_slowed"),
        InlineKeyboardButton(f"⚡ Nightcore{vip_badge}", callback_data=f"dl_{idx}_nightcore")
    )
    bot.send_message(chat_id, "🎛 **Выбери аудио-эффект эквалайзера:**", reply_markup=markup, parse_mode="Markdown")
    bot.answer_callback_query(call.id)

# --- СКАЧИВАНИЕ ТРЕКА С ЭФФЕКТАМИ ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def handle_download_callback(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    register_user(chat_id)
    
    parts = call.data.split("_")
    index = int(parts[1])
    effect = parts[2] if len(parts) > 2 else "none"

    if effect != "none" and not is_vip(user_id):
        bot.answer_callback_query(call.id, "👑 Эквалайзер и аудиоэффекты доступны только в VIP!", show_alert=True)
        return

    user_tracks = tracks_cache.get(chat_id, [])
    if index >= len(user_tracks):
        bot.answer_callback_query(call.id, "❌ Трек не найден в кэше.")
        return
        
    track = user_tracks[index]
    track_url = track['url']
    title = track.get('title', 'Трек')
    uploader = track.get('uploader', 'Музыка')
    
    bot.answer_callback_query(call.id, f"📥 Загружаю: {title[:30]}...")
    fx_note = f" [{effect.upper()}]" if effect != "none" else ""
    
    status_msg = bot.send_message(
        chat_id, 
        f"⏳ **Скачиваю трек{fx_note}:**\n🎵 {title}\n🧑‍🎤 {uploader}\n\n*Подожди пару секунд...*",
        parse_mode="Markdown"
    )

    def worker():
        audio_filename = None
        thumbnail_filename = None
        original_thumb = None
        cache_key = f"{track_url}_{effect}"

        try:
            if cache_key in audio_cache and not audio_cache[cache_key].startswith("http"):
                file_id = audio_cache[cache_key]
                bot.send_audio(chat_id, file_id, title=title + fx_note, performer=uploader)
                
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton(text="🔍 Найти другой трек", callback_data="back_to_search"))
                
                bot.edit_message_text(
                    text=f"▶️ **Плеер:**\n🎵 {title}{fx_note}\n🧑‍🎤 {uploader}",
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

            audio_filename = compress_or_process_audio(audio_filename, thumbnail_filename, effect=effect if effect != "none" else None)

            with open(audio_filename, "rb") as audio:
                thumb_file = open(thumbnail_filename, "rb") if thumbnail_filename and os.path.exists(thumbnail_filename) else None
                
                sent_msg = bot.send_audio(
                    chat_id, 
                    audio,
                    title=f"{info.get('title', title)}{fx_note}",
                    performer=info.get('uploader', uploader),
                    thumbnail=thumb_file
                )
                if thumb_file:
                    thumb_file.close()
                file_id = sent_msg.audio.file_id
                audio_cache[cache_key] = file_id

            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(text="🔍 Найти другой трек", callback_data="back_to_search"))

            bot.edit_message_text(
                text=f"▶️ **Плеер:**\n🎵 {info.get('title', title)}{fx_note}\n🧑‍🎤 {info.get('uploader', uploader)}",
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
            except Exception:
                pass
        finally:
            for file_p in [audio_filename, thumbnail_filename, original_thumb]:
                if file_p and os.path.exists(file_p):
                    try: os.remove(file_p)
                    except Exception: pass

    threading.Thread(target=worker, daemon=True).start()


# --- API MINI APP: AUTH, SEARCH, FAVORITES, HISTORY, PLAYLISTS, STREAM, LYRICS ---

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
    settings = get_user_settings(user["id"])
    changed = False
    changed |= fill_missing_track_covers(settings.get("favorites", []), max_items=10)
    changed |= fill_missing_track_covers(settings.get("history", []), max_items=10)
    if changed:
        save_all_data()
    api_json(handler, 200, {
        "user": {"id": user["id"], "first_name": user.get("first_name", "Музыка")},
        "is_vip": is_vip(user["id"]),
        "vip_expires": vip_users.get(str(user["id"]), 0),
        "theme": settings.get("theme", "default"),
        "search_limit": int(settings.get("search_limit", 10)),
        "favorites": settings.get("favorites", []),
        "history": settings.get("history", []),
        "playlists": settings.get("playlists", [])
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
            "error": "Сначала вступи в нашу группу или приобрети VIP.",
            "subscription_required": True,
            "invite_url": "https://t.me/+8JYfdTy2YhE0NDJi"
        })
    query = str((query_params.get("q") or [""])[0]).strip()[:100]
    search_type = str((query_params.get("type") or ["track"])[0]).strip().lower()
    if not query:
        return api_json(handler, 400, {"error": "Введи имя автора или название трека."})
    
    settings = get_user_settings(user["id"])
    has_vip = is_vip(user["id"])
    limit = int(settings.get("search_limit", 10))
    if not has_vip and limit > 25:
        limit = 25
    limit = max(5, min(50 if has_vip else 25, limit))

    try:
        ydl_opts = {
            "extract_flat": True, "quiet": True, "no_warnings": True,
            "socket_timeout": 12, "retries": 1, "extractor_retries": 1,
            "ignoreerrors": True
        }
        search_query = f"scsearch{max(30, limit * 3) if search_type == 'author' else max(20, limit * 2)}:{query}"
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


def handle_miniapp_lyrics(handler, query_params):
    title = (query_params.get("title") or [""])[0]
    artist = (query_params.get("artist") or [""])[0]
    lyrics = fetch_song_lyrics(title, artist)
    if lyrics:
        api_json(handler, 200, {"lyrics": lyrics, "title": title, "artist": artist})
    else:
        api_json(handler, 404, {"error": "Текст песни не найден."})


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
        return api_json(handler, 403, {"error": "Сначала вступи в нашу группу или приобрети VIP."})
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


def download_track_for_miniapp(user_id, track, effect=None):
    track_url = track["url"]
    title = track["title"]
    uploader = track["uploader"]
    audio_filename = thumbnail_filename = original_thumb = None
    fx_note = f" [{effect.upper()}]" if effect and effect != "none" else ""
    cache_key = f"{track_url}_{effect or 'none'}"

    try:
        cached_file_id = audio_cache.get(cache_key)
        if cached_file_id and not str(cached_file_id).startswith("http"):
            bot.send_audio(user_id, cached_file_id, title=title + fx_note, performer=uploader)
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
        audio_filename = compress_or_process_audio(audio_filename, thumbnail_filename, effect=effect if effect != "none" else None)
        with open(audio_filename, "rb") as audio:
            thumb_file = open(thumbnail_filename, "rb") if thumbnail_filename and os.path.exists(thumbnail_filename) else None
            try:
                sent = bot.send_audio(
                    user_id, audio,
                    title=f"{info.get('title', title)}{fx_note}",
                    performer=info.get("uploader", uploader),
                    thumbnail=thumb_file
                )
            finally:
                if thumb_file:
                    thumb_file.close()
        audio_cache[cache_key] = sent.audio.file_id
        stats_data["total_downloads"] += 1
        save_all_data()
    except Exception as e:
        print(f"Ошибка загрузки трека из Mini App: {e}")
        try:
            bot.send_message(user_id, f"❌ Не удалось скачать «{title[:80]}». Попробуй выбрать другой результат.")
        except Exception:
            pass
    finally:
        for file_path in (audio_filename, thumbnail_filename, original_thumb):
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
    has_vip = is_vip(user_id)
    settings = get_user_settings(user_id)

    if path == "/api/settings":
        try:
            count = int(payload.get("count", 10))
            theme = str(payload.get("theme", "default"))
        except (TypeError, ValueError):
            return api_json(handler, 400, {"error": "Некорректные параметры."})
        
        max_allowed = 50 if has_vip else 25
        if count > max_allowed:
            return api_json(handler, 403, {"error": "Лимит более 25 треков доступен только с VIP!"})
        if theme in ("cyberpunk", "gold", "glass", "oled") and not has_vip:
            return api_json(handler, 403, {"error": "VIP-темы доступны только подписчикам!"})
        
        settings["search_limit"] = count
        settings["theme"] = theme
        save_all_data()
        return api_json(handler, 200, {"ok": True, "search_limit": count, "theme": theme})

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
                "error": "Сначала вступи в нашу группу или приобрети VIP.",
                "subscription_required": True,
                "invite_url": "https://t.me/+8JYfdTy2YhE0NDJi"
            })
        track = normalize_miniapp_track(payload.get("track"))
        if not track:
            return api_json(handler, 400, {"error": "Некорректный трек."})
        effect = payload.get("effect", "none")
        if effect != "none" and not has_vip:
            return api_json(handler, 403, {"error": "Эквалайзер и аудиоэффекты доступны только в VIP!"})
        
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
            user_settings[user_id_str]["search_limit"] = count
            save_all_data()
            bot.send_message(message.chat.id, f"✅ Настройки успешно сохранены!\nВыдача: {count} треков на страницу.")
    except Exception as e:
        print(f"Ошибка при получении данных из Web App: {e}")

# --- ОСНОВНОЙ БЛОК ЗАПУСКА С ЗАЩИТОЙ ОТ 409 ОШИБКИ ---
if __name__ == '__main__':
    print("🚀 Запуск бота с поддержкой VIP...")
    try:
        bot.set_my_commands([
            telebot.types.BotCommand("app", "🎧 Открыть плеер"),
            telebot.types.BotCommand("vip", "👑 Премиум VIP статус"),
            telebot.types.BotCommand("settings", "⚙️ Настройки и лимит треков"),
            telebot.types.BotCommand("stats", "📊 Статистика бота"),
            telebot.types.BotCommand("donate", "⭐ Поддержка проекта")
        ])
        print("✅ Команды меню успешно обновлены.")
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
