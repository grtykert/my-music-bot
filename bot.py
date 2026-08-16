import os
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
import yt_dlp

API_TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(API_TOKEN)

user_data = {}


@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(message, "👋 Привет! Просто напиши название трека 🎵")


def get_tracks_keyboard(tracks, page=0):
  markup = InlineKeyboardMarkup()
  start_idx = page * 5
  end_idx = start_idx + 5
  page_tracks = tracks[start_idx:end_idx]

  for track in page_tracks:
    title = track.get("title", "Без названия")[:40]
    video_url = track.get("url") or f"https://youtu.be/{track.get('id')}"
    # Убрали кривое время, оставили только аккуратное название
    markup.add(InlineKeyboardButton(f"🎵 {title}", callback_data=f"dl_{video_url}"))

  nav_buttons = []
  if page > 0:
    nav_buttons.append(
        InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{page-1}")
    )
  if end_idx < len(tracks):
    nav_buttons.append(
        InlineKeyboardButton("Вперед ➡️", callback_data=f"page_{page+1}")
    )
  if nav_buttons:
    markup.row(*nav_buttons)
  return markup


@bot.message_handler(func=lambda message: True)
def search_message(message):
  query = message.text
  msg = bot.reply_to(message, "🔍 Ищу варианты...")

  ydl_opts = {
      "format": "bestaudio",
      "quiet": True,
      "extract_flat": True,
      "force_generic_extractor": True,
  }
  with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    try:
      info = ydl.extract_info(f"ytsearch10:{query}", download=False)
      tracks = info.get("entries", [])
      user_data[message.chat.id] = {"tracks": tracks, "page": 0}

      bot.edit_message_text(
          "🎧 Выбери трек:",
          message.chat.id,
          msg.message_id,
          reply_markup=get_tracks_keyboard(tracks),
      )
    except Exception:
      bot.edit_message_text(
          "❌ Ошибка поиска.", message.chat.id, msg.message_id
      )


@bot.callback_query_handler(func=lambda call: call.data.startswith("page_"))
def callback_page(call):
  page = int(call.data.split("_")[1])
  tracks = user_data[call.message.chat.id]["tracks"]
  user_data[call.message.chat.id]["page"] = page
  bot.edit_message_reply_markup(
      call.message.chat.id,
      call.message.message_id,
      reply_markup=get_tracks_keyboard(tracks, page),
  )


@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def callback_download(call):
  video_url = call.data.replace("dl_", "", 1)
  bot.answer_callback_query(call.id, "📥 Скачиваю...")

  ydl_opts = {
      "format": "bestaudio",
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

    with open(mp3_filename, "rb") as f:
      bot.send_audio(call.message.chat.id, f)

    if os.path.exists(mp3_filename):
      os.remove(mp3_filename)
  except Exception as e:
    print(f"Ошибка загрузки: {e}")
    bot.send_message(
        call.message.chat.id,
        "❌ Не удалось скачать. Попробуй другой вариант.",
    )


bot.infinity_polling()

