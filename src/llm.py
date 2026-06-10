# src/llm.py
import asyncio
import hashlib
from openai import AsyncOpenAI
from src.config import PROXY_KEY, PROXY_BASE_URL, MODEL_NAME

client = AsyncOpenAI(api_key=PROXY_KEY, base_url=PROXY_BASE_URL)

# Каскадный список моделей для генерации описаний (в порядке приоритета).
# Если первая модель в списке недоступна, бот автоматически пробует следующую.
CAPTION_MODELS_FALLBACK = [
    "google/gemma-4-31b-it:free",                  # Gemma 4 (бесплатная мультимодальная модель)
    "meta-llama/llama-3.2-11b-vision-instruct",   # Платная Llama 3.2 (очень дешевый и стабильный резерв)
    "openrouter/free",                             # Авто-роутер спускаем в самый конец списка
]

# Локальный кэш описаний изображений
IMAGE_DESCRIPTION_CACHE = {}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск актуальной информации в интернете ИСКЛЮЧИТЕЛЬНО через Google Search. Поддерживает одновременный параллельный поиск по нескольким разным запросам (до 5 штук). Если после первого поиска информации всё ещё недостаточно для полного и точного ответа, ты должен совершить новый уточняющий поиск на следующем шаге (допускается до 3 последовательных шагов поиска).",
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        },
                        "description": "Список поисковых запросов для параллельного поиска (например: ['погода в Москве', 'курс доллара сегодня']). Используй для комплексных тем. Максимум 5 запросов."
                    },
                    "query": {
                        "type": "string",
                        "description": "Одиночный поисковый запрос (используй, если нужен только один конкретный запрос)."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "Сохранить важную информацию, заметку или воспоминание в текстовый файл memories.txt. Используй этот инструмент, когда пользователь просит тебя что-то запомнить, записать или сохранить.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Текст заметки или воспоминания для сохранения"
                    }
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_notes",
            "description": "Прочитать все ранее сохраненные заметки и воспоминания из файла memories.txt. Используй этот инструмент, когда пользователь спрашивает о том, что ты помнишь, или просит показать сохраненные заметки.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]


def model_supports_vision(model_name: str) -> bool:
    """
    Определяет, поддерживает ли модель изображения на основе её названия.
    """
    model_lower = model_name.lower()
    
    vision_keywords = [
        "vision", "vl", "multimodal", "gemma-4-31b", "gemini", 
        "gpt-4o", "gpt-4-turbo", "claude-3", "pixtral", "llama-3.2-11b", "llama-3.2-90b"
    ]
    
    non_vision_models = ["nemotron-3-super", "llama-3.1-405b", "qwen3-235b"]
    
    if any(non_v in model_lower for non_v in non_vision_models):
        return False
        
    return any(keyword in model_lower for keyword in vision_keywords)


def get_image_hash(base64_url: str) -> str:
    """Генерирует MD5-хэш для base64-строки изображения для кэширования."""
    return hashlib.md5(base64_url.encode("utf-8")).hexdigest()


async def describe_image_async(base64_url: str, message_id: int = None, channel_id: int = None, bot=None) -> str:
    """
    Асинхронно запрашивает подробное описание изображения, проходя по каскадному списку моделей.
    Реакции ставятся непосредственно на исходное сообщение, содержащее картинку.
    """
    img_hash = get_image_hash(base64_url)
    
    discord_message = None
    bot_user = None
    guild_id = None
    if bot and message_id and channel_id:
        try:
            channel = bot.get_channel(channel_id)
            if not channel:
                channel = await bot.fetch_channel(channel_id)
            if channel:
                discord_message = await channel.fetch_message(message_id)
                guild_id = channel.guild.id if getattr(channel, "guild", None) else None
            bot_user = bot.user
        except Exception as e:
            print(f"[VISION] Ошибка получения сообщения {message_id} для установки реакций: {e}", flush=True)

    async def safe_add_reaction(emoji: str):
        if discord_message:
            try:
                await discord_message.add_reaction(emoji)
            except Exception as e:
                print(f"[REACTION] Не удалось добавить реакцию {emoji}: {e}", flush=True)

    async def safe_remove_reaction(emoji: str):
        if discord_message:
            try:
                target = bot_user or (discord_message.guild.me if getattr(discord_message, "guild", None) else None)
                if target:
                    await discord_message.remove_reaction(emoji, target)
                else:
                    me = getattr(discord_message.channel, "me", None)
                    if me:
                        await discord_message.remove_reaction(emoji, me)
            except Exception as e:
                print(f"[REACTION] Не удалось удалить реакцию {emoji}: {e}", flush=True)

    # Проверяем наличие описания в локальном кэше
    if img_hash in IMAGE_DESCRIPTION_CACHE:
        print(f"[VISION_CACHE] Возвращено описание из кэша (Хэш: {img_hash[:8]})", flush=True)
        await safe_add_reaction("✅")
        cached_val = IMAGE_DESCRIPTION_CACHE[img_hash]
        if isinstance(cached_val, dict):
            # Обновляем метаданные сообщения, если они ранее отсутствовали
            if message_id and not cached_val.get("message_id"):
                cached_val["message_id"] = message_id
                cached_val["channel_id"] = channel_id
                cached_val["guild_id"] = guild_id
            return cached_val["description"]
        return cached_val

    # Описания нет в кэше — начинаем генерацию
    await safe_add_reaction("🔄")

    prompt = (
        "Пожалуйста, подробно опиши это изображение для другой языковой модели, которая не умеет видеть. "
        "Перечисли все ключевые объекты, цвета, действия, эмоции людей, текст (если он есть на картинке, обязательно распознай его и выведи), "
        "а также общий контекст происходящего. Ответ напиши на русском языке."
    )
    
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": base64_url}}
            ]
        }
    ]
    
    # Последовательно пробуем модели из списка приоритетов
    for model in CAPTION_MODELS_FALLBACK:
        print(f"[VISION] Пробуем получить описание через модель {model}...", flush=True)
        
        for attempt in range(2):  # Делаем по 2 попытки на каждую модель перед переходом к следующей
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=1000
                )
                description = response.choices[0].message.content or ""
                description = description.strip()
                
                if description:
                    # Кэшируем результат со структурированными метаданными
                    IMAGE_DESCRIPTION_CACHE[img_hash] = {
                        "description": description,
                        "message_id": message_id,
                        "channel_id": channel_id,
                        "guild_id": guild_id
                    }
                    print(f"[VISION] Описание успешно получено через {model} и сохранено в кэш (Хэш: {img_hash[:8]})", flush=True)
                    
                    await safe_remove_reaction("🔄")
                    await safe_add_reaction("✅")
                    return description
                    
            except Exception as e:
                print(f"[VISION_ERR] Модель {model} (попытка {attempt+1}/2) вернула ошибку: {e}", flush=True)
                if attempt < 1:
                    await asyncio.sleep(1.0)
            
    await safe_remove_reaction("🔄")
    return "[Изображение (не удалось получить описание: все модели-описатели временно недоступны)]"


