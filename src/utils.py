import io
import base64
import httpx
import re

def compress_image(img_bytes: bytes, max_size: int = 1000, quality: int = 70) -> bytes:
    """
    Сжимает изображение и уменьшает его разрешение, чтобы сэкономить ОЗУ хостинга и токены.
    """
    try:
        from PIL import Image
        
        image = Image.open(io.BytesIO(img_bytes))
        
        # Конвертируем RGBA/P в RGB для сохранения в формате JPEG
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        
        # Уменьшаем разрешение, сохраняя пропорции
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


# Регулярное выражение для поиска ссылок с расширениями изображений в тексте
IMAGE_URL_REGEX = re.compile(
    r'https?://[^\s/$.?#].[^\s]*\.(?:png|jpg|jpeg|webp|gif|bmp)(?:\?[^\s]*)?', 
    re.IGNORECASE
)


def extract_image_urls(message) -> list:
    """
    Извлекает уникальные ссылки на изображения как из текста сообщения, так и из эмбедов.
    """
    urls = []
    
    # 1. Поиск прямых ссылок на картинки в тексте сообщения
    if message.content:
        found_urls = IMAGE_URL_REGEX.findall(message.content)
        for url in found_urls:
            if url not in urls:
                urls.append(url)
                
    # 2. Поиск изображений внутри сгенерированных Discord-эмбедов (включая превью ссылок)
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
        
        # Проверяем наличие текстовых полей, игнорируя эмбеды, содержащие исключительно медиафайлы
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
            # Сначала пытаемся отправить быстрый HEAD-запрос для предварительной проверки
            try:
                head_resp = await client.head(url, timeout=5.0, follow_redirects=True)
                if head_resp.status_code == 200:
                    content_type = head_resp.headers.get("content-type", "").lower()
                    if content_type and "image" not in content_type:
                        return None
                    
                    content_length = head_resp.headers.get("content-length")
                    if content_length and int(content_length) > 20 * 1024 * 1024:
                        print(f"[FETCH_IMAGE] Отмена скачивания. Файл по ссылке превышает лимит в 20МБ.", flush=True)
                        return None
            except Exception:
                # Если сервер не поддерживает HEAD-запрос, переходим сразу к GET
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
    total_tokens = 350  # Базовый запас под системный промпт
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
            print("[PRUNE] Удалено одно сообщение из объединенного блока пользователя.", flush=True)
        else:
            if len(history) >= 2:
                history.pop(0)
                history.pop(0)
            else:
                history.pop(0)
            print("[PRUNE] Удален полный шаг диалога.", flush=True)
            
    # Гарантируем, что история не начнется с технического ответа ассистента или инструмента
    while history and history[0].get("role") in ["assistant", "tool"]:
        history.pop(0)
        
    final_tokens = estimate_tokens(history)
    if pruned:
        print(f"[PRUNE] Очистка завершена. Было: {initial_tokens} токенов, стало: {final_tokens} токенов.", flush=True)
    else:
        print(f"[PRUNE_CHECK] Контекст в норме ({final_tokens}/{max_tokens} токенов). Очистка не требуется.", flush=True)
        
    log_last_message(history, "PRUNE_END")
    return history
