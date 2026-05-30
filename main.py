import sys
from src.config import DISCORD_TOKEN
from src.bot import bot

if __name__ == "__main__":
    print("Инициализация запуска бота через API-прокси...", flush=True)
    if DISCORD_TOKEN:
        print("Запуск бота через bot.run()...", flush=True)
        try:
            bot.run(DISCORD_TOKEN)
        except Exception as e:
            print(f"КРИТИЧЕСКАЯ ОШИБКА ДИСКОРД: {e}", flush=True)
    else:
        print("Ошибка: Переменная DISCORD_BOT_TOKEN не задана в системе или файле .env", flush=True)
        sys.exit(1)