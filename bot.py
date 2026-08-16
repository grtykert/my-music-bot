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
TOKEN = "8957555829:AAHtDCNwFA2OIP1VQHXPunYXScETR1xM37k"
bot = telebot.TeleBot(TOKEN)


@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(message, "Привет! Напиши название трека, и я найду его 🎵")


# Функция для скачивания в фоновом потоке, чтобы бот не зависал
def download_and_send(message, query):
  sent_msg = bot.reply_to(message, f"Ищу трек: {query} 🔍")

  ydl_opts = {
      "format": "bestaudio/best",
      "outtmpl": "downloads/%(title)s.%(ext)s",
      "default_search": "ytsearch1:",
      "user_agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/120.0.0.0 Safari/537.36"
      ),
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
        f"Не удалось скачать трек. Ошибка: {e}",
      




