import os
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
import yt_dlp
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- ФЕЙКОВЫЙ ВЕБ-СЕРВЕР ---
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"Bot is active and running!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server_address = ('0.0.0.0', port)
    httpd = HTTPServer(server_address, DummyHandler)
    httpd.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()
# ----------------------------

API_TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(API_TOKEN)

user_ids = set()
tracks_cache = {}

@bot.message_handler(commands=['stats'])
def show_stats(message):
    bot.reply_to(message, f"📊 Всего уникальных пользователей в боте: {len(user_ids)}")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_ids.add(message.from_user.id)
    bot.reply_to(message, "👋 Привет! Напиши название трека, и я найду его полный вариант! 🎵\n\n⭐ Поддержать проект: /donate")

@bot.message_handler(commands=['donate'])
def donate_command(message):
    user_ids.add(message.from_user.id)
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton(text='⭐ 1 Звезда', callback_data='donate_1'),
        InlineKeyboardButton(text='⭐ 5 Звезд', callback_data='donate_5'),
        InlineKeyboardButton(text='⭐ 10 Звезд', callback_data='donate_10'),
        InlineKeyboardButton(text='⭐ 25 Звезд', callback_data='donate_25')
    )
    bot.reply_to(message, "💖 Выбери сумму поддержки:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('donate_'))
def process_donate_selection(call):
    stars_count = int(call.data.replace('donate_', ''))
    bot.answer_callback_query(call.id)
    bot.send_invoice(
        chat_id=call.message.chat.id,
        title="Поддержка бота",
        description=f"Спасибо за развитие проекта! Поддержка на {stars_count} ⭐",
        invoice_payload=f"donate_{stars_count}_stars",
        provider_token="",  
        currency="XTR",     
        prices=[LabeledPrice(label=f"{stars_count} Звезд(ы)", amount=stars_count)]
    )

@bot.pre_checkout_query_handler(func=lambda query: True)
def pre_checkout_query(query):
    bot.answer_pre_checkout_query(query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    bot.reply_to(message, f"🎉 Спасибо большое за поддержку! Получено звезд: {message.successful_payment.total_amount} ⭐")

@bot.message_handler(func=lambda message: True)
def search_music(message):
    user_ids.add(message.from_user.id)
    query = message.text
    msg = bot.reply_to(message, "🔍 Ищу варианты треков на YouTube...")
    
    ydl_opts = {"extract_flat": True, "quiet": True}

    try:
        search_query = f"ytsearch5:{query}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(search_query, download=False)
            tracks = result.get("entries", [])
            
        if not tracks:
            bot.edit_message_text("❌ Ничего не найдено.", message.chat.id, msg.message_id)
            return
            
        tracks_cache[message.chat.id] = tracks
        markup = InlineKeyboardMarkup(row_width=1)
        
        for i, track in enumerate(tracks):
            title = track.get("title", "Без названия")[:38]
            markup.add(InlineKeyboardButton(text=f"🎵 {title}", callback_data=f"dl_{i}"))

        bot.edit_message_text("🎧 Выбери трек для скачивания:", message.chat.id, msg.message_id, reply_markup=markup)
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        bot.edit_message_text("❌ Ошибка при поиске треков.", message.chat.id, msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def callback_download_track(call):
    index = int(call.data.replace("dl_", ""))
    user_tracks = tracks_cache.get(call.message.chat.id, [])
    
    if index >= len(user_tracks):
        bot.answer_callback_query(call.id, "❌ Список устарел, отправь запрос заново.")
        return

    track = user_tracks[index]
    video_url = track.get("url")
    
    bot.answer_callback_query(call.id, "📥 Скачиваю...")
    msg = bot.send_message(call.message.chat.id, "⏳ Качаю трек, секунду...")

    # Скачиваем сразу лучший доступный аудиофайл без конвертации через ffmpeg
    ydl_opts = {
        "format": "bestaudio",
        "outtmpl": f"song_{call.message.chat.id}_%(id)s.%(ext)s",
        "quiet": True,
    }

    audio_filename = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            audio_filename = ydl.prepare_filename(info)
            title = info.get('title', 'Музыка')
            uploader = info.get('uploader', 'Исполнитель')

        with open(audio_filename, "rb") as audio:
            bot.send_audio(call.message.chat.id, audio, title=title, performer=uploader)

        bot.delete_message(call.message.chat.id, msg.message_id)

        if audio_filename and os.path.exists(audio_filename):
            os.remove(audio_filename)
            
    except Exception as e:
        print(f"Ошибка скачивания: {e}")
        bot.edit_message_text("❌ Не удалось скачать трек.", call.message.chat.id, msg.message_id)
        if audio_filename and os.path.exists(audio_filename):
            os.remove(audio_filename)

bot.delete_webhook(drop_pending_updates=True)
bot.infinity_polling()



    

        

                

