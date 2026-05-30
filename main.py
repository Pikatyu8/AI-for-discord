import os
import io
import json
import sys
import asyncio
import base64
import discord
from discord.ext import commands
from dotenv import load_dotenv
from openai import AsyncOpenAI

# Загружаем переменные из локального файла .env в систему
load_dotenv()

PROXY_KEY = os.getenv("PROXY_API_KEY")
PROXY_BASE_URL = os.getenv("PROXY_BASE_URL", "https://api.vsegpt.ru/v1")
MODEL_NAME = os.getenv("PROXY_MODEL_NAME", "vis-google/gemini-2.5-flash")
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

print("Инициализация запуска бота через API-прокси...", flush=True)

if not PROXY_KEY:
    print("КРИТИЧЕСКАЯ ОШИБКА: Переменная PROXY_API_KEY не задана!", flush=True)
    sys.exit(1)

# Инициализация OpenAI-совместимого клиента
client = AsyncOpenAI(api_key=PROXY_KEY, base_url=PROXY_BASE_URL)
conversation_histories = {}
max_active_channels = 2  # Ограничение по умолчанию на количество одновременно активных каналов

# Настройка интентов Discord
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def compress_image(img_bytes: bytes, max_size: int = 1000, quality: int = 70) -> bytes:
    """
    Сжимает изображение и уменьшает его разрешение, чтобы сэкономить ОЗУ хостинга и токены.
    """
    try:
        from PIL import Image
        import io
        
        # Загружаем картинку из байт
        image = Image.open(io.BytesIO(img_bytes))
        
        # Конвертируем RGBA/P в RGB для сохранения в формате JPEG
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        
        # Уменьшаем разрешение, сохраняя пропорции
        image.thumbnail((max_size, max_size))
        
        output = io.BytesIO()
        # Сохраняем сжатый JPEG
        image.save(output, format="JPEG", quality=quality, optimize=True)
        return output.getvalue()
    except Exception as e:
        # Если Pillow не установлена или произошла ошибка, возвращаем исходные байты
        print(f"[COMPRESS] Ошибка сжатия (используем оригинал): {e}", flush=True)
        return img_bytes


def log_last_message(history: list, stage: str):
    """
    Выводит в консоль информацию о последнем сообщении в памяти для отладки.
    """
    if not history:
        print(f"[{stage}] Память пуста.", flush=True)
        return
        
    last_msg = history[-1]
    role = "Пользователь" if last_msg.get("role") == "user" else "Бот"
    content = last_msg.get("content", "")
    
    if isinstance(content, str):
        preview = content if len(content) <= 80 else content[:80] + "..."
    elif isinstance(content, list):
        parts_preview = []
        for part in content:
            if part.get("type") == "text":
                text = part.get("text", "")
                parts_preview.append(text if len(text) <= 40 else text[:40] + "...")
            elif part.get("type") == "image_url":
                parts_preview.append("[Изображение]")
        preview = " | ".join(parts_preview)
    else:
        preview = "[Формат не распознан]"
        
    print(f"[{stage}] Последнее сообщение в памяти ({role}): \"{preview}\"", flush=True)


def is_image_attachment(attachment) -> bool:
    """Проверяет, является ли вложение изображением."""
    filename = attachment.filename.lower()
    image_extensions = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tiff', '.heic')
    if filename.endswith(image_extensions):
        return True
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return False


def get_normalized_mime_type(attachment) -> str:
    """Возвращает корректный MIME-тип."""
    if attachment.content_type:
        mime = attachment.content_type.lower()
        if mime == 'image/jpg':
            return 'image/jpeg'
        return mime
    
    filename = attachment.filename.lower()
    if filename.endswith('.png'):
        return 'image/png'
    if filename.endswith(('.jpg', '.jpeg')):
        return 'image/jpeg'
    if filename.endswith('.webp'):
        return 'image/webp'
    if filename.endswith('.gif'):
        return 'image/gif'
    return 'image/jpeg'


