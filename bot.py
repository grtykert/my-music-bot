import os
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, InputMediaAudio
import yt_dlp
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import time
import json
import imageio_ffmpeg

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
API_TOKEN = "8957555829:AAG6hOKd1aDDv5mHF_2gOtxmup6voikxsyk"
BACKUP_CHANNEL_ID = -1004445455425
ADMIN_ID = 5378591975  # Твой ID всегда на 1-м месте

bot = telebot.TeleBot(API_TOKEN)
tracks_cache = {}
inline_tracks_cache = {}
original_queries = {}
waiting_for_custom_stars = set()

# Переменные хранения данных (теперь список для контроля порядка)
active_users = []
stats_data = {"total_downloads": 0}
audio_cache = {}

# --- СИСТЕМА ЕДИНОГО БЭКАПА ЧЕРЕЗ ЗАКРЕП ---
def restore_all_data():
    global active_users, stats_data, audio_cache
    try:
        chat = bot.get_chat(BACKUP_CHANNEL_ID)
        if chat.pinned_message and chat.pinned_message.document:
            file_info = bot.get_file(chat.pinned_message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            data = json.loads(downloaded_file.decode('utf-8'))
            
            active_users = data.get("users", [])
            stats_data = data.get("stats", {"total_downloads": 0})
            audio_cache = data.get("cache", {})
            
            # Гарантируем, что ты всегда первый в списке при загрузке бэкапа
            if ADMIN_ID in active_users:
                active_users.remove(ADMIN_ID)
            active_users.insert(0, ADMIN_ID)
            
            print("✅ Все данные успешно восстановлены из закрепа!")
    except Exception as e:
        print(f"⚠️ Ошибка или отсутствие закрепа при автовосстановлении: {e}")
        # Если бэкапа нет, сразу добавляем тебя
        if ADMIN_ID not in active_users:
            active_users.insert(0, ADMIN_ID)

def save_all_data():
    try:
        # Перед сохранением убеждаемся, что ты на 1-м месте
        if ADMIN_ID in active_users:
            active_users.remove(ADMIN_ID)
        active_users.insert(0, ADMIN_ID)

        data = {
            "users": active_users,
            "stats": stats_data,
            "cache": audio_cache
        }
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            
        with open("data.json", "rb") as f:
            msg = bot.send_document(BACKUP_CHANNEL_ID, f, caption="💾 Бэкап базы данных")
            try:
                bot.pin_chat_message(BACKUP_CHANNEL_ID, msg.message_id, disable_notification=True)
            except Exception as pe:
                print(f"Ошибка закрепления: {pe}")
    except Exception as e:
        print(f"Ошибка сохранения бэкапа: {e}")

# Восстанавливаем данные перед стартом бота
restore_all_data()

bot_start_time = time.time()

def register_user(chat_id):
    if chat_id == ADMIN_ID:
        if chat_id in active_users:
            active_users.remove(chat_id)
        active_users.insert(0, chat_id)
        save_all_data()
    elif chat_id not in active_users:
        active_users.append(chat_id)
        save_all_data()

@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    register_user(chat_id)
    bot.reply_to(
        message, 
        "👋 Привет! Пиши название трека, я найду его. Пользуйся фильтрами и страницами! 🎵\n\n"
        "🔎 Можешь искать музыку прямо в любых чатах просто написав:`/m название`\n"
        "💬 Наш канал: https://t.me/teruteg\n\n"
        "💰 Поддержать разработчика: /donate\n"
        "📊 Статистика бота: /stats"
    )

# --- АДМИНСКАЯ КОМАНДА ДЛЯ ОЧИСТКИ КЭША ТРЕКОВ ---
@bot.message_handler(commands=['clearcache'])
def clear_bot_cache(message):
    if message.chat.id == ADMIN_ID:
        global audio_cache
        audio_cache.clear()
        save_all_data()
        bot.reply_to(message, "✅ Кэш треков успешно очищен! Статистика и пользователи сохранены. Теперь старые треки скачаются заново с нормальными обложками.")
    else:
        bot.reply_to(message, "❌ У вас нет прав для этой команды.")

@bot.my_chat_member_handler()
def handle_chat_member(message):
    chat_id = message.chat.id
    new_status = message.new_chat_member.status
    if new_status in ['kicked', 'left']:
        if chat_id in active_users and chat_id != ADMIN_ID:
            active_users.remove(chat_id)
            save_all_data()
    elif new_status == 'member':
        register_user(chat_id)

# --- ИНЛАЙН-РЕЖИМ ПОИСКА ВО ВСЕХ ЧАТАХ ---
@bot.inline_handler(func=lambda query: True)
def inline_query(query):
    search_text = query.query.strip()
    if not search_text:
        return
    
    try:
        ydl_opts = {"extract_flat": True, "quiet": True}
        search_query = f"ytsearch10:{search_text}"
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
                    description=f"Автор: {uploader} | Нажми для отправки в чат",
                    input_message_content=telebot.types.InputTextMessageContent(
                        message_text=f"⏳ Загружаю трек: {title[:40]}..."
                    )
                )
            )
        bot.answer_inline_query(query.id, results, cache_time=1)
    except Exception as e:
        print(f"Ошибка инлайн-поиска: {e}")

