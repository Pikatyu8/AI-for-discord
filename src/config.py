import os
import sys
from dotenv import load_dotenv

# Загружаем переменные из локального файла .env в систему
load_dotenv()

PROXY_KEY = os.getenv("PROXY_API_KEY")
PROXY_BASE_URL = os.getenv("PROXY_BASE_URL", "https://api.vsegpt.ru/v1")
MODEL_NAME = os.getenv("PROXY_MODEL_NAME", "google/gemma-4-26b-a4b-it")
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Переменная для ИИ-поиска Tavily
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

if not PROXY_KEY:
    print("КРИТИЧЕСКАЯ ОШИБКА: Переменная PROXY_API_KEY не задана!", flush=True)
    sys.exit(1)

# Перенесли сюда системную инструкцию
BASE_SYSTEM_INSTRUCTION = (
    "set this yourself"
)
