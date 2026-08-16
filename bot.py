from flask import Flask
import threading
import telebot
import yt_dlp
app = Flask(__name__)
@app.route('/')
def home():
   return "Bot is alive!"
def run_web():
    app.run(host='0.0.0.0', port=10000)
threading.Thread(target=run_web).daemon = True
TOKEN = "8957555829:AAHtDCNwFA2OIP1VQHXPunYXscEtRlxM37k"
bot = telebot.TeleBot(TOKEN)
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Бот успешно запущен в облаке!")
@bot.message_handler(func=lambda message: True)
def echo_all(message):
    bot.reply_to(message, f"Эхо: {message.text}")
bot.infinity_polling()