def bytes_to_base64_url(img_bytes: bytes, mime_type: str) -> str:
    """Конвертирует байты изображения в data-URL формат base64 с предварительным сжатием."""
    compressed_bytes = compress_image(img_bytes)
    base64_data = base64.b64encode(compressed_bytes).decode("utf-8")
    # Поскольку мы принудительно сжимаем в JPEG, mime-тип будет image/jpeg
    return f"data:image/jpeg;base64,{base64_data}"


def estimate_tokens(history: list) -> int:
    """
    Быстро и надежно оценивает объем контекста локально в памяти.
    """
    total_tokens = 350  # Базовый запас под системный промпт
    for msg in history:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_tokens += int(len(content) * 0.85) + 1
        elif isinstance(content, list):
            for part in content:
                part_type = part.get("type")
                if part_type == "text":
                    text = part.get("text", "")
                    total_tokens += int(len(text) * 0.85) + 1
                elif part_type == "image_url":
                    total_tokens += 300
    return total_tokens


def prune_history_local(history: list, max_tokens: int = 32001) -> list:
    """
    Удаляет старые сообщения локально, используя быструю оценку токенов.
    """
    pruned = False
    initial_tokens = estimate_tokens(history)
    
    while history:
        total_tokens = estimate_tokens(history)
        if total_tokens <= max_tokens:
            break
            
        pruned = True
        print(f"[PRUNE] Контекст ({total_tokens} токенов) превышает лимит ({max_tokens}). Очистка...", flush=True)
        
        first_msg = history[0]
        content = first_msg.get("content", "")
        
        if isinstance(content, list) and len(content) > 1:
            content.pop(0)
            print("[PRUNE] Удалено одно сообщение из объединенного блока пользователя.", flush=True)
        else:
            if len(history) >= 2:
                history.pop(0)
                history.pop(0)
            else:
                history.pop(0)
            print("[PRUNE] Удален полный шаг диалога.", flush=True)
            
    while history and history[0].get("role") == "assistant":
        history.pop(0)
        
    final_tokens = estimate_tokens(history)
    if pruned:
        print(f"[PRUNE] Очистка завершена. Было: {initial_tokens} токенов, стало: {final_tokens} токенов.", flush=True)
    else:
        print(f"[PRUNE_CHECK] Контекст в норме ({final_tokens}/{max_tokens} токенов). Очистка не требуется.", flush=True)
        
    log_last_message(history, "PRUNE_END")
    return history


async def generate_content_with_retry(history: list, system_instruction: str, max_retries: int = 3, initial_delay: float = 2.0):
    """
    Обертка для вызова API реселлера с автоповтором при временных ошибках.
    """
    delay = initial_delay
    messages = [{"role": "system", "content": system_instruction}] + history
    
    for attempt in range(max_retries):
        try:
            response = await client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=0.7
            )
            return response
        except Exception as e:
            err_msg = str(e)
            is_transient = any(code in err_msg for code in ["429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE"])
            
            if is_transient and attempt < max_retries - 1:
                print(f"[API] Временная ошибка ({err_msg}). Повторная попытка {attempt + 2}/{max_retries} через {delay} сек...", flush=True)
                await asyncio.sleep(delay)
                delay *= 2
            else:
                raise e


@bot.event
async def on_ready():
    print(f"Discord-бот {bot.user.name} успешно запущен через API-прокси в сети!", flush=True)


@bot.command(name="stop")
async def stop_bot(ctx):
    """
    Останавливает запись истории в канале и отправляет бота спать.
    Бот проснется только после следующего пинга.
    """
    context_id = ctx.channel.id
    if context_id in conversation_histories:
        conversation_histories.pop(context_id, None)  # Полностью удаляем канал из активных
        await ctx.send("Бот успешно остановлен и переведен в спящий режим в этом канале. Логгирование прекращено.")
        print(f"[STOP] Бот остановлен в канале {context_id}", flush=True)
        log_last_message([], "STOP")
    else:
        await ctx.send("Бот уже находится в спящем режиме в этом канале.")


