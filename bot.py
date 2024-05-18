import asyncio
import threading
import nest_asyncio
import telebot
from telebot import types
from netschoolapi import NetSchoolAPI
from netschoolapi.errors import SchoolNotFoundError, AuthError
import httpx
from cryptography.fernet import Fernet, InvalidToken
import sqlite3
from PIL import Image, ImageDraw, ImageFont
import io
import traceback
import os

# Патч для поддержки вложенных циклов asyncio
nest_asyncio.apply()

# Токен Telegram бота
TELEGRAM_TOKEN = 'Ваш_Telegram_Token'

# Создание экземпляра бота
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Установка увеличенных значений тайм-аутов непосредственно через модуль apihelper
import telebot.apihelper as apihelper
apihelper.SESSION_TIMEOUT = 60  # Увеличение тайм-аута сессии
apihelper.READ_TIMEOUT = 60     # Увеличение тайм-аута чтения

# Функция для загрузки или генерации ключа Fernet
def load_or_generate_key():
    if os.path.exists('secret.key'):
        with open('secret.key', 'rb') as key_file:
            key = key_file.read()
    else:
        key = Fernet.generate_key()
        with open('secret.key', 'wb') as key_file:
            key_file.write(key)
    return key

# Загрузка или генерация ключа Fernet
key = load_or_generate_key()
cipher_suite = Fernet(key)

