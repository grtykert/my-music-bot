import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
import requests

API_TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(API_TOKEN)

@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.reply_to(message, "👋 Привет! Напиши название трека, и я поищу его.")

@bot.message_handler(func=lambda message: True)
def search_music(message):
    query = message.text
    msg = bot.reply_to(message, "🔍 Ищу музыку...")
    
    try:
        # Используем открытое музыкальное API для поиска треков
        response = requests.get(f"https://itunes.apple.com/search?term={query}&entity=song&limit=5")
        data = response.json()
        
        results = data.get("results", [])
        if not results:
            bot.edit_message_text("❌ Ничего не найдено.", message.chat.id, msg.message_id)
            return
            
        markup = InlineKeyboardMarkup()
        for track in results:
            title = track.get("trackName", "Трек")[:30]
            artist = track.get("artistName", "Исполнитель")[:20]
            preview_url = track.get("previewUrl") # Ссылка на превью (30 секунд)
            
            if preview_url:
                markup.add(InlineKeyboardButton(text=f"🎵 {artist} - {title}", callback_data=f"play_{preview_url}"))
                
        bot.edit_message_text("🎧 Выбери трек для прослушивания:", message.chat.id, msg.message_id, reply_markup=markup)
    except Exception as e:
        print(f"Ошибка: {e}")
        bot.edit_message_text("❌ Произошла ошибка при поиске.", message.chat.id, msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("play_"))
def callback_play(call):
    audio_url = call.data.replace("play_", "", 1)
    bot.answer_callback_query(call.id, "🎶 Отправляю трек...")
    try:
        bot.send_audio(call.message.chat.id, audio_url, caption="🎵 Музыкальный бот")
    except Exception as e:
        print(f"Ошибка отправки: {e}")
        bot.send_message(call.message.chat.id, "❌ Не удалось отправить аудио.")

bot.infinity_polling()

