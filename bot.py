import os
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
import yt_dlp

API_TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(API_TOKEN)
user_data = {}

@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.reply_to(message, "👋 Привет! Напиши название трека 🎵")

def get_tracks_keyboard(tracks, page=0):
    markup = InlineKeyboardMarkup()
    start_idx, end_idx = page * 5, page * 5 + 5
    for i, track in enumerate(tracks[start_idx:end_idx]):
        markup.add(InlineKeyboardButton(f"🎵 {track['title'][:35]}", callback_data=f"dl_{start_idx + i}"))
    if end_idx < len(tracks):
        markup.add(InlineKeyboardButton("Вперед ➡️", callback_data=f"page_{page+1}"))
    return markup

@bot.message_handler(func=lambda message: True)
def search(message):
    msg = bot.reply_to(message, "🔍 Ищу...")
    ydl_opts = {"format": "bestaudio/best", "quiet": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch5:{message.text}", download=False)
            tracks = info.get("entries", [])
            user_data[message.chat.id] = tracks
            bot.edit_message_text("🎧 Выбери трек:", message.chat.id, msg.message_id, reply_markup=get_tracks_keyboard(tracks))
        except:
            bot.edit_message_text("❌ Ошибка.", message.chat.id, msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def download(call):
    idx = int(call.data.split("_")[1])
    track = user_data.get(call.message.chat.id)[idx]
    bot.answer_callback_query(call.id, "⚡️ Отправляю...")
    
    # Упрощенная опция для максимально быстрой передачи
    ydl_opts = {
        "format": "bestaudio",
        "outtmpl": "audio.mp3",
        "quiet": True,
        "no_warnings": True
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={track['id']}"])
        with open("audio.mp3", "rb") as f:
            bot.send_audio(call.message.chat.id, f, title=track['title'])
        os.remove("audio.mp3")
    except Exception as e:
        bot.send_message(call.message.chat.id, "❌ Ошибка отправки аудио.")

bot.infinity_polling()