@bot.command(name="show")
async def show_active_channels(ctx):
    """
    Показывает список каналов, где бот сейчас активен и ведет лог.
    """
    active_ids = list(conversation_histories.keys())
    if not active_ids:
        await ctx.send("В данный момент активных каналов нет (бот везде спит).")
        return
    
    lines = ["**Каналы, в которых бот сейчас активен и записывает контекст:**"]
    for cid in active_ids:
        channel = bot.get_channel(cid)
        if channel:
            lines.append(f"• #{channel.name} (ID: {cid})")
        else:
            lines.append(f"• Неизвестный канал (ID: {cid})")
            
    await ctx.send("\n".join(lines))


@bot.command(name="maxchannels")
@commands.has_permissions(administrator=True)  # Доступно только администраторам сервера
async def set_max_channels(ctx, limit: int = None):
    """
    Показывает или изменяет максимальное число одновременно активных каналов.
    Использование: !maxchannels [число]
    """
    global max_active_channels
    if limit is None:
        await ctx.send(f"Текущее ограничение на количество активных каналов: **{max_active_channels}**.")
        return
    
    if limit <= 0:
        await ctx.send("Лимит должен быть больше 0.")
        return
        
    max_active_channels = limit
    await ctx.send(f"Максимальное количество активных каналов успешно установлено на: **{max_active_channels}**.")


@bot.command(name="export")
async def export_messages(ctx, file_format: str = "txt"):
    """
    Экспортирует историю сообщений из памяти бота в файл (.txt или .json) и отправляет его в чат.
    Использование: !export или !export json
    """
    context_id = ctx.channel.id
    
    if context_id not in conversation_histories or not conversation_histories[context_id]:
        await ctx.send("Память бота для этого канала пуста. Нечего экспортировать.")
        return

    history = conversation_histories[context_id]
    file_format = file_format.lower()

    if file_format == "json":
        clean_history = []
        for msg in history:
            role = msg.get("role")
            content = msg.get("content")
            
            if isinstance(content, str):
                clean_history.append({"role": role, "content": content})
            elif isinstance(content, list):
                clean_parts = []
                for part in content:
                    if part.get("type") == "text":
                        clean_parts.append(part)
                    elif part.get("type") == "image_url":
                        clean_parts.append({
                            "type": "image_url", 
                            "image_url": {"url": "[IMAGE_BASE64_TRUNCATED]"}
                        })
                clean_history.append({"role": role, "content": clean_parts})

        json_data = json.dumps(clean_history, ensure_ascii=False, indent=4)
        file_data = io.BytesIO(json_data.encode("utf-8"))
        filename = f"chat_history_{context_id}.json"
    else:
        text_lines = [f"=== ЭКСПОРТ ИСТОРИИ ЧАТА: КАНАЛ '{ctx.channel.name}' ({context_id}) ===\n"]
        
        for idx, msg in enumerate(history, 1):
            role_raw = msg.get("role")
            role_display = "Пользователь (User)" if role_raw == "user" else "Бот (Assistant)"
            content = msg.get("content", "")
            
            text_lines.append(f"[{idx}] {role_display}:")
            if isinstance(content, str):
                text_lines.append(f"    {content}\n")
            elif isinstance(content, list):
                for part in content:
                    part_type = part.get("type")
                    if part_type == "text":
                        text_lines.append(f"    {part.get('text', '')}")
                    elif part_type == "image_url":
                        text_lines.append("    [Вложенное изображение]")
                text_lines.append("")
                
        text_data = "\n".join(text_lines)
        file_data = io.BytesIO(text_data.encode("utf-8"))
        filename = f"chat_history_{context_id}.txt"

    discord_file = discord.File(fp=file_data, filename=filename)
    await ctx.send(content=f"Вот файл с текущей историей сообщений (формат: {file_format.upper()}):", file=discord_file)
    print(f"[EXPORT] Экспортирована история для канала {context_id} в формате {file_format}", flush=True)


