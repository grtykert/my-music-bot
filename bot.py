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
TOKEN = "ТВОЙ_ТОКЕН_БОТА"
bot = telebot.TeleBot(TOKEN)


@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(
      message,
      "Привет! Отправь мне ссылку на видео или аудио, и я попробую его скачать"
      " для тебя.",
  )


@bot.message_handler(func=lambda message: True)
def handle_message(message):
  url = message.text
  bot.reply_to(message, "Скачиваю... Подожди немного ⏳")

  ydl_opts = {
      "format": "bestaudio/best",
      "outtmpl": "downloads/%(title)s.%(ext)s",
      "postprocessors": [{
          "key": "FFmpegExtractAudio",
          "preferredcodec": "mp3",
          "preferredquality": "192",
      }],
  }

  try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
      info = ydl.extract_info(url, download=True)
      filename = ydl.prepare_filename(info)
      # Заменяем расширение на mp3 после конвертации
      base, _ = os.path.splitext(filename)
      mp3_file = base + ".mp3"

    with open(mp3_file, "rb") as audio:
      bot.send_audio(message.chat.id, audio)

    # Удаляем файл с сервера после отправки, чтобы не забивать память
    os.remove(mp3_file)

  except Exception as e:
    bot.reply_to(message, f"Произошла ошибка при скачивании: {e}")


bot.infinity_polling()




