import os
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
import requests

API_TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(API_TOKEN)

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "👋 Привет! Напиши название трека, и я найду его и пришлю аудиофайлом 🎵")

@bot.message_handler(func=lambda message: True)
def search_music(message):
    query = message.text
    msg = bot.reply_to(message, "🔍 Ищу трек...")
    
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        r = requests.get(f"https://api-v2.soundcloud.com/search/tracks?q={query}&client_id=i15yYToGBsvhmSs7dbVyXYJJTkgbxmmg&limit=8", headers=headers)
        data = r.json()
        tracks = data.get("collection", [])
        
        if not tracks:
            bot.edit_message_text("❌ Ничего не найдено.", message.chat.id, msg.message_id)
            return
            
        markup = InlineKeyboardMarkup(row_width=1)
        valid_tracks = []
        
        for track in tracks:
            stream_url = track.get("media", {}).get("transcodings", [{}])[0].get("url")
            if stream_url:
                title = track.get("title", "Трек")[:30]
                artist = track.get("user", {}).get("username", "Исполнитель")[:20]
                track_id = len(valid_tracks)
                
                valid_tracks.append({
                    "title": title,
                    "artist": artist,
                    "uri": stream_url
                })
                
                markup.add(InlineKeyboardButton(text=f"🎵 {artist} - {title}", callback_data=f"sc_{track_id}"))

        if not valid_tracks:
            bot.edit_message_text("❌ Не удалось найти доступные треки.", message.chat.id, msg.message_id)
            return

        if not hasattr(bot, 'sc_cache'):
            bot.sc_cache = {}
        bot.sc_cache[message.chat.id] = valid_tracks

        bot.edit_message_text("🎧 Выбери трек для загрузки:", message.chat.id, msg.message_id, reply_markup=markup)
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        bot.edit_message_text("❌ Ошибка при поиске треков.", message.chat.id, msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("sc_"))
def callback_send_audio(call):
    track_index = int(call.data.replace("sc_", ""))
    tracks = getattr(bot, 'sc_cache', {}).get(call.message.chat.id, [])
    
    if track_index >= len(tracks):
        bot.answer_callback_query(call.id, "❌ Список устарел, введи запрос заново.")
        return

    track = tracks[track_index]
    bot.answer_callback_query(call.id, "📥 Скачиваю трек...")

    filename = f"track_{call.message.chat.id}.mp3"
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        stream_meta = requests.get(f"{track['uri']}?client_id=i15yYToGBsvhmSs7dbVyXYJJTkgbxmmg", headers=headers).json()
        mp3_url = stream_meta.get("url")
        
        if not mp3_url:
            bot.send_message(call.message.chat.id, "❌ Не удалось получить ссылку на поток.")
            return

        r = requests.get(mp3_url, headers=headers)
        with open(filename, "wb") as f:
            f.write(r.content)

        with open(filename, "rb") as f:
            bot.send_audio(
                call.message.chat.id, 
                f, 
                title=track['title'], 
                performer=track['artist']
            )

        if os.path.exists(filename):
            os.remove(filename)
            
    except Exception as e:
        print(f"Ошибка отправки: {e}")
        bot.send_message(call.message.chat.id, "❌ Ошибка при загрузке трека.")
        if os.path.exists(filename):
            os.remove(filename)

bot.infinity_polling()
