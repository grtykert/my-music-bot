import os
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
import requests

API_TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(API_TOKEN)

user_ids = set()

@bot.message_handler(commands=['stats'])
def show_stats(message):
    bot.reply_to(message, f"📊 Всего пользователей в боте: {len(user_ids)}")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "👋 Привет! Напиши название трека, и я найду его. Дам послушать превью и дам ссылку на полную версию! 🎵")

@bot.message_handler(func=lambda message: True)
def search_music(message):
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
            
            # Кнопка для отправки 30 секунд в чат
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

        # Создаем клавиатуру с сылками на полные сервисы
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

@bot.message_handler(commands=['donate'])
def donate_command(message):
    markup = types.InlineKeyboardMarkup()
    btn = types.InlineKeyboardButton(text='⭐ Поддержать на 1⭐', pay=True)
    markup.add(btn)
    
    bot.send_invoice(
        chat_id=message.chat.id,
        title="Поддержка бота",
        description="Спасибо за развитие проекта! ⭐",
        invoice_payload="monthly_donate",
        provider_token="",  # Обязательно пусто для Telegram Stars!
        currency="XTR",     # Валюта — звезды
        prices=[types.LabeledPrice(label="Звезда", amount=1)]  # Сумма в штуках (1 звезда)
    )

# Обязательный шаг перед оплатой
@bot.pre_checkout_query_handler(func=lambda query: True)
def pre_checkout_query(query):
    bot.answer_pre_checkout_query(query.id, ok=True)

# Что происходит после успешной оплаты
@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    bot.reply_to(message, f"🎉 Спасибо большое за поддержку! Получено звезд: {message.successful_payment.total_amount} ⭐")
