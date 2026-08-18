import os
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
import yt_dlp
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import time
import json

# --- ВЕБ-СЕРВЕР (Для Render) ---
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(('0.0.0.0', port), DummyHandler).serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()

# --- БОТ И БЭКАП ГРУППА ---
API_TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
BACKUP_CHANNEL_ID = -1004445455425

bot = telebot.TeleBot(API_TOKEN)
tracks_cache = {}
original_queries = {}
waiting_for_custom_stars = set()

# Файлы для сохранения данных
USERS_FILE = "users.json"
STATS_FILE = "stats.json"
AUDIO_CACHE_FILE = "audio_cache.json"  # Файл для хранения file_id отправленных треков

# --- ФУНКЦИИ БЭКАПА И ВОССТАНОВЛЕНИЯ ---
def restore_from_channel(filename):
    try:
        messages = bot.get_chat_history(BACKUP_CHANNEL_ID, limit=10)
        for msg in messages:
            if msg.document and msg.document.file_name == filename:
                file_info = bot.get_file(msg.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                with open(filename, 'wb') as f:
                    f.write(downloaded_file)
                print(f"Успешно восстановлен файл {filename} из бэкапа.")
                return True
    except Exception as e:
        print(f"Ошибка восстановления {filename}: {e}")
    return False

def backup_to_channel(filename):
    try:
        if os.path.exists(filename):
            with open(filename, 'rb') as f:
                bot.send_document(BACKUP_CHANNEL_ID, f)
    except Exception as e:
        print(f"Ошибка отправки бэкапа {filename}: {e}")

def load_json(filename, default_value):
    if not os.path.exists(filename):
        restore_from_channel(filename)

    if os.path.exists(filename):
        try:
            with open(filename, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Ошибка чтения {filename}: {e}")
    return default_value

def save_json(filename, data):
    try:
        with open(filename, "w") as f:
            json.dump(data, f)
        backup_to_channel(filename)
    except Exception as e:
        print(f"Ошибка сохранения {filename}: {e}")

# Загружаем постоянные данные
active_users = set(load_json(USERS_FILE, []))
stats_data = load_json(STATS_FILE, {"total_downloads": 0})
audio_cache = load_json(AUDIO_CACHE_FILE, {})  # Загрузка базы кэша

# Время запуска (сбрасывается при перезагрузке)
bot_start_time = time.time()

def register_user(chat_id):
    if chat_id not in active_users:
        active_users.add(chat_id)
        save_json(USERS_FILE, list(active_users))

@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    register_user(chat_id)
    bot.reply_to(message, "👋 Привет! Пиши название трека, я найду его. Пользуйся фильтрами и страницами! 🎵\n\n💰 Поддержать разработчика: /donate\n📊 Статистика бота: /stats")

@bot.my_chat_member_handler()
def handle_chat_member(message):
    chat_id = message.chat.id
    new_status = message.new_chat_member.status
    if new_status in ['kicked', 'left']:
        if chat_id in active_users:
            active_users.remove(chat_id)
            save_json(USERS_FILE, list(active_users))
    elif new_status == 'member':
        if chat_id not in active_users:
            active_users.add(chat_id)
            save_json(USERS_FILE, list(active_users))

# --- ОБНОВЛЁННАЯ КОМАНДА СТАТИСТИКИ ---
@bot.message_handler(commands=['stats'])
def stats_command(message):
    chat_id = message.chat.id
    register_user(chat_id)
    
    # 1. Измерение пинга до Telegram API
    start_ping = time.time()
    bot.get_me()
    ping_ms = int((time.time() - start_ping) * 1000)
    
    # 2. Определение порядкового номера пользователя
    users_list = list(active_users)
    user_number = users_list.index(chat_id) + 1 if chat_id in users_list else len(users_list)
    
    # 3. Расчёт времени работы
    uptime_seconds = int(time.time() - bot_start_time)
    hours = uptime_seconds // 3600
    minutes = (uptime_seconds % 3600) // 60
    
    stats_text = (
        f"📊 **Статистика бота:**\n\n"
        f"👥 Активных пользователей: `{len(active_users)}`\n"
        f"⏱ Время работы: `{hours}ч {minutes}м`\n"
        f"📥 Скачано треков: `{stats_data['total_downloads']}`\n"
        f"⚡️ Пинг сервера: `{ping_ms} мс`\n"
        f"👤 Твой номер в системе: `#{user_number}`\n\n"
        f"🟢 Статус: `Онлайн (Render)`"
    )
    bot.reply_to(message, stats_text, parse_mode="Markdown")

# --- КОМАНДА ДОНАТА ---
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
        InlineKeyboardButton(text="✍️ Своё количество", callback_data="donate_custom")
    )
    bot.send_message(
        chat_id, 
        "💖 Спасибо за желание поддержать проект!\nВыбери сумму в звёздах или введи своё количество:", 
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("donate_"))
def handle_donate_callback(call):
    chat_id = call.message.chat.id
    action = call.data.replace("donate_", "")

    if action == "custom":
        waiting_for_custom_stars.add(chat_id)
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, "✍️ Напиши в чат число — сколько звёзд ты хочешь отправить (например: `50`):")
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
        description=f'Спасибо за донат в размере {amount} ⭐! Эти средства пойдут на развитие бота.',
        invoice_payload=f'donate_{amount}',
        provider_token='',
        currency='XTR',
        prices=prices,
        start_parameter='donate'
    )