async def convert_vision_messages_to_text(messages: list, bot=None) -> list:
    """
    Преобразует мультимодальные сообщения в чисто текстовые.
    Извлекает сохраненные ID сообщения/канала, чтобы реакции ставились на посты с картинками.
    Не блокирует выполнение основного запроса, если описание загружается дольше 5 секунд.
    """
    cleaned = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        
        cleaned_msg = {"role": role}
        for key in ["tool_calls", "name", "tool_call_id"]:
            if key in msg:
                cleaned_msg[key] = msg[key]
                
        if isinstance(content, list):
            text_parts = []
            image_tasks_info = []
            
            for part in content:
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    url_obj = part.get("image_url", {})
                    url = url_obj.get("url", "")
                    m_id = url_obj.get("message_id")
                    c_id = url_obj.get("channel_id")
                    if url:
                        # Запускаем фоновую задачу описания картинок с сохранением связи с сообщением
                        task = asyncio.create_task(describe_image_async(
                            url, 
                            message_id=m_id, 
                            channel_id=c_id, 
                            bot=bot
                        ))
                        image_tasks_info.append(task)
            
            if image_tasks_info:
                print(f"[VISION] Ожидание генерации описаний до 5.0 сек...", flush=True)
                done, pending = await asyncio.wait(set(image_tasks_info), timeout=5.0)
                
                for idx, task in enumerate(image_tasks_info):
                    if task in done:
                        try:
                            desc = task.result()
                        except Exception as e:
                            desc = f"[Изображение (ошибка обработки: {e})]"
                    else:
                        desc = "[Изображение (описание загружается в фоновом режиме...)]"
                        print(f"[VISION] Картинка #{idx+1} обрабатывается слишком долго, продолжаем в фоне.", flush=True)
                        
                    text_parts.append(f"\n[Контекст прикрепленного изображения:\n{desc}\n]")
            
            cleaned_msg["content"] = "\n".join(text_parts).strip()
        else:
            cleaned_msg["content"] = content
            
        cleaned.append(cleaned_msg)
    return cleaned


async def generate_content_with_retry(
    history: list, 
    system_instruction: str, 
    max_retries: int = 3, 
    initial_delay: float = 2.0, 
    tools=None, 
    thinking_enabled: bool = False,
    bot=None
):
    """
    Обертка для вызова API реселлера с автоповтором при временных ошибках.
    """
    delay = initial_delay
    
    # Проверяем, поддерживает ли текущая модель зрение
    supports_vision = model_supports_vision(MODEL_NAME)
    
    # Формируем базовый список сообщений
    raw_messages = [{"role": "system", "content": system_instruction}] + history
    
    # Если модель не поддерживает зрение, преобразуем историю
    if not supports_vision:
        messages = await convert_vision_messages_to_text(raw_messages, bot=bot)
    else:
        messages = raw_messages
    
    # Список сигнатур моделей, поддерживающих Reasoning на OpenRouter
    REASONING_MODELS = ["deepseek-r1", "nemotron-3-super", "gemma-4-31b", "thinking"]
    is_reasoning_model = any(m in MODEL_NAME.lower() for m in REASONING_MODELS)
    
    for attempt in range(max_retries):
        try:
            kwargs = {
                "model": MODEL_NAME,
                "messages": messages,
                "temperature": 0.7
            }
            if tools:
                kwargs["tools"] = tools
                
            # Управление нативными рассуждениями
            if is_reasoning_model:
                kwargs["extra_body"] = {
                    "reasoning": {
                        "exclude": not thinking_enabled
                    }
                }
                
            response = await client.chat.completions.create(**kwargs)
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