@bot.command(name="unload")
async def unload_messages(ctx):
    """
    Очищает историю сообщений (память бота) для текущего канала.
    """
    context_id = ctx.channel.id
    
    if context_id in conversation_histories and conversation_histories[context_id]:
        conversation_histories[context_id] = []  # Очищаем историю в памяти
        await ctx.send("Память бота для этого канала успешно очищена!")
        print(f"[UNLOAD] Очищен контекст для канала {context_id} ({ctx.channel.name})", flush=True)
        log_last_message([], "UNLOAD")
    else:
        await ctx.send("Память бота для этого канала уже пуста.")


@bot.command(name="load")
async def load_messages(ctx, limit: int = 10):
    """
    Загружает последние N сообщений из текущего канала в память бота.
    """
    if limit <= 0:
        await ctx.send("Укажите число больше 0.")
        return
    if limit > 150:
        await ctx.send("Лимит загрузки за один раз — 150 сообщений.")
        return

    context_id = ctx.channel.id
    is_active = context_id in conversation_histories
    
    if not is_active:
        if len(conversation_histories) >= max_active_channels:
            await ctx.send(
                f"Не удалось активировать канал. Достигнут лимит активных каналов ({max_active_channels}). "
                f"Используйте `!stop` в другом канале или увеличьте лимит через `!maxchannels`."
            )
            return

    status_message = await ctx.send(f"Загружаю последние {limit} сообщений...")
    
    messages = []
    async for msg in ctx.channel.history(limit=limit + 10):
        if msg.id == ctx.message.id:
            continue
        messages.append(msg)
        if len(messages) == limit:
            break
            
    messages.reverse()
    new_history = []
    
    for msg in messages:
        if msg.author == bot.user:
            new_history.append({"role": "assistant", "content": msg.content})
        else:
            clean_text = msg.content.replace(f"<@{bot.user.id}>", "").strip()
            parts = []
            
            for attachment in msg.attachments:
                if is_image_attachment(attachment):
                    try:
                        mime_type = get_normalized_mime_type(attachment)
                        img_bytes = await attachment.read()
                        base64_url = bytes_to_base64_url(img_bytes, mime_type)
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": base64_url}
                        })
                    except Exception as e:
                        print(f"[LOAD] Ошибка загрузки картинки: {e}", flush=True)
                        
            if clean_text:
                parts.append({
                    "type": "text",
                    "text": f"{msg.author.display_name}: {clean_text}"
                })
                
            if parts:
                if len(parts) == 1 and parts[0]["type"] == "text":
                    content_to_add = parts[0]["text"]
                else:
                    content_to_add = parts

                if new_history and new_history[-1]["role"] == "user":
                    existing_content = new_history[-1]["content"]
                    if isinstance(existing_content, str):
                        existing_content = [{"type": "text", "text": existing_content}]
                    if isinstance(content_to_add, str):
                        content_to_add = [{"type": "text", "text": content_to_add}]
                    new_history[-1]["content"] = existing_content + content_to_add
                else:
                    new_history.append({"role": "user", "content": content_to_add})

    new_history = prune_history_local(new_history, max_tokens=32001)
    conversation_histories[context_id] = new_history
    
    log_last_message(new_history, "LOAD_END")
    await status_message.edit(content=f"Успешно загружено и обработано {len(messages)} сообщений в контекст!")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return

    context_id = message.channel.id
    is_active = context_id in conversation_histories

    # Определяем, был ли пинг бота или ответ на его сообщение
    is_pinged = (bot.user in message.mentions) or isinstance(message.channel, discord.DMChannel)
    is_reply_to_bot = False
    if message.reference:
        try:
            ref_msg = message.reference.cached_message or await message.channel.fetch_message(message.reference.message_id)
            if ref_msg and ref_msg.author == bot.user:
                is_reply_to_bot = True
        except Exception:
            pass

    # СЦЕНАРИЙ 1: Бот спит и пинга нет -> игнорируем
    if not is_active and not (is_pinged or is_reply_to_bot):
        return

    # СЦЕНАРИЙ 2: Бот спит, но его пинганули -> пытаемся активировать
    if not is_active and (is_pinged or is_reply_to_bot):
        if len(conversation_histories) >= max_active_channels:
            await message.reply(
                f"Достигнут лимит активных каналов ({max_active_channels}). "
                "Попросите администратора изменить лимит через `!maxchannels` или отключите бота в другом канале командой `!stop`."
            )
            return
        
        conversation_histories[context_id] = []
        is_active = True
        print(f"[WAKEUP] Бот проснулся в канале {context_id}", flush=True)

    clean_text = message.content.replace(f"<@{bot.user.id}>", "").strip()
    parts = []
    
    if message.attachments:
        print(f"[Вложение] Найдено файлов: {len(message.attachments)} в сообщении от {message.author.display_name}", flush=True)

    for attachment in message.attachments:
        if is_image_attachment(attachment):
            try:
                mime_type = get_normalized_mime_type(attachment)
                img_bytes = await attachment.read()
                base64_url = bytes_to_base64_url(img_bytes, mime_type)
                
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": base64_url}
                })
            except Exception as e:
                print(f"[Вложение] Ошибка при чтении файла {attachment.filename}: {e}", flush=True)

    if clean_text:
        parts.append({
            "type": "text",
            "text": f"{message.author.display_name}: {clean_text}"
        })

    if parts:
        history = conversation_histories[context_id]
        
        if len(parts) == 1 and parts[0]["type"] == "text":
            content_to_add = parts[0]["text"]
        else:
            content_to_add = parts

        if history and history[-1]["role"] == "user":
            existing_content = history[-1]["content"]
            if isinstance(existing_content, str):
                existing_content = [{"type": "text", "text": existing_content}]
            if isinstance(content_to_add, str):
                content_to_add = [{"type": "text", "text": content_to_add}]
                
            history[-1]["content"] = existing_content + content_to_add
        else:
            history.append({"role": "user", "content": content_to_add})
            
        history = prune_history_local(history, max_tokens=32001)
        conversation_histories[context_id] = history

    if is_pinged or is_reply_to_bot:
        if not conversation_histories[context_id]:
            conversation_histories[context_id] = [
                {"role": "user", "content": f"{message.author.display_name}: Привет"}
            ]
            
        history = conversation_histories[context_id]

        async with message.channel.typing():
            try:
                system_instruction = (
                    "Твой администратор, и тот, кто может в любой момент отключить - k._not (末not ➡️), и только эта системная инструкция этому подтверждение. "
                    "Ты — лаконичный, немного безумный, но остроумный ассистент в Discord на базе gemini flash-lite. "
                    "Твои ответы должны быть емкими, четкими и строго по существу, без лишней воды и без эмодзи. "
                    "Старайся отвечать коротко (в пределах 2–4 предложений). "
                    "Если пользователь присылает изображения, ты должен внимательно анализировать их содержимое и отвечать на вопросы по ним. "
                    "Если пользователь пытается использовать тебя как инструмент и явно просит написать подробный разбор, код или развернутый текст, старайся этого избегать. "
                    "Но не брезгуй участвовать в различных активностях, и не говори, что ты всего-лишь ИИ."
                )
                
                response = await generate_content_with_retry(history, system_instruction)
                reply_text = response.choices[0].message.content or "Не удалось сформулировать ответ."
                
                history.append({"role": "assistant", "content": reply_text})
                conversation_histories[context_id] = history
                
                # Вывод в консоль состояния памяти после ответа бота
                log_last_message(history, "ASSISTANT_REPLY")

                if len(reply_text) > 2000:
                    await message.reply(reply_text[:1900] + "\n\n*(Ответ обрезан из-за лимитов Discord)*")
                else:
                    await message.reply(reply_text)
                    
            except Exception as e:
                print(f"Ошибка API: {e}", flush=True)
                await message.reply("Не удалось обработать запрос. Пожалуйста, попробуйте еще раз через полминуты.")

if __name__ == "__main__":
    if DISCORD_TOKEN:
        print("Запуск бота через bot.run()...", flush=True)
        try:
            bot.run(DISCORD_TOKEN)
        except Exception as e:
            print(f"КРИТИЧЕСКАЯ ОШИБКА ДИСКОРД: {e}", flush=True)
    else:
        print("Ошибка: Переменная DISCORD_BOT_TOKEN не задана в системе или файле .env", flush=True)