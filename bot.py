import os
import threading
import telebot
from flask import Flask
import yt_dlp

# 1. Веб-сервер для Render
app = Flask(__name__)


@app.route("/")
def home():
  return "Music Bot is alive!"


def run_web():
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)


threading.Thread(target=run_web, daemon=True).start()

# 2. Настройка бота
TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY1"
bot = telebot.TeleBot(TOKEN)


@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(message, "Привет! Напиши название трека, и я поищу его в VK 🎵")


@bot.message_handler(func=lambda message: True)
def handle_message(message):
  query = message.text
  sent_msg = bot.reply_to(message, f"Ищу в VK: {query} 🔍")

  # Строго поиск через VK
  ydl_opts = {
      "format": "bestaudio/best",
      "outtmpl": "downloads/%(title)s.%(ext)s",
      "default_search": "vksearch1:",
      "noplaylist": True,
      "postprocessors": [{
          "key": "FFmpegExtractAudio",
          "preferredcodec": "mp3",
          "preferredquality": "192",
      }],
  }

  try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
      info = ydl.extract_info(query, download=True)
      
      if "entries" in info:
        info = info["entries"][0]

      filename = ydl.prepare_filename(info)
      base, _ = os.path.splitext(filename)
      mp3_file = base + ".mp3"

    with open(mp3_file, "rb") as audio:
      bot.send_audio(message.chat.id, audio)

    os.remove(mp3_file)
    bot.delete_message(message.chat.id, sent_msg.message_id)

  except Exception as e:
    bot.edit_message_text(
        f"Не удалось найти трек в VK. Ошибка: {e}",
        chat_id=message.chat.id,
        message_id=sent_msg.message_id,
    )


bot.infinity_polling()