@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    bot.reply_to(message, "🎉 Ура! Огромное спасибо за донат! Твоя поддержка бесценна ❤️")

# Работает только в ЛИЧНЫХ сообщениях (в группе бэкапов молчит)
@bot.message_handler(func=lambda message: message.chat.type == 'private' and not message.text.startswith('/'))
def text_handler(message):
    chat_id = message.chat.id
    register_user(chat_id)
    
    if chat_id in waiting_for_custom_stars:
        waiting_for_custom_stars.remove(chat_id)
        text = message.text.strip()
        if text.isdigit() and int(text) > 0:
            stars_count = int(text)
            send_invoice_stars(chat_id, stars_count)
        else:
            bot.reply_to(message, "❌ Пожалуйста, введи корректное число (например, 30). Попробуй снова через /donate")
        return

    original_queries[chat_id] = message.text
    search_music_by_query(message, query=message.text, page=1, is_new=True)

def search_music_by_query(message, query, page=1, is_new=False, is_filter=False):
    chat_id = message.chat.id
    
    if is_new:
        msg = bot.reply_to(message, "🔍 Ищу варианты...")
    else:
        msg = message

    try:
        ydl_opts = {"extract_flat": True, "quiet": True}
        search_query = f"scsearch20:{query}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(search_query, download=False)
            all_tracks = result.get("entries", [])
            
        start = (page - 1) * 10
        tracks = all_tracks[start:start + 10]
            
        if not tracks:
            if is_new:
                bot.edit_message_text("❌ Больше ничего не найдено.", chat_id, msg.message_id)
            else:
                bot.edit_message_text("❌ Больше ничего не найдено.", chat_id, msg.message_id, reply_markup=None)
            return
            
        tracks_cache[chat_id] = tracks
        markup = InlineKeyboardMarkup(row_width=1)
        
        for i, track in enumerate(tracks):
            title = track.get("title", "Без названия")[:35]
            markup.add(InlineKeyboardButton(text=f"🎵 {i+1}. {title}", callback_data=f"dl_{i}"))
        
        nav_buttons = []
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

        text_content = f"🎧 Страница {page}. Запрос: {query}"
        bot.edit_message_text(text_content, chat_id, msg.message_id, reply_markup=markup)
            
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        try:
            bot.edit_message_text("❌ Ошибка поиска.", chat_id, msg.message_id)
        except:
            pass

