import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
import requests

API_TOKEN = "ТВОЙ_ТОКЕН_БОТА"
bot = telebot.TeleBot(API_TOKEN)

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "👋 Привет! Напиши название трека, и я найду его 🎵")

@bot.message_handler(func=lambda message: True)
def search_music(message):
    query = message.text
    msg = bot.reply_to(message, "🔍 Ищу в интернете...")
    
    try:
        # Ищем треки через открытое музыкальное API
        response = requests.get(f"https://itunes.apple.com/search?term={query}&entity=song&limit=5")
        data = response.json()
        
        results = data.get("results", [])
        if not results:
            bot.edit_message_text("❌ Ничего не найдено.", message.chat.id, msg.message_id)
            return
            
        markup = InlineKeyboardMarkup()
        for track in results:
            title = track.get("trackName", "Трек")[:35]
            artist = track.get("artistName", "Исполнитель")[:25]
            audio_url = track.get("previewUrl")
            
            if audio_url:
                markup.add(InlineKeyboardButton(text=f"🎵 {artist} - {title}", callback_data=f"play_{track.get('trackId')}"))
        
        if not hasattr(bot, 'tracks_cache'):
            bot.tracks_cache = {}
        bot.tracks_cache[message.chat.id] = results

        bot.edit_message_text("🎧 Выбери трек:", message.chat.id, msg.message_id, reply_markup=markup)
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
    title = f"{track.get('artistName', '')} - {track.get('trackName', 'Трек')}"

    bot.answer_callback_query(call.id, "🎶 Отправляю...")
    
    try:
        # Отправляем аудиофайл напрямую по ссылке из интернета
        bot.send_audio(call.message.chat.id, audio_url, title=title[:50])
    except Exception as e:
        print(f"Ошибка отправки: {e}")
        bot.send_message(call.message.chat.id, "❌ Не удалось отправить аудио.")

bot.infinity_polling()

