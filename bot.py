import os
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
import yt_dlp
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- ВЕБ-СЕРВЕР (Для Render) ---
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(('0.0.0.0', port), DummyHandler).serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()

# --- БОТ ---
API_TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(API_TOKEN)
tracks_cache = {}
original_queries = {}

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "👋 Привет! Пиши название трека, я найду его. Пользуйся фильтрами и страницами! 🎵\n\n💰 Поддержать разработчика: /donate")

# --- КОМАНДА ДОНАТА ---
@bot.message_handler(commands=['donate'])
def donate_command(message):
    chat_id = message.chat.id
    # Отправляем инвойс на тестовую звезду Telegram (или кастомную валюту XTR / копейки)
    # Здесь используется Telegram Stars (XTR) или тестовый платеж
    try:
        prices = [LabeledPrice(label='Поддержать бота ☕', amount=1)] # 1 единица (можно изменить)
        bot.send_invoice(
            chat_id=chat_id,
            title='Поддержка проекта',
            description='Спасибо за развитие бота! Эти деньги пойдут на оплату стабильной работы.',
            invoice_payload='donate_payload',
            provider_token='', # Пусто для Telegram Stars (XTR)
            currency='XTR',
            prices=prices,
            start_parameter='donate'
        )
    except Exception as e:
        # Если Telegram Stars недоступны, отправляем текстом
        bot.reply_to(message, "☕ Огромное спасибо за желание поддержать проект! Пока что донаты настраиваются через Telegram Stars, либо ты можешь просто пользоваться ботом и рекомендовать его друзьям!")

@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    bot.reply_to(message, "🎉 Ура! Огромное спасибо за донат! Твоя поддержка очень важна для проекта ❤️")

@bot.message_handler(func=lambda message: not message.text.startswith('/'))
def text_search_handler(message):
    original_queries[message.chat.id] = message.text
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def callback_download_track(call):
    index = int(call.data.replace("dl_", ""))
    user_tracks = tracks_cache.get(call.message.chat.id, [])
    if index >= len(user_tracks):
        bot.answer_callback_query(call.id, "❌ Список устарел.")
        return

    track = user_tracks[index]
    bot.answer_callback_query(call.id, "📥 Скачиваю...")
    msg = bot.send_message(call.message.chat.id, "⏳ Подожди, конвертирую...")

    audio_filename = None
    try:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": f"song_{call.message.chat.id}_%(id)s.%(ext)s",
            "quiet": True,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(track['url'], download=True)
            filename = ydl.prepare_filename(info)
            audio_filename = os.path.splitext(filename)[0] + ".mp3"
            
        with open(audio_filename, "rb") as audio:
            bot.send_audio(call.message.chat.id, audio, title=info.get('title'), performer=info.get('uploader'))
        bot.delete_message(call.message.chat.id, msg.message_id)
    except Exception as e:
        print(f"Ошибка скачивания: {e}")
        bot.edit_message_text("❌ Не удалось скачать.", call.message.chat.id, msg.message_id)
    finally:
        if audio_filename and os.path.exists(audio_filename):
            try:
                os.remove(audio_filename)
            except:
                pass

bot.delete_webhook(drop_pending_updates=True)
bot.infinity_polling()