@bot.callback_query_handler(func=lambda call: call.data.startswith(("page_", "filter_", "back_")))
def handle_navigation(call):
    data = call.data.split("_")
    
    if data[0] == "page":
        page = int(data[1])
        query = "_".join(data[2:])
        is_filt = "speedup" in query or "slowed" in query
        search_music_by_query(call.message, query=query, page=page, is_new=False, is_filter=is_filt)
        
    elif data[0] == "filter":
        filter_type = data[-1]
        query = "_".join(data[1:-1])
        new_query = f"{query} {filter_type}"
        search_music_by_query(call.message, query=new_query, page=1, is_new=False, is_filter=True)
        
    elif data[0] == "back":
        query = "_".join(data[1:])
        search_music_by_query(call.message, query=query, page=1, is_new=False, is_filter=False)

# --- ИЗМЕНЕННАЯ ФУНКЦИЯ СКАЧИВАНИЯ С КЭШЕМ ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def callback_download_track(call):
    index = int(call.data.replace("dl_", ""))
    chat_id = call.message.chat.id
    user_tracks = tracks_cache.get(chat_id, [])
    
    if index >= len(user_tracks):
        bot.answer_callback_query(call.id, "❌ Список устарел.")
        return

    track = user_tracks[index]
    track_url = track['url']

    # 1. ПРОВЕРКА КЭША (отправка за 1 секунду)
    if track_url in audio_cache:
        bot.answer_callback_query(call.id, "⚡ Моментальная отправка...")
        msg = bot.send_message(chat_id, "🚀 Отправляю из кэша...")
        try:
            bot.send_audio(chat_id, audio_cache[track_url])
            stats_data["total_downloads"] += 1
            save_json(STATS_FILE, stats_data)
            bot.delete_message(chat_id, msg.message_id)
            return
        except Exception as e:
            print(f"Ошибка отправки из кэша: {e}")
            del audio_cache[track_url]
            save_json(AUDIO_CACHE_FILE, audio_cache)

    # 2. ЕСЛИ В КЭШЕ НЕТ - СКАЧИВАЕМ (первый раз занимает время)
    bot.answer_callback_query(call.id, "📥 Скачиваю...")
    msg = bot.send_message(chat_id, "⏳ Подожди, загружаю с сервера...")

    audio_filename = None
    thumbnail_filename = None
    try:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": f"song_{chat_id}_%(id)s.%(ext)s",
            "writethumbnail": True,
            "quiet": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                {"key": "EmbedThumbnail"}
            ]
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(track_url, download=True)
            filename = ydl.prepare_filename(info)
            audio_filename = os.path.splitext(filename)[0] + ".mp3"
            
            base_name = os.path.splitext(filename)[0]
            for ext in ['.jpg', '.jpeg', '.png', '.webp']:
                if os.path.exists(base_name + ext):
                    thumbnail_filename = base_name + ext
                    break

        with open(audio_filename, "rb") as audio:
            thumb_file = open(thumbnail_filename, "rb") if thumbnail_filename and os.path.exists(thumbnail_filename) else None
            
            # Сохраняем результат отправки в переменную sent_msg
            sent_msg = bot.send_audio(
                chat_id, 
                audio, 
                title=info.get('title'), 
                performer=info.get('uploader'),
                thumb=thumb_file
            )
            if thumb_file:
                thumb_file.close()

            # 3. ДОБАВЛЯЕМ FILE_ID В КЭШ ДЛЯ СЛЕДУЮЩИХ РАЗОВ
            if sent_msg and sent_msg.audio:
                audio_cache[track_url] = sent_msg.audio.file_id
                save_json(AUDIO_CACHE_FILE, audio_cache)

        stats_data["total_downloads"] += 1
        save_json(STATS_FILE, stats_data)
        
        bot.delete_message(chat_id, msg.message_id)
    except Exception as e:
        print(f"Ошибка скачивания: {e}")
        bot.edit_message_text("❌ Не удалось скачать.", chat_id, msg.message_id)
    finally:
        if audio_filename and os.path.exists(audio_filename):
            try: os.remove(audio_filename)
            except: pass
        if thumbnail_filename and os.path.exists(thumbnail_filename):
            try: os.remove(thumbnail_filename)
            except: pass

bot.delete_webhook(drop_pending_updates=True)
bot.infinity_polling()


    
    
        
    
    