# Настройка базы данных
conn = sqlite3.connect('users.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS users
                  (chat_id INTEGER PRIMARY KEY, school TEXT, login TEXT, password TEXT)''')
conn.commit()

# Временное хранилище для данных пользователя
user_data = {}
user_data_lock = threading.Lock()

# Создание event loop для задач asyncio
loop = asyncio.new_event_loop()

# Запуск event loop asyncio в отдельном потоке
def start_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()

threading.Thread(target=start_loop, daemon=True).start()

# Обработчик команды start
@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    cursor.execute("SELECT school, login, password FROM users WHERE chat_id=?", (chat_id,))
    result = cursor.fetchone()

    if result:
        school, login, encrypted_password = result
        try:
            password = cipher_suite.decrypt(encrypted_password).decode('utf-8')
            asyncio.run_coroutine_threadsafe(initialize_ns(chat_id, school, login, password), loop)
        except InvalidToken:
            bot.send_message(chat_id, 'Ошибка декодирования данных. Пожалуйста, создайте новый аккаунт с помощью команды /new_account.')
    else:
        msg = bot.send_message(chat_id, 'Привет! Я бот для NetSchool. Пожалуйста, введите название вашей школы:')
        bot.register_next_step_handler(msg, get_school)

# Обработчик команды new_account
@bot.message_handler(commands=['new_account'])
def new_account(message):
    chat_id = message.chat.id
    msg = bot.send_message(chat_id, 'Введите название вашей школы:')
    bot.register_next_step_handler(msg, get_school)

# Обработчик для получения названия школы
def get_school(message):
    chat_id = message.chat.id
    with user_data_lock:
        user_data[chat_id] = {'school': message.text}
    msg = bot.send_message(chat_id, 'Введите ваш логин:')
    bot.register_next_step_handler(msg, get_login)

# Обработчик для получения логина
def get_login(message):
    chat_id = message.chat.id
    with user_data_lock:
        user_data[chat_id]['login'] = message.text
    msg = bot.send_message(chat_id, 'Введите ваш пароль:')
    bot.register_next_step_handler(msg, get_password)

# Обработчик для получения пароля и инициализации NetSchool API
def get_password(message):
    chat_id = message.chat.id
    with user_data_lock:
        user_data[chat_id]['password'] = message.text
        school = user_data[chat_id]['school']
        login = user_data[chat_id]['login']
        password = user_data[chat_id]['password']

    encrypted_password = cipher_suite.encrypt(password.encode('utf-8'))
    cursor.execute("INSERT OR REPLACE INTO users (chat_id, school, login, password) VALUES (?, ?, ?, ?)",
                   (chat_id, school, login, encrypted_password))
    conn.commit()

    asyncio.run_coroutine_threadsafe(initialize_ns(chat_id, school, login, password), loop)

# Асинхронная функция для инициализации NetSchool API
async def initialize_ns(chat_id, school, login, password):
    ns = NetSchoolAPI('Ваша_Ссылка_на_сетевойгород')
    
    try:
        await ns.login(login, password, school)
        with user_data_lock:
            if chat_id not in user_data:
                user_data[chat_id] = {}
            user_data[chat_id]['ns'] = ns
        bot.send_message(chat_id, 'Успешный вход! Используйте команды /diary чтобы увидеть дневник')
    except SchoolNotFoundError:
        bot.send_message(chat_id, "Ошибка: школа не найдена.")
    except AuthError:
        bot.send_message(chat_id, "Ошибка: неправильное имя пользователя или пароль.")
    except httpx.ConnectError as e:
        bot.send_message(chat_id, f"Ошибка подключения: {e}")
    except httpx.RequestError as e:
        bot.send_message(chat_id, f"Ошибка запроса: {e}")
    except Exception as e:
        error_message = ''.join(traceback.format_exception(None, e, e.__traceback__))
        bot.send_message(chat_id, f"Произошла ошибка: {error_message}")

# Обработчик команды diary
@bot.message_handler(commands=['diary'])
def diary(message):
    chat_id = message.chat.id
    with user_data_lock:
        ns = user_data.get(chat_id, {}).get('ns')

    if ns is None:
        bot.send_message(chat_id, 'Ошибка подключения к NetSchool API. Сначала войдите с помощью команды /start.')
        return
    
    asyncio.run_coroutine_threadsafe(fetch_diary(chat_id, ns), loop)

# Асинхронная функция для получения данных дневника
async def fetch_diary(chat_id, ns):
    try:
        diary = await ns.diary()

        # Подготовка данных для изображения
        data = []
        for day in diary.schedule:
            day_date = day.day
            for lesson in day.lessons:
                subject = lesson.subject
                assignments = lesson.assignments
                for assignment in assignments:
                    content = assignment.content
                    mark = assignment.mark if assignment.mark else 'Нет оценки'
                    data.append((day_date.strftime('%d.%m.%Y'), subject, content, mark))
            data.append(('-------------------------------------------------------', '', '', ''))

        # Создание изображения
        image = create_diary_image(data)

        # Отправка изображения
        bio = io.BytesIO()
        bio.name = 'diary.png'
        image.save(bio, 'PNG')
        bio.seek(0)
        bot.send_photo(chat_id, photo=bio)

    except Exception as e:
        error_message = ''.join(traceback.format_exception(None, e, e.__traceback__))
        bot.send_message(chat_id, f"Ошибка при получении дневника: {error_message}")

# Функция для создания изображения дневника
def create_diary_image(data):
    # Определение размера изображения и шрифтов
    width = 1200
    height = 60 * (len(data) + 1)  # 60 пикселей на строку, плюс заголовок
    image = Image.new('RGB', (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    
    # Использование шрифта, поддерживающего кириллицу
    try:
        font = ImageFont.truetype("arial.ttf", 24)
    except IOError:
        font = ImageFont.load_default()  # Резервный вариант, если шрифт не найден

    # Рисование заголовка
    header = ("Дата", "Предмет", "Оценка")  # Изменено на три элемента
    draw.text((10, 10), f"{header[0]:<12} {header[1]:<20} {header[2]:<50}", fill=(0, 0, 0), font=font)  # Изменено форматирование

    # Рисование строк
    y_offset = 60
    for row in data:
        draw.text((10, y_offset), f"{row[0]:<12} {row[1]:<20} {row[2]:<50} {row[3]:<10}", fill=(0, 0, 0), font=font)
        y_offset += 60

    return image

# Обновление команд бота
def set_bot_commands():
    commands = [
        telebot.types.BotCommand('/start', 'Начать использование бота'),
        telebot.types.BotCommand('/diary', 'Показать дневник'),
    ]
    bot.set_my_commands(commands)

# Запуск бота
if __name__ == '__main__':
    set_bot_commands()
    bot.polling(none_stop=True)
