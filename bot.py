import os
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote

API_TOKEN = "8957555829:AAFXEQ7b24M5YMbnZpRB8cYLnSi-VL6zraY"
bot = telebot.TeleBot(API_TOKEN)

user_ids = set()

@bot.message_handler(commands=['stats'])
def show_stats(message):
    bot.reply_to(message, f"📊 Всего уникальных пользователей в боте: {len(user_ids)}")

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_ids.add(message.from_user.id)
    bot.reply_to(message, "👋 Привет! Напиши название трека, и я найду готовую полную версию! 🎵\n\n⭐ Поддержать проект: /donate")

@bot.message_handler(commands=['donate'])
def donate_command(message):
    user_ids.add(message.from_user.id)
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton(text='⭐ 1 Звезда', callback_data='donate_1'),
        InlineKeyboardButton(text='⭐ 5 Звезд', callback_data='donate_5'),
        InlineKeyboardButton(text='⭐ 10 Звезд', callback_data='donate_10'),
        InlineKeyboardButton(text='⭐ 25 Звезд', callback_data='donate_25')
    )
    bot.reply_to(message, "💖 Выбери сумму поддержки:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('donate_'))
def process_donate_selection(call):
    stars_count = int(call.data.replace('donate_', ''))
    bot.answer_callback_query(call.id)
    
    bot.send_invoice(
        chat_id=call.message.chat.id,
        title="Поддержка бота",
        description=f"Спасибо за развитие проекта! Поддержка на {stars_count} ⭐",
        invoice_payload=f"donate_{stars_count}_stars",
        provider_token="",  
        currency="XTR",     
        prices=[LabeledPrice(label=f"{stars_count} Звезд(ы)", amount=stars_count)]
    )

@bot.pre_checkout_query_handler(func=lambda query: True)
def pre_checkout_query(query):
    bot.answer_pre_checkout_query(query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    bot.reply_to(message, f"🎉 Спасибо большое за поддержку! Получено звезд: {message.successful_payment.total_amount} ⭐")

@bot.message_handler(func=lambda message: True)
def search_music(message):
    user_ids.add(message.from_user.id)
    query = message.text
    msg = bot.reply_to(message, "🔍 Ищу треки в MP3-архиве...")
    
    try:
        # Ищем музыку на открытом mp3-сайте
        url = f"https://ru.hitmotop.com/search?q={quote(query)}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')

        # Берем первые 5 результатов
        tracks = soup.find_all('li', class_='tracks__item')[:5]
        
        if not tracks:
            bot.edit_message_text("❌ Ничего не найдено.", message.chat.id, msg.message_id)
            return

        markup = InlineKeyboardMarkup(row_width=1)
        results = []
        
        for i, track in enumerate(tracks):
            title_elem = track.find('div', class_='track__title')
            desc_elem = track.find('div', class_='track__desc')
            download_elem = track.find('a', class_='track__download-btn')
            
            if title_elem and download_elem:
                title = title_elem.text.strip()
                artist = desc_elem.text.strip() if desc_elem else "Неизвестен"
                mp3_url = download_elem.get('href')
                
                results.append({'url': mp3_url, 'title': title, 'artist': artist})
                
                btn = InlineKeyboardButton(text=f"🎵 {artist} - {title}", callback_data=f"dl_{i}")
                markup.add(btn)
        
        if not hasattr(bot, 'mp3_cache'):
            bot.mp3_cache = {}
        bot.mp3_cache[message.chat.id] = results

        if not results:
             bot.edit_message_text("❌ Не удалось найти ссылки на скачивание.", message.chat.id, msg.message_id)
             return

        bot.edit_message_text("🎧 Выбери трек для скачивания (полная версия):", message.chat.id, msg.message_id, reply_markup=markup)
        
    except Exception as e:
        print(f"Ошибка поиска: {e}")
        bot.edit_message_text("❌ Ошибка при поиске.", message.chat.id, msg.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("dl_"))
def callback_download_track(call):
    index = int(call.data.replace("dl_", ""))
    tracks = getattr(bot, 'mp3_cache', {}).get(call.message.chat.id, [])
    
    if index >= len(tracks):
        bot.answer_callback_query(call.id, "❌ Список устарел, введи запрос заново.")
        return

    track = tracks[index]
    bot.answer_callback_query(call.id, "🎶 Загружаю трек...")
    msg = bot.send_message(call.message.chat.id, "⏳ Скачиваю файл, подожди немного...")
    
    filename = f"track_{call.message.chat.id}.mp3"
    
    try:
        # Скачиваем файл по прямой ссылке
        response = requests.get(track['url'], stream=True, headers={'User-Agent': 'Mozilla/5.0'})
        with open(filename, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                
        # Отправляем готовый MP3 в чат
        with open(filename, 'rb') as audio:
            bot.send_audio(
                call.message.chat.id, 
                audio, 
                title=track['title'], 
                performer=track['artist']
            )
            
        bot.delete_message(call.message.chat.id, msg.message_id)
        if os.path.exists(filename):
            os.remove(filename)
            
    except Exception as e:
        print(f"Ошибка отправки: {e}")
        bot.edit_message_text("❌ Не удалось скачать или отправить трек.", call.message.chat.id, msg.message_id)
        if os.path.exists(filename):
            os.remove(filename)

bot.infinity_polling()
                

