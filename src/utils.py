import io
import base64
import httpx
import re
import os
import json
from datetime import datetime

MEMORIES_FILE = "src/memories.json"
SEARCH_LIMITS_FILE = "src/search_limits.json"
CHAT_HISTORY_FILE = "src/chat.txt"

def compress_image(img_bytes: bytes, max_size: int = 1000, quality: int = 70) -> bytes:
    """
    Сжимает изображение и уменьшает его разрешение, чтобы сэкономить ОЗУ хостинга и токены.
    """
    try:
        from PIL import Image
        
        image = Image.open(io.BytesIO(img_bytes))
        
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        
        image.thumbnail((max_size, max_size))
        
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        return output.getvalue()
    except Exception as e:
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
    content = last_msg.get("content") or ""
    
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


def is_text_or_pdf_attachment(attachment) -> bool:
    """Проверяет, является ли вложение текстовым документом (.txt, .json, .log, .py) или PDF."""
    filename = attachment.filename.lower()
    text_extensions = ('.txt', '.json', '.log', '.py', '.pdf')
    if filename.endswith(text_extensions):
        return True
    if attachment.content_type:
        mime = attachment.content_type.lower()
        if any(t in mime for t in ["text/", "json", "pdf"]):
            return True
    return False


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    Извлекает текстовый контент из PDF-документа с помощью библиотеки pypdf.
    """
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text_parts = []
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
        return "\n".join(text_parts).strip()
    except ImportError:
        print("[PDF] Ошибка: библиотека 'pypdf' не установлена. Используйте `pip install pypdf`.", flush=True)
        return "[Ошибка: на сервере не установлена библиотека pypdf для парсинга PDF-файлов. Пожалуйста, выполните 'pip install pypdf']"
    except Exception as e:
        print(f"[PDF_ERR] Ошибка при извлечении текста из PDF: {e}", flush=True)
        return f"[Ошибка при чтении PDF-файла: {e}]"


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
    return f"data:image/jpeg;base64,{base64_data}"


IMAGE_URL_REGEX = re.compile(
    r'https?://[^\s/$.?#].[^\s]*\.(?:png|jpg|jpeg|webp|gif|bmp)(?:\?[^\s]*)?', 
    re.IGNORECASE
)


def extract_image_urls(message) -> list:
    """
    Извлекает уникальные ссылки на изображения как из текста сообщения, так и из эмбедов.
    """
    urls = []
    if message.content:
        found_urls = IMAGE_URL_REGEX.findall(message.content)
        for url in found_urls:
            if url not in urls:
                urls.append(url)
                
    if message.embeds:
        for embed in message.embeds:
            if embed.image and embed.image.url:
                if embed.image.url not in urls:
                    urls.append(embed.image.url)
            elif embed.thumbnail and embed.thumbnail.url:
                if embed.thumbnail.url not in urls:
                    urls.append(embed.thumbnail.url)
                    
    return urls


def extract_embeds_text(message) -> str:
    """
    Собирает текстовый контент из эмбедов сообщения (заголовки, описания, поля, авторы).
    """
    if not message.embeds:
        return ""
        
    embed_texts = []
    for idx, embed in enumerate(message.embeds, 1):
        parts = []
        if embed.author and embed.author.name:
            parts.append(f"Автор: {embed.author.name}")
        if embed.title:
            parts.append(f"Заголовок: {embed.title}")
        if embed.description:
            parts.append(f"Описание: {embed.description}")
            
        for field in embed.fields:
            if field.name and field.value:
                parts.append(f"{field.name}: {field.value}")
                
        if embed.footer and embed.footer.text:
            parts.append(f"Подвал: {embed.footer.text}")
            
        if parts:
            embed_texts.append(f"--- Информация из прикреплённой ссылки #{idx} ---\n" + "\n".join(parts))
            
    return "\n\n".join(embed_texts)


async def fetch_image_as_base64(url: str) -> str:
    """
    Асинхронно скачивает изображение по ссылке, сжимает его и кодирует в Base64.
    """
    try:
        async with httpx.AsyncClient() as client:
            try:
                head_resp = await client.head(url, timeout=5.0, follow_redirects=True)
                if head_resp.status_code == 200:
                    content_type = head_resp.headers.get("content-type", "").lower()
                    if "image" not in content_type:
                        return None
                    
                    content_length = head_resp.headers.get("content-length")
                    if content_length and int(content_length) > 20 * 1024 * 1024:
                        return None
            except Exception:
                pass

            response = await client.get(url, timeout=10.0, follow_redirects=True)
            if response.status_code == 200:
                real_content_type = response.headers.get("content-type", "").lower()
                if "image" not in real_content_type:
                    return None
                    
                mime_type = "image/jpeg"
                if "png" in real_content_type:
                    mime_type = "image/png"
                elif "webp" in real_content_type:
                    mime_type = "image/webp"
                elif "gif" in real_content_type:
                    mime_type = "image/gif"
                
                img_bytes = response.content
                return bytes_to_base64_url(img_bytes, mime_type)
    except Exception as e:
        print(f"[FETCH_IMAGE] Ошибка скачивания изображения по ссылке {url}: {e}", flush=True)
    return None


def estimate_tokens(history: list) -> int:
    """
    Быстро и надежно оценивает объем контекста локально в памяти.
    """
    total_tokens = 350
    for msg in history:
        content = msg.get("content") or ""
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
                    
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                args = func.get("arguments", "")
                total_tokens += int((len(name) + len(args)) * 0.85) + 20
    return total_tokens


def prune_history_local(history: list, max_tokens: int = 128000) -> list:
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
        content = first_msg.get("content") or ""
        
        if isinstance(content, list) and len(content) > 1:
            content.pop(0)
        else:
            if len(history) >= 2:
                history.pop(0)
                history.pop(0)
            else:
                history.pop(0)
            
    while history and history[0].get("role") in ["assistant", "tool"]:
        history.pop(0)
        
    final_tokens = estimate_tokens(history)
    if pruned:
        print(f"[PRUNE] Очистка завершена. Было: {initial_tokens} токенов, стало: {final_tokens} токенов.", flush=True)
        
    log_last_message(history, "PRUNE_END")
    return history


def extract_and_strip_thoughts(content: str) -> tuple[str, str | None]:
    """
    Извлекает размышления из тегов <think>...</think>, возвращая очищенный текст и сами размышления.
    """
    if not content:
        return "", None
        
    thinking = None
    match = re.search(r'<(think|thought)>(.*?)</\1>', content, re.DOTALL | re.IGNORECASE)
    if match:
        thinking = match.group(2).strip()
        clean_content = re.sub(r'<(think|thought)>.*?</\1>', '', content, flags=re.DOTALL | re.IGNORECASE).strip()
    else:
        open_match = re.search(r'<(think|thought)>(.*)', content, re.DOTALL | re.IGNORECASE)
        if open_match:
            thinking = open_match.group(2).strip()
            clean_content = re.sub(r'<(think|thought)>.*', '', content, flags=re.DOTALL | re.IGNORECASE).strip()
        else:
            clean_content = content
            
    return clean_content, thinking


def load_memories_data() -> dict:
    """
    Загружает JSON со всеми заметками и системными промптами.
    Поддерживает обратную совместимость со старыми плоскими файлами memories.json.
    """
    if not os.path.exists(MEMORIES_FILE):
        return {"notes": {}, "system_instructions": {}}
    try:
        with open(MEMORIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "notes" not in data and "system_instructions" not in data:
                return {"notes": data, "system_instructions": {}}
            return data
    except Exception as e:
        print(f"[MEMORIES] Ошибка загрузки: {e}", flush=True)
        return {"notes": {}, "system_instructions": {}}


def save_memories_data(data: dict) -> None:
    """Сохраняет структуру заметок и системных инструкций в JSON."""
    try:
        os.makedirs(os.path.dirname(MEMORIES_FILE), exist_ok=True)
        with open(MEMORIES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[MEMORIES] Ошибка сохранения: {e}", flush=True)


def append_memory(server_id: str, text: str, is_manual: bool = False) -> bool:
    """
    Добавляет заметку для конкретного сервера.
    """
    from src.config import get_server_limits
    
    try:
        guild_id = int(server_id) if server_id.isdigit() else None
    except ValueError:
        guild_id = None
        
    limits = get_server_limits(guild_id)
    data = load_memories_data()
    
    notes_dict = data.setdefault("notes", {})
    server_str = str(server_id)
    if server_str not in notes_dict:
        notes_dict[server_str] = []
        
    server_notes = notes_dict[server_str]
    
    # БЕЗОПАСНАЯ проверка типа (пропускает старые строки без вызова исключения AttributeError)
    auto_notes_count = sum(
        1 for n in server_notes 
        if isinstance(n, dict) and not n.get("is_manual", False)
    )
    
    if not is_manual and auto_notes_count >= limits["max_tool_notes"]:
        print(f"[MEMORIES] Сервер {server_id} превысил лимит авто-заметок ({limits['max_tool_notes']})", flush=True)
        return False
        
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    server_notes.append({
        "timestamp": timestamp,
        "text": text,
        "is_manual": is_manual
    })
    save_memories_data(data)
    return True


def read_memories(server_id: str) -> str:
    """
    Возвращает все сохраненные заметки для указанного сервера.
    """
    data = load_memories_data()
    notes_dict = data.get("notes", {})
    server_str = str(server_id)
    if server_str not in notes_dict or not notes_dict[server_str]:
        return "Заметок пока нет."
        
    lines = []
    for n in notes_dict[server_str]:
        if isinstance(n, dict):
            note_prefix = "[Ручная]" if n.get("is_manual", False) else "[Авто]"
            lines.append(f"[{n['timestamp']}] {note_prefix} {n['text']}")
        elif isinstance(n, str):
            # Безопасно выводим старые записи, сохраненные как обычные строки
            lines.append(n)
    return "\n".join(lines)


def get_custom_system_instruction(server_id: str) -> str | None:
    """Возвращает кастомную системную инструкцию для сервера из memories.json."""
    data = load_memories_data()
    return data.get("system_instructions", {}).get(str(server_id))


def set_custom_system_instruction(server_id: str, instruction: str | None) -> None:
    """Задает или сбрасывает кастомную системную инструкцию для сервера в memories.json."""
    data = load_memories_data()
    instructions = data.setdefault("system_instructions", {})
    
    server_str = str(server_id)
    if instruction is None:
        instructions.pop(server_str, None)
    else:
        instructions[server_str] = instruction
        
    save_memories_data(data)


def check_and_increment_search(server_id: str) -> bool:
    """
    Проверяет дневной лимит поисков для указанного сервера.
    """
    from src.config import get_server_limits
    
    try:
        guild_id = int(server_id) if server_id.isdigit() else None
    except ValueError:
        guild_id = None
        
    limits = get_server_limits(guild_id)
    max_searches = limits["max_searches_per_day"]
    
    if max_searches >= 999999:
        return True
        
    today = datetime.now().strftime("%Y-%m-%d")
    data = {}
    
    if os.path.exists(SEARCH_LIMITS_FILE):
        try:
            with open(SEARCH_LIMITS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
            
    server_str = str(server_id)
    if server_str not in data:
        data[server_str] = {"date": today, "count": 0}
        
    server_data = data[server_str]
    if server_data.get("date") != today:
        server_data["date"] = today
        server_data["count"] = 0
        
    if server_data["count"] >= max_searches:
        return False
        
    server_data["count"] += 1
    
    try:
        os.makedirs(os.path.dirname(SEARCH_LIMITS_FILE), exist_ok=True)
        with open(SEARCH_LIMITS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[SEARCH_LIMITS] Ошибка записи счетчика: {e}", flush=True)
        
    return True


def save_conversations(histories: dict) -> None:
    """
    Сохраняет историю диалогов в файл src/chat.txt в формате JSON.
    """
    try:
        os.makedirs(os.path.dirname(CHAT_HISTORY_FILE), exist_ok=True)
        serialized = {str(k): v for k, v in histories.items()}
        with open(CHAT_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(serialized, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[SAVE_HIST] Ошибка сохранения истории в {CHAT_HISTORY_FILE}: {e}", flush=True)


def load_conversations() -> dict:
    """
    Загружает историю диалогов из файла src/chat.txt.
    """
    if not os.path.exists(CHAT_HISTORY_FILE):
        return {}
    try:
        with open(CHAT_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    except Exception as e:
        print(f"[LOAD_HIST] Ошибка загрузки истории из {CHAT_HISTORY_FILE}: {e}", flush=True)
        return {}
    
def get_max_active_channels(server_id: str) -> int:
    """Возвращает максимальное количество активных каналов для сервера."""
    data = load_memories_data()
    return data.get("max_active_channels", {}).get(str(server_id), 2)


def set_max_active_channels(server_id: str, limit: int) -> None:
    """Сохраняет максимальное количество активных каналов для сервера."""
    data = load_memories_data()
    max_channels = data.setdefault("max_active_channels", {})
    max_channels[str(server_id)] = limit
    save_memories_data(data)


def register_channel_server(channel_id: int, server_id: str) -> None:
    """Регистрирует канал за конкретным сервером в memories.json."""
    data = load_memories_data()
    mapping = data.setdefault("channel_servers", {})
    mapping[str(channel_id)] = str(server_id)
    save_memories_data(data)


def unregister_channel_server(channel_id: int) -> None:
    """Удаляет канал из реестра memories.json."""
    data = load_memories_data()
    mapping = data.setdefault("channel_servers", {})
    mapping.pop(str(channel_id), None)
    save_memories_data(data)


def get_active_channels_count(bot, server_id: str) -> int:
    """
    Подсчитывает количество активных каналов на указанном сервере.
    Использует реестр в memories.json и кэш бота для обратной совместимости.
    """
    data = load_memories_data()
    mapping = data.setdefault("channel_servers", {})
    
    # Получаем текущие активные ID каналов из оперативной памяти бота
    active_ids = set(str(cid) for cid in bot.conversation_histories.keys())
    
    # Очищаем из реестра те каналы, которые больше не активны в памяти
    outdated = [cid for cid in mapping if cid not in active_ids]
    if outdated:
        for cid in outdated:
            mapping.pop(cid, None)
        save_memories_data(data)
    
    count = 0
    for cid_str in active_ids:
        if cid_str in mapping:
            if mapping[cid_str] == str(server_id):
                count += 1
        else:
            # Если записи в реестре нет, пытаемся определить сервер через объект канала
            try:
                channel = bot.get_channel(int(cid_str))
                if channel:
                    ch_server_id = str(channel.guild.id) if getattr(channel, "guild", None) else f"DM_{cid_str}"
                    mapping[cid_str] = ch_server_id
                    save_memories_data(data)
                    if ch_server_id == str(server_id):
                        count += 1
            except Exception:
                pass
    return count


def format_message_timestamp(dt) -> str:
    """
    Форматирует дату и время сообщения в Московском часовом поясе (UTC+3)
    """
    from datetime import timezone, timedelta
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    moscow_tz = timezone(timedelta(hours=3))
    moscow_dt = dt.astimezone(moscow_tz)
    return moscow_dt.strftime("%Y-%m-%d %H:%M:%S")


def format_message_with_metadata(author_name: str, clean_text: str, timestamp, ref_msg=None) -> str:
    """
    Форматирует сообщение пользователя с добавлением даты, времени и информации об ответе.
    """
    timestamp_str = format_message_timestamp(timestamp)
    reply_info = ""
    if ref_msg:
        ref_author = getattr(ref_msg.author, "display_name", str(ref_msg.author))
        ref_content = getattr(ref_msg, "clean_content", "") or getattr(ref_msg, "content", "") or ""
        
        # Очищаем превью от лишних пробелов и переносов
        ref_preview = ref_content.strip()
        if len(ref_preview) > 80:
            ref_preview = ref_preview[:80] + "..."
        ref_preview = ref_preview.replace("\n", " ")
        reply_info = f" (в ответ на {ref_author}: \"{ref_preview}\")"
        
    if clean_text:
        return f"[{timestamp_str}] {author_name}{reply_info}: {clean_text}"
    else:
        return f"[{timestamp_str}] {author_name}{reply_info}"
    
    # === ДОБАВЛЕНО В КОНЕЦ ФАЙЛА src/utils.py ===

def log_context_occupancy(bot) -> None:
    """
    Выводит в консоль структурированный отчет по заполненности контекста в токенах и процентах
    для каждого активного канала, сгруппированных по серверам.
    """
    try:
        from src.config import get_server_limits
        
        # Загружаем сохраненные соответствия каналов серверам из реестра
        mem_data = load_memories_data()
        channel_servers = mem_data.get("channel_servers", {})
        
        # Структура для группировки: {server_display_name: [channel_data]}
        grouped = {}
        
        for channel_id, history in bot.conversation_histories.items():
            channel = bot.get_channel(channel_id)
            
            guild_id = None
            server_name = "Личные сообщения (DM)"
            channel_name = f"ID: {channel_id}"
            
            if channel:
                channel_name = getattr(channel, "name", channel_name)
                if getattr(channel, "guild", None):
                    guild_id = channel.guild.id
                    server_name = f"Сервер '{channel.guild.name}' (ID: {guild_id})"
            else:
                # Если канала нет в кэше, пытаемся восстановить имя/ID сервера из сохраненного маппинга
                saved_server_id = channel_servers.get(str(channel_id))
                if saved_server_id:
                    if saved_server_id.startswith("DM_"):
                        server_name = "Личные сообщения (DM)"
                    else:
                        try:
                            guild_id = int(saved_server_id)
                            guild = bot.get_guild(guild_id)
                            if guild:
                                server_name = f"Сервер '{guild.name}' (ID: {guild_id})"
                            else:
                                server_name = f"Сервер ID: {saved_server_id}"
                        except ValueError:
                            server_name = f"Сервер ID: {saved_server_id}"
                            
            limits = get_server_limits(guild_id)
            max_tokens = limits["max_context_tokens"]
            current_tokens = estimate_tokens(history)
            
            percent = (current_tokens / max_tokens) * 100 if max_tokens > 0 else 0.0
            
            grouped.setdefault(server_name, []).append({
                "name": channel_name,
                "id": channel_id,
                "current": current_tokens,
                "max": max_tokens,
                "percent": percent
            })
            
        if not grouped:
            print("\n[CONTEXT MONITOR] Нет активных контекстов диалогов.\n", flush=True)
            return
            
        lines = ["\n" + "=" * 60, "[CONTEXT MONITOR] Текущая заполненность контекста по серверам и каналам:"]
        for server, channels in grouped.items():
            lines.append(f"• {server}:")
            for ch in channels:
                lines.append(
                    f"  - #{ch['name']} (ID: {ch['id']}): "
                    f"{ch['current']}/{ch['max']} токенов ({ch['percent']:.1f}%)"
                )
        lines.append("=" * 60 + "\n")
        print("\n".join(lines), flush=True)
        
    except Exception as e:
        print(f"[CONTEXT MONITOR ERROR] Ошибка при формировании отчета: {e}", flush=True)
