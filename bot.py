import os
import threading
import telebot
from flask import Flask
import yt_dlp

app = Flask(__name__)


@app.route("/")
def home():
  return "Music Bot is alive!"


threading.Thread(
    target=lambda: app.run(host="0.0.0.0", port=10000), daemon=True
).start()

# Твой актуальный токен бота
TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(TOKEN)

# Твой личный числовой ID администратора
ADMIN_ID = 5378591975

CACHE_DIR = "downloads"
if not os.path.exists(CACHE_DIR):
  os.makedirs(CACHE_DIR)

USERS_FILE = "users.txt"


def add_user(user_id):
  if not os.path.exists(USERS_FILE):
    open(USERS_FILE, "w").close()

  with open(USERS_FILE, "r") as f:
    users = f.read().splitlines()

  if str(user_id) not in users:
    with open(USERS_FILE, "a") as f:
      f.write(str(user_id) + "\n")


def normalize_query(query):
  try:
    ru_query = translit(query, "ru")
  except:
    ru_query = query
  try:
    en_query = translit(query, "ru", reversed=True)
  except:
    en_query = query
  return [query, ru_query, en_query]


def download_and_send(message, query):
  queries_to_try = normalize_query(query)
  safe_query_for_filename = "".join(
      [c for c in query if c.isalnum() or c in (" ", "-", "_", ".")]
  ).strip()
  mp3_file = os.path.join(CACHE_DIR, f"{safe_query_for_filename}.mp3")

  if os.path.exists(mp3_file):
    with open(mp3_file, "rb") as audio:
      bot.send_audio(message.chat.id, audio)
    return

  sent_msg = bot.reply_to(message, f"Ищу варианты: {query} 🔍")

  found = False
  ydl_opts = {
      "format": "bestaudio/best",
      "outtmpl": f"{CACHE_DIR}/%(title)s.%(ext)s",
      "quiet": True,
      "no_warnings": True,
      "default_search": "scsearch1:",
      "postprocessors": [{
          "key": "FFmpegExtractAudio",
          "preferredcodec": "mp3",
          "preferredquality": "192",
      }],
  }

  for current_query in queries_to_try:
    if found:
      break
    try:
      with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(current_query, download=True)
        if "entries" in info:
          info = info["entries"][0]

        downloaded_file = ydl.prepare_filename(info)
        base, _ = os.path.splitext(downloaded_file)
        real_file = base + ".mp3"

        if os.path.exists(real_file):
          os.rename(real_file, mp3_file)
          found = True

    except Exception as e:
      continue

  if found:
    with open(mp3_file, "rb") as audio:
      bot.send_audio(message.chat.id, audio)
    bot.delete_message(message.chat.id, sent_msg.message_id)
  else:
    bot.edit_message_text(
        f"Не удалось найти трек по запросу '{query}'.",
        chat_id=message.chat.id,
        message_id=sent_msg.message_id,
    )


@bot.message_handler(commands=["start"])
def send_welcome(message):
  add_user(message.chat.id)
  bot.reply_to(
      message, "Привет! Напиши название трека, и я найду его полный вариант 🎵"
  )


@bot.message_handler(commands=["stats"])
def show_stats(message):
  if message.from_user.id != ADMIN_ID:
    bot.reply_to(message, "У тебя нет доступа к этой команде. ❌")
    return

  if os.path.exists(USERS_FILE):
    with open(USERS_FILE, "r") as f:
      users = f.read().splitlines()
    count = len(users)
  else:
    count = 0

  bot.reply_to(message, f"📊 Всего пользователей в твоем боте: {count}")


@bot.message_handler(func=lambda message: True)
def handle_message(message):
  threading.Thread(
      target=download_and_send, args=(message, message.text), daemon=True
  ).start()


bot.infinity_polling()
import os


# Обработка нажатия на кнопку (скачивание и отправка трека)
@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def callback_download(call):
  video_url = call.data.replace("dl_", "")
  bot.answer_callback_query(call.id, "📥 Скачиваю трек, погоди секунду...")

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

    # Отправляем аудиофайл пользователю
    with open(mp3_filename, "rb") as audio:
      bot.send_audio(call.message.chat.id, audio)

    # Удаляем файл с сервера, чтобы не забивать память
    if os.path.exists(mp3_filename):
      os.remove(mp3_filename)

  except Exception as e:
    bot.send_message(
        call.message.chat.id, "❌ Ошибка при скачивании трека. Попробуй другой!"
    )
    print(f"Ошибка скачивания: {e}")
    from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup


# Перехватываем обычное сообщение, чтобы выдать кнопки с выбором треков
@bot.message_handler(func=lambda message: True)
def handle_message_with_buttons(message):
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
    markup.add(
        InlineKeyboardButton(text=f"🎵 {title}", callback_data=f"dl_{video_url}")
    )

  bot.edit_message_text(
      "🎧 Выбери трек для скачивания:",
      message.chat.id,
      msg.message_id,
      reply_markup=markup,
  )


# Обработка нажатия на кнопку (скачивание выбранного трека)
@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def callback_download_track(call):
  video_url = call.data.replace("dl_", "")
  bot.answer_callback_query(call.id, "📥 Скачиваю...")

  ydl_opts = {
      "format": "bestaudio/best",
      "postprocessors": [{
          "key": "FFmpegExtractAudio",
          "preferredcodec": "mp3",
          "preferredquality": "192",
      }],
      "outtmpl": "song.%(ext)s",
      "quiet": True,
  }

  try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
      ydl.download([video_url])

    with open("song.mp3", "rb") as audio:
      bot.send_audio(call.message.chat.id, audio)
    os.remove("song.mp3")
  except Exception as e:
    bot.send_message(call.message.chat.id, "❌ Ошибка скачивания.")
      




