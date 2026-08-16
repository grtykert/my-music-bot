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
TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(TOKEN)


@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(
      message,
      "Привет! Напиши название трека или исполнителя, и я найду и скачаю его"
      " для тебя 🎵",
  )


@bot.message_handler(func=lambda message: True)
def handle_message(message):
  query = message.text
  bot.reply_to(message, f"Ищу трек: {query} 🔍")

  # Настройки поиска и скачивания через yt-dlp по названию
  ydl_opts = {
      "format": "bestaudio/best",
      "outtmpl": "downloads/%(title)s.%(ext)s",
      "default_search": "ytsearch1:",  две строчки включают поиск по названию (берет 1-й результат)
      "postprocessors": [{
          "key": "FFmpegExtractAudio",
          "preferredcodec": "mp3",
          "preferredquality": "192",
      }],
  }

  try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
      # Передаем поисковый запрос
      info = ydl.extract_info(query, download=True)

      # Если поиск шел через ytsearch, результат возвращается в виде словаря с ключом 'entries'
      if "entries" in info:
        info = info["entries"][0]

      filename = ydl.prepare_filename(info)
      base, _ = os.path.splitext(filename)
      mp3_file = base + ".mp3"

    with open(mp3_file, "rb") as audio:
      # Отправляем именно как музыкальный плеер (аудио)
      bot.send_audio(message.chat.id, audio)

    # Удаляем файл с сервера
    os.remove(mp3_file)

  except Exception as e:
    bot.reply_to(message, f"Не удалось найти или скачать трек: {e}")


bot.infinity_polling()




