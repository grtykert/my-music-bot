import os
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
import requests

API_TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(API_TOKEN)

user_ids = set()

@bot.message_handler(commands=['stats'])
def show_stats(message):
    bot.reply_to(message, f"📊 Всего уникальных пользователей в боте: {len(user_ids)}")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_ids.add(message.from_user.id)
    bot.reply_to(message, "👋 Привет! Напиши название трека, и я найду его. Дам послушать превью и дам ссылку на полную версию! 🎵\n\n⭐ Поддержать проект: /donate")

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
    msg = bot.reply_to(message, "🔍 Ищу треки...")
    
    try:
        response = requests.get(f"https://itunes.apple.com/search?term={query}&entity=song&limit=10")
        data = response.json()
        
        results = data.get("results", [])
        if not results:
            bot.edit_message_text("❌ Ничего не найдено.", message.chat.id, msg.message_id)
            return
            
        markup = InlineKeyboardMarkup(row_width=1)
        for track in results:
            title = track.get("trackName", "Трек")[:28]
            artist = track.get("artistName", "Исполнитель")[:20]
            track_id = track.get("trackId")
            
            btn_play = InlineKeyboardButton(text=f"🎧 Демо: {artist} - {title}", callback_data=f"play_{track_id}")
            markup.add(btn_play)
        
        if not hasattr(bot, 'tracks_cache'):
            bot.tracks_cache = {}
        bot.tracks_cache[message.chat.id] = results

        bot.edit_message_text("🎧 Выбери трек для прослушивания демо:", message.chat.id, msg.message_id, reply_markup=markup)
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        bot.edit_message_text("❌ Ошибка при поиске.", message.chat.id, msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("play_"))
def callback_send_audio(call):
    track_id = int(call.data.replace("play_", ""))
    tracks = getattr(bot, 'tracks_cache', {}).get(call.message.chat.id, [])
    
    track = next((t for t in tracks if t.get('trackId') == track_id), None)
    
    if not track:
        bot.answer_callback_query(call.id, "❌ Список устарел, введи запрос заново.")
        return

    audio_url = track.get("previewUrl")
    title = track.get('trackName', 'Music')
    performer = track.get('artistName', 'Artist')
    track_url = track.get('trackViewUrl', 'https://music.apple.com')

    bot.answer_callback_query(call.id, "🎶 Отправляю...")
    
    filename = "audio.mp3"
    try:
        r = requests.get(audio_url)
        with open(filename, "wb") as f:
            f.write(r.content)

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(text="🌐 Слушать полную версию (Apple Music)", url=track_url))

        with open(filename, "rb") as f:
            bot.send_audio(
                call.message.chat.id, 
                f, 
                title=title, 
                performer=performer,
                reply_markup=markup
            )

        if os.path.exists(filename):
            os.remove(filename)
            
    except Exception as e:
        print(f"Ошибка отправки: {e}")
        bot.send_message(call.message.chat.id, "❌ Не удалось отправить аудио.")
        if os.path.exists(filename):
            os.remove(filename)

bot.infinity_polling()
2

