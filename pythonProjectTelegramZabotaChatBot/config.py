import os
from dotenv import load_dotenv

load_dotenv()

# Токен бота из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN', '')

# Настройки базы данных
DATABASE_URL = 'sqlite+aiosqlite:///bot_database.db'

# Минимальный и максимальный возраст
MIN_AGE = 14
MAX_AGE = 18