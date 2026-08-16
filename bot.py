import os
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
import yt_dlp

API_TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(API_TOKEN)


@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(message, "👋 Привет! Напиши название трека, и я найду варианты 🎵")


@bot.message_handler(func=lambda message: True)
def handle_message(message):
  query = message.text
  msg = bot.reply_to(message, "🔍 Ищу варианты...")

  ydl_opts = {"extract_flat": True, "default_search": "ytsearch5", "quiet": True}

  with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    try:
      result = ydl.extract_info(query, download=False)
      tracks = result.get("entries", [])
    except Exception:
      tracks = []

  if not tracks:
    bot.edit_message_text(
        "❌ Ничего не найдено.", message.chat.id, msg.message_id
    )
    return

  markup = InlineKeyboardMarkup()
  for track in tracks:
    title = track.get("title", "Без названия")[:40]
    video_url = track.get("url")
    if video_url:
      markup.add(
          InlineKeyboardButton(
              text=f"🎵 {title}", callback_data=f"dl_{video_url}"
          )
      )

  bot.edit_message_text(
      "🎧 Выбери трек для скачивания:",
      message.chat.id,
      msg.message_id,
      reply_markup=markup,
  )


@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def callback_download(call):
  video_url = call.data.replace("dl_", "")
  bot.answer_callback_query(call.id, "📥 Скачиваю...")

  ydl_opts = {
      "format": "bestaudio/best",
      "postprocessors": [{
          "key": "FFmpegExtractAudio",
          "preferredcodec": "mp3",
          "preferredquality": "192",
      }],
      "outtmpl": "song_%(id)s.%(ext)s",
      "quiet": True,
  }

  try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
      info = ydl.extract_info(video_url, download=True)
      filename = ydl.prepare_filename(info)
      mp3_filename = os.path.splitext(filename)[0] + ".mp3"

    with open(mp3_filename, "rb") as audio:
      bot.send_audio(call.message.chat.id, audio)

    if os.path.exists(mp3_filename):
      os.remove(mp3_filename)
  except Exception as e:
    bot.send_message(call.message.chat.id, "❌ Ошибка при скачивании трека.")
    print(f"Ошибка: {e}")


bot.infinity_polling()
      




