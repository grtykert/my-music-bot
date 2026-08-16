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
      2
      




