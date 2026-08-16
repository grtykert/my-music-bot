import os
import threading
import telebot
from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
  return "Bot is alive!"


def run_web():
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)


threading.Thread(target=run_web, daemon=True).start()

TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(TOKEN)


@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(message, "Привет! Бот успешно запущен в облаке!")


@bot.message_handler(func=lambda message: True)
def echo_all(message):
  bot.reply_to(message, f"Эхо: {message.text}")


bot.infinity_polling()

