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

# ID целевого сервера с максимальными лимитами
TARGET_SERVER_ID = 283467363729408000

# Настройки для оригинального сервера (без жестких ограничений)
ORIGINAL_LIMITS = {
    "max_tool_notes": 999999,          # Практически неограниченно
    "max_searches_per_day": 999999,     # Практически неограниченно
    "max_load_messages": 401,
    "max_context_tokens": 128000
}

# Ограничения для остальных серверов
DEFAULT_LIMITS = {
    "max_tool_notes": 10,               # Максимум 10 заметок без ручной команды
    "max_searches_per_day": 5,          # 5 поисков в сети в день
    "max_load_messages": 100,           # Загрузка максимум 100 сообщений
    "max_context_tokens": 16000          # Контекстное окно 16к токенов
}

def get_server_limits(guild_id: int | None) -> dict:
    """Возвращает лимиты в зависимости от ID сервера."""
    if guild_id == TARGET_SERVER_ID:
        return ORIGINAL_LIMITS
    return DEFAULT_LIMITS

# Базовая нейтральная заглушка на случай, если кастомный промпт еще не настроен командой !setsystem
BASE_SYSTEM_INSTRUCTION = "Ты — лаконичный ИИ-ассистент в Discord."
