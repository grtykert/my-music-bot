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
  bot.reply_to(message, "Привет! Напиши название трека, и я попробую его найти 🎵")


@bot.message_handler(func=lambda message: True)
def handle_message(message):
  query = message.text
  sent_msg = bot.reply_to(message, f"Ищу трек: {query} 🔍")

  # Используем стандартный поиск, но с таймаутами и без лишних падений
  ydl_opts = {
      "format": "bestaudio/best",
      "outtmpl": "downloads/%(title)s.%(ext)s",
      "default_search": "ytsearch1:",
      "noplaylist": True,
      "postprocessors": [{
          "key": "FFmpegExtractAudio",
          "preferredcodec": "mp3",
          "preferredquality": "192",
      }],
  }

  try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
      info = ydl.extract_info(query, download=False)
      if "entries" in info:
        info = info["entries"][0]

      # Получаем прямую ссылку на найденное видео
      video_url = info.get("webpage_url") or info.get("url")

      # Скачиваем уже конкретную ссылку, а не через поисковый запрос напрямую
      ydl.download([video_url])
      filename = ydl.prepare_filename(info)
      base, _ = os.path.splitext(filename)
      mp3_file = base + ".mp3"

    with open(mp3_file, "rb") as audio:
      bot.send_audio(message.chat.id, audio)

    os.remove(mp3_file)

  except Exception as e:
    bot.edit_message_text(
        f"Не удалось скачать трек. Ошибка: {e}",
        chat_id=message.chat.id,
        message_id=sent_msg.message_id,
    )


bot.infinity_polling()