@bot.chosen_inline_handler(func=lambda chosen: True)
def handle_chosen_inline(chosen):
    user_id = chosen.from_user.id
    register_user(user_id)
    result_id = int(chosen.result_id)
    user_tracks = inline_tracks_cache.get(user_id, [])
    
    if result_id >= len(user_tracks):
        return
        
    track = user_tracks[result_id]
    track_url = track['url']
    title = track.get('title', 'Трек')
    uploader = track.get('uploader', 'Музыка')
    inline_msg_id = chosen.inline_message_id
    
    if track_url in audio_cache and audio_cache[track_url].startswith("http") == False:
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
    try:
        ydl_opts = {
            "format": "bestaudio/best",
            "cookiefile": "cookies.txt",  # <--- Добавлено
            "outtmpl": f"song_inline_{user_id}_%(id)s.%(ext)s",
            "writethumbnail": True,
            "quiet": True,
            "socket_timeout": 15,
            "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"},
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
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
            
            sent_media = bot.edit_message_media(
                media=InputMediaAudio(
                    media=audio,
                    title=info.get('title', title),
                    performer=info.get('uploader', uploader)
                ),
                inline_message_id=inline_msg_id
            )
            if thumb_file:
                thumb_file.close()

        stats_data["total_downloads"] += 1
        save_all_data()
    except Exception as e:
        print(f"Ошибка скачивания (инлайн): {e}")
        try:
            bot.edit_message_text(
                text="❌ Не удалось скачать выбранный трек.",
                inline_message_id=inline_msg_id
            )
        except:
            pass
    finally:
        if audio_filename and os.path.exists(audio_filename):
            try: os.remove(audio_filename)
            except: pass
        if thumbnail_filename and os.path.exists(thumbnail_filename):
            try: os.remove(thumbnail_filename)
            except: pass

@bot.message_handler(commands=['stats'])
def stats_command(message):
    chat_id = message.chat.id
    register_user(chat_id)
    
    start_ping = time.time()
    bot.get_me()
    ping_ms = int((time.time() - start_ping) * 1000)
    
    user_number = active_users.index(chat_id) + 1 if chat_id in active_users else len(active_users) + 1
    
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
    search_music_by_query(message, query=query, page=1, is_new=True)

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
        search_query = f"ytsearch20:{query}"
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
    
    bot.answer_callback_query(call.id, f"📥 Скачиваю: {title[:30]}...")
    msg = bot.send_message(chat_id, f"⏳ Загружаю трек: {title[:40]}...")
    
    if track_url in audio_cache and audio_cache[track_url].startswith("http") == False:
        try:
            bot.send_audio(chat_id, audio_cache[track_url], title=title, performer=uploader)
            bot.delete_message(chat_id, msg.message_id)
            stats_data["total_downloads"] += 1
            save_all_data()
            return
        except Exception as e:
            print(f"Ошибка отправки из кэша: {e}")

    audio_filename = None
    thumbnail_filename = None
    try:
        ydl_opts = {
            "format": "bestaudio/best",
            "cookiefile": "cookies.txt",  # <--- Добавлено
            "outtmpl": f"song_{chat_id}_%(id)s.%(ext)s",
            "writethumbnail": True,
            "quiet": True,
            "socket_timeout": 15,
            "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"},
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
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
            
            sent_msg = bot.send_audio(
                chat_id, 
                audio, 
                title=info.get('title', title), 
                performer=info.get('uploader', uploader),
                thumbnail=thumb_file
            )
            if thumb_file:
                thumb_file.close()
            
            audio_cache[track_url] = sent_msg.audio.file_id

        bot.delete_message(chat_id, msg.message_id)
        stats_data["total_downloads"] += 1
        save_all_data()
    except Exception as e:
        print(f"Ошибка скачивания: {e}")
        try:
            bot.edit_message_text("❌ Не удалось скачать трек.", chat_id, msg.message_id)
        except:
            pass
    finally:
        if audio_filename and os.path.exists(audio_filename):
            try: os.remove(audio_filename)
            except: pass
        if thumbnail_filename and os.path.exists(thumbnail_filename):
            try: os.remove(thumbnail_filename)
            except: pass

bot.infinity_polling()
        

    
            
