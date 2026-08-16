import os
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
import yt_dlp

API_TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(API_TOKEN)

user_data = {}


@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(message, "👋 Привет! Напиши название трека, и я скину аудио 🎵")


@bot.message_handler(func=lambda message: True)
def search_music(message):
  query = message.text
  msg = bot.reply_to(message, "🔍 Ищу аудио...")

  ydl_opts = {
      "format": "bestaudio/best",
      "quiet": True,
      "extract_flat": True,
      "force_generic_extractor": True,
  }

  try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
      info = ydl.extract_info(f"ytsearch5:{query}", download=False)
      tracks = info.get("entries", [])

      if not tracks:
        bot.edit_message_text(
            "❌ Ничего не найдено.", message.chat.id, msg.message_id
        )
        return

      user_data[message.chat.id] = tracks

      markup = InlineKeyboardMarkup()
      for i, track in enumerate(tracks):
        title = track.get("title", "Без названия")[:40]
        markup.add(
            InlineKeyboardButton(
                text=f"🎵 {title}", callback_data=f"send_audio_{i}"
            )
        )

      bot.edit_message_text(
          "🎧 Выбери трек:",
          message.chat.id,
          msg.message_id,
          reply_markup=markup,
      )
  except Exception as e:
    print(f"Ошибка поиска: {e}")
    bot.edit_message_text(
        "❌ Ошибка при поиске.", message.chat.id, msg.message_id
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("send_audio_"))
def callback_send_audio(call):
  index = int(call.data.replace("send_audio_", ""))
  tracks = user_data.get(call.message.chat.id, [])

  if not tracks or index >= len(tracks):
    bot.answer_callback_query(call.id, "❌ Список устарел, введи запрос заново.")
    return

  track = tracks[index]
  video_url = track.get("url") or f"https://youtu.be/{track.get('id')}"
  title = track.get("title", "Аудиозапись")

  bot.answer_callback_query(call.id, "📥 Загружаю аудио...")

  ydl_opts = {
      "format": "bestaudio",
      "outtmpl": "song.%(ext)s",
      "quiet": True,
  }

  try:
    filename = "song.m4a"
    if os.path.exists(filename):
      os.remove(filename)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
      info = ydl.extract_info(video_url, download=True)
      filename = ydl.prepare_filename(info)

    # Отправляем именно как аудио (будет полноценный плеер Telegram с ползунком)
    with open(filename, "rb") as f:
      bot.send_audio(
          call.message.chat.id, f, title=title[:50], performer="YouTube"
      )

    if os.path.exists(filename):
      os.remove(filename)
  except Exception as e:
    print(f"Ошибка отправки аудио: {e}")
    bot.send_message(
        call.message.chat.id,
        "❌ Не удалось отправить аудио. Попробуй другой трек.",
    )


bot.infinity_polling()

