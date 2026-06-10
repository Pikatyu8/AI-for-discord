# commands.py
import json
import io
import discord
import re
import asyncio
from discord.ext import commands
from datetime import datetime, timezone

from src.config import BASE_SYSTEM_INSTRUCTION, get_server_limits, GEMINI_API_KEY
from src.llm import generate_content_with_retry, describe_image_async
from src.search import (
    perform_search_async, 
    perform_google_search, 
    HAS_GOOGLE_GENAI, 
    GOOGLE_GENAI_IMPORT_ERROR,
    parse_queries_list
)
from src.utils import (
    is_image_attachment,
    get_normalized_mime_type,
    bytes_to_base64_url,
    prune_history_local,
    log_last_message,
    extract_image_urls,
    fetch_image_as_base64,
    extract_embeds_text,
    extract_and_strip_thoughts,
    append_memory,
    read_memories,
    get_custom_system_instruction,
    set_custom_system_instruction,
    check_and_increment_search,
    save_conversations,
    is_text_or_pdf_attachment,
    extract_text_from_pdf,
    get_max_active_channels,
    set_max_active_channels,
    get_active_channels_count,
    register_channel_server,
    unregister_channel_server,
    load_memories_data,
    format_message_timestamp,
    format_message_with_metadata,
    log_context_occupancy
)

def find_cache_entry_by_target(target: str) -> str | None:
    """
    Ищет ключ в IMAGE_DESCRIPTION_CACHE по MD5-хэшу (полному или первыми 8 символами),
    ID сообщения или по полной ссылке на сообщение.
    """
    from src.llm import IMAGE_DESCRIPTION_CACHE
    target = target.strip()
    
    if not target:
        return None
        
    # 1. Полнотекстовое совпадение с MD5
    if target in IMAGE_DESCRIPTION_CACHE:
        return target
        
    # 2. Совпадение хэша без учета регистра
    if len(target) == 32 and all(c in "0123456789abcdefABCDEF" for c in target):
        target_lower = target.lower()
        if target_lower in IMAGE_DESCRIPTION_CACHE:
            return target_lower
            
    # 3. Поиск по ссылке на сообщение (например, https://discord.com/channels/123/456/789)
    msg_link_match = re.search(r'channels/(?:@me|\d+)/\d+/(\d+)', target)
    if msg_link_match:
        msg_id = msg_link_match.group(1)
        for h, val in IMAGE_DESCRIPTION_CACHE.items():
            if isinstance(val, dict) and str(val.get("message_id")) == msg_id:
                return h
                
    # 4. Поиск по чистому ID сообщения
    if target.isdigit():
        for h, val in IMAGE_DESCRIPTION_CACHE.items():
            if isinstance(val, dict) and str(val.get("message_id")) == target:
                return h
                
    # 5. Поиск по частичному совпадению хэша (префиксу, например первые 8 символов)
    for h in IMAGE_DESCRIPTION_CACHE.keys():
        if h.startswith(target.lower()):
            return h
            
    return None



class BotCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot


    @commands.command(name="show-descs")
    async def show_descs_cmd(self, ctx):
        """Показывает описания картинок с ссылками на сообщения."""
        from src.llm import IMAGE_DESCRIPTION_CACHE
        
        if not IMAGE_DESCRIPTION_CACHE:
            await ctx.send("Описаний картинок пока нет в кэше.")
            return
            
        lines = ["**Сохраненные описания картинок:**"]
        for img_hash, entry in IMAGE_DESCRIPTION_CACHE.items():
            if isinstance(entry, dict):
                desc = entry.get("description", "")
                m_id = entry.get("message_id")
                c_id = entry.get("channel_id")
                g_id = entry.get("guild_id")
                
                if m_id and c_id:
                    g_str = str(g_id) if g_id else "@me"
                    link = f"https://discord.com/channels/{g_str}/{c_id}/{m_id}"
                    link_text = f"[Ссылка]({link})"
                else:
                    link_text = "Ссылка отсутствует"
                    
                short_desc = desc if len(desc) <= 120 else desc[:117] + "..."
                lines.append(f"• `{img_hash[:8]}` ({link_text}): {short_desc}")
            else:
                short_desc = entry if len(entry) <= 120 else entry[:117] + "..."
                lines.append(f"• `{img_hash[:8]}` (Нет ссылки): {short_desc}")
                
        full_text = "\n".join(lines)
        if len(full_text) > 1900:
            file_data = io.BytesIO(full_text.encode("utf-8"))
            discord_file = discord.File(fp=file_data, filename="image_descriptions.txt")
            await ctx.send("Кэш описаний слишком большой. Полный список выгружен в файл:", file=discord_file)
        else:
            await ctx.send(full_text)

    @commands.command(name="edit-desc")
    async def edit_desc_cmd(self, ctx, *, args: str = None):
        """Редактирует описание картинки по хешу/ссылке на сообщение, если текущее описание неточно."""
        from src.llm import IMAGE_DESCRIPTION_CACHE
        
        if not args:
            await ctx.send(
                "Укажите параметры. Формат команды:\n"
                "`!edit-desc <хэш или ссылка> | <новое описание>`\n\n"
                "Примеры:\n"
                "• `!edit-desc 8a9b2c3d | Кошка сидит на диване`\n"
                "• `!edit-desc https://discord.com/channels/123/456/789 | Исправленный текст`"
            )
            return
            
        parts = [p.strip() for p in args.split("|") if p.strip()]
        if len(parts) < 2:
            # Если разделитель "|" отсутствует, пробуем разделить по первому пробелу
            split_space = args.split(maxsplit=1)
            if len(split_space) == 2:
                target_cand, desc_cand = split_space
                if len(target_cand) == 32 or "channels/" in target_cand or target_cand.isdigit():
                    parts = [target_cand, desc_cand]
                    
        if len(parts) < 2:
            await ctx.send("Ошибка: Не удалось распознать аргументы. Используйте вертикальную черту `|` для разделения хэша/ссылки и нового описания.")
            return
            
        target, new_desc = parts[0], parts[1]
        img_hash = find_cache_entry_by_target(target)
        
        if not img_hash or img_hash not in IMAGE_DESCRIPTION_CACHE:
            await ctx.send(f"Ошибка: Не найдено сохраненное описание для '{target}' в кэше.")
            return
            
        entry = IMAGE_DESCRIPTION_CACHE[img_hash]
        if isinstance(entry, dict):
            entry["description"] = new_desc
        else:
            # Преобразуем старую плоскую запись в новую структуру
            IMAGE_DESCRIPTION_CACHE[img_hash] = {
                "description": new_desc,
                "message_id": None,
                "channel_id": None,
                "guild_id": None
            }
            
        await ctx.send(f"Описание для картинки `{img_hash[:8]}` успешно отредактировано!")

    @commands.command(name="help")
    async def help_cmd(self, ctx, *, lang: str = "ru"):
        """Показывает список доступных команд."""
        lang = lang.replace("|", "").strip().lower()
        if lang not in ["en", "english"]:
            help_text = (
                "**Доступные команды бота:**\n"
                "```\n"
                "!load n - загружает n сообщений в чате, до 201\n"
                "!unload - выгружает все сообщения из чата\n"
                "!export | json - показывает историю чата в боте (можно запустить с параметром json)\n"
                "!show - показывает, где активен бот\n"
                "!stop - приостанавливает запись сообщений в канале\n"
                "!maxchannels | n - задает максимум активных каналов для бота (по умолчанию 2)\n"
                "!think | on/off - переключает режим размышлений модели\n"
                "!search | info  - ищет информацию и заставляет бота ответить на её основе\n"
                "!search-google | info - тоже самое что и !search но без фолбека на тавили\n"
                "!note | text - создает запись в память бота для текущего сервера\n"
                "!notes - показывает записи в памяти\n"
                "!show-descs - показывает описания картинок с ссылками на сообщения\n"
                "!edit-desc | md5-hash/message link | desc - редактирует описание картинки по хешу/ссылке на сообщение, если текущее описание неточно\n"
                "!help | en/ru - показывает этот текст\n"
                "```"
            )
        else:
            help_text = (
                "**Available bot commands:**\n"
                "```\n"
                "!load n - loads n messages from the chat, up to 201\n"
                "!unload - unloads all messages from the chat\n"
                "!export | json - displays the chat history in the bot (can be run with the json parameter)\n"
                "!show - shows where the bot is active\n"
                "!stop - pauses message logging in the channel\n"
                "!maxchannels | n - sets the maximum number of active channels for the bot (default is 2)\n"
                "!think | on/off - toggles the model's thinking mode\n"
                "!search | info  - searches for information and prompts the bot to respond based on it\n"
                "!search-google | info - same as !search but without a fallback to the bot\n"
                "!note | text - creates an entry in the bot's memory for the current server\n"
                "!notes - displays entries in memory\n"
                "!show-descs - displays image descriptions with links to messages\n"
                "!edit-desc | md5-hash/message link | desc - edits the image description based on the hash/message link if the current description is inaccurate\n"
                "!help | en/ru - displays this text\n"
                "```"
            )
        await ctx.send(help_text)

    

    @commands.command(name="stop")
    async def stop_bot(self, ctx):
        context_id = ctx.channel.id
        self.bot.thinking_channels.discard(context_id)
        
        in_history = context_id in self.bot.conversation_histories
        if in_history:
            self.bot.conversation_histories.pop(context_id, None)
            unregister_channel_server(context_id)
            save_conversations(self.bot.conversation_histories)
            print(f"[STOP] Бот остановлен в канале {context_id}", flush=True)
            log_last_message([], "STOP")
            log_context_occupancy(self.bot)
        
        try:
            if in_history:
                await ctx.send("Бот успешно остановлен и переведен в спящий режим в этом канале. Логгирование прекращено.")
            else:
                await ctx.send("Бот уже находится в спящем режиме в этом канале.")
        except discord.Forbidden:
            print(f"[STOP] Предупреждение: Не удалось отправить подтверждение в канал {context_id} из-за отсутствия прав (Forbidden). Память успешно очищена локально.", flush=True)

    @commands.command(name="setsystem")
    async def set_system_instruction_cmd(self, ctx, *, text: str = None):
        """Задает кастомную системную инструкцию для текущего сервера (доступно только разработчику)."""
        if ctx.author.id != 1145437788217553017:
            await ctx.send("У вас нет прав для изменения системной инструкции.")
            return

        server_id_str = str(ctx.guild.id) if ctx.guild else f"DM_{ctx.channel.id}"
        
        if not text:
            current = get_custom_system_instruction(server_id_str)
            if current:
                await ctx.send(f"**Текущая кастомная системная инструкция:**\n```\n{current}\n```")
            else:
                await ctx.send("Кастомная системная инструкция не задана (используется дефолтная).")
            return

        if text.lower() == "reset":
            set_custom_system_instruction(server_id_str, None)
            await ctx.send("Системная инструкция успешно сброшена на дефолтную!")
            return

        set_custom_system_instruction(server_id_str, text)
        await ctx.send("Кастомная системная инструкция успешно сохранена для этого сервера!")

    @commands.command(name="think")
    async def toggle_thinking(self, ctx, state: str = None):
        """Включает или выключает режим размышлений для текущего канала."""
        context_id = ctx.channel.id
        if state is None:
            if context_id in self.bot.thinking_channels:
                self.bot.thinking_channels.remove(context_id)
                await ctx.send("Режим размышлений для этого канала **выключен**.")
            else:
                self.bot.thinking_channels.add(context_id)
                await ctx.send("Режим размышлений для этого канала **включен**.")
        else:
            state = state.lower()
            if state in ["on", "true", "yes", "вкл", "включить"]:
                self.bot.thinking_channels.add(context_id)
                await ctx.send("Режим размышлений для этого канала **включен**.")
            elif state in ["off", "false", "no", "выкл", "выключить"]:
                self.bot.thinking_channels.discard(context_id)
                await ctx.send("Режим размышлений для этого канала **выключен**.")
            else:
                await ctx.send("Укажите `on`/`off` или вызовите команду без аргументов для переключения.")

    @commands.command(name="show")
    async def show_active_channels(self, ctx):
        active_ids = list(self.bot.conversation_histories.keys())
        if not active_ids:
            await ctx.send("В данный момент active-каналов нет (бот везде спит).")
            return
        
        server_id_str = str(ctx.guild.id) if ctx.guild else f"DM_{ctx.channel.id}"
        data = load_memories_data()
        mapping = data.get("channel_servers", {})
        
        server_active_ids = []
        for cid in active_ids:
            if mapping.get(str(cid)) == server_id_str:
                server_active_ids.append(cid)
            else:
                channel = self.bot.get_channel(cid)
                if channel:
                    ch_server_id = str(channel.guild.id) if getattr(channel, "guild", None) else f"DM_{cid}"
                    if ch_server_id == server_id_str:
                        server_active_ids.append(cid)
                        
        if not server_active_ids:
            await ctx.send("В данный момент активных каналов на этом сервере нет.")
            return
        
        lines = [f"**Активные каналы на этом сервере ({len(server_active_ids)}/{get_max_active_channels(server_id_str)}):**"]
        for cid in server_active_ids:
            channel = self.bot.get_channel(cid)
            status = "🧠 [Мысли ВКЛ]" if cid in self.bot.thinking_channels else "💤 [Мысли ВЫКЛ]"
            if channel:
                lines.append(f"• #{channel.name} (ID: {cid}) — {status}")
            else:
                lines.append(f"• Неизвестный канал (ID: {cid}) — {status}")
                
        await ctx.send("\n".join(lines))

    @commands.command(name="maxchannels")
    @commands.has_permissions(administrator=True)
    async def set_max_channels(self, ctx, limit: int = None):
        server_id_str = str(ctx.guild.id) if ctx.guild else f"DM_{ctx.channel.id}"
        if limit is None:
            current_limit = get_max_active_channels(server_id_str)
            await ctx.send(f"Текущее ограничение на количество активных каналов для этого сервера: **{current_limit}**.")
            return
        
        if limit <= 0:
            await ctx.send("Лимит должен быть больше 0.")
            return
            
        set_max_active_channels(server_id_str, limit)
        await ctx.send(f"Максимальное количество активных каналов для этого сервера успешно установлено на: **{limit}**.")

    @commands.command(name="export")
    async def export_messages(self, ctx, file_format: str = "txt"):
        context_id = ctx.channel.id
        
        if context_id not in self.bot.conversation_histories or not self.bot.conversation_histories[context_id]:
            await ctx.send("Память бота для этого канала пуста. Нечего экспортировать.")
            return

        history = self.bot.conversation_histories[context_id]
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
                    clean_history.append({"role": "role", "content": clean_parts})

            json_data = json.dumps(clean_history, ensure_ascii=False, indent=4)
            file_data = io.BytesIO(json_data.encode("utf-8"))
            filename = f"chat_history_{context_id}.json"
        else:
            text_lines = [f"=== ЭКСПОРТ ИСТОРИИ ЧАТА: КАНАЛ '{ctx.channel.name}' ({context_id}) ===\n"]
            
            for idx, msg in enumerate(history, 1):
                role_raw = msg.get("role")
                role_display = "Пользователь (User)" if role_raw == "user" else "Бот (Assistant)"
                content = msg.get("content") or ""
                
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

    @commands.command(name="unload")
    async def unload_messages(self, ctx):
        context_id = ctx.channel.id
        
        if context_id in self.bot.conversation_histories and self.bot.conversation_histories[context_id]:
            self.bot.conversation_histories[context_id] = []
            save_conversations(self.bot.conversation_histories)
            await ctx.send("Память бота для этого канала успешно очищена!")
            print(f"[UNLOAD] Очищен контекст для канала {context_id} ({ctx.channel.name})", flush=True)
            log_last_message([], "UNLOAD")
            log_context_occupancy(self.bot)
        else:
            await ctx.send("Память бота для этого канала уже пуста.")

    @commands.command(name="load")
    async def load_messages(self, ctx, limit: int = 10):
        guild_id = ctx.guild.id if ctx.guild else None
        limits = get_server_limits(guild_id)
        max_load = limits["max_load_messages"]

        if limit <= 0:
            await ctx.send("Укажите число больше 0.")
            return
        if limit > max_load:
            await ctx.send(f"Лимит загрузки за один раз на этом сервере — {max_load} сообщений.")
            return

        context_id = ctx.channel.id
        is_active = context_id in self.bot.conversation_histories
        server_id_str = str(ctx.guild.id) if ctx.guild else f"DM_{context_id}"
        
        if not is_active:
            max_channels = get_max_active_channels(server_id_str)
            active_count = get_active_channels_count(self.bot, server_id_str)
            if active_count >= max_channels:
                await ctx.send(
                    f"Не удалось активировать канал. Достигнут лимит активных каналов на этом сервере ({max_channels}). "
                    f"Используйте `!stop` в другом канале этого сервера или увеличьте лимит через `!maxchannels`."
                )
                return

        status_message = await ctx.send(f"Загружаю последние {limit} сообщений...")
        
        messages = []
        async for msg in ctx.channel.history(limit=limit + 10):
            # Пропускаем и само сообщение команды, и временное сервисное сообщение бота
            if msg.id == ctx.message.id or msg.id == status_message.id:
                continue
            messages.append(msg)
            if len(messages) == limit:
                break
                
        messages.reverse()
        new_history = []
        
        msg_map = {m.id: m for m in messages}
        
        for msg in messages:
            timestamp_str = format_message_timestamp(msg.created_at)
            
            if msg.author == self.bot.user:
                new_history.append({"role": "assistant", "content": f"[{timestamp_str}] {msg.clean_content}"})
            else:
                clean_text = msg.clean_content
                
                bot_mentions = [f"@{self.bot.user.name}", f"@{self.bot.user.display_name}"]
                if msg.guild and msg.guild.me:
                    bot_mentions.append(f"@{msg.guild.me.display_name}")
                    bot_mentions.append(f"@{msg.guild.me.name}")
                    
                for mention in sorted(list(set(bot_mentions)), key=len, reverse=True):
                    clean_text = clean_text.replace(mention, "")
                
                clean_text = clean_text.strip()
                
                embeds_text = extract_embeds_text(msg)
                if embeds_text:
                    if clean_text:
                        clean_text = f"{clean_text}\n\n[Текст из прикрепленных ссылок/эмбедов]:\n{embeds_text}"
                    else:
                        clean_text = f"[Текст из прикрепленных ссылок/эмбедов]:\n{embeds_text}"
                
                parts = []
                
                for attachment in msg.attachments:
                    if is_image_attachment(attachment):
                        try:
                            mime_type = get_normalized_mime_type(attachment)
                            img_bytes = await attachment.read()
                            base64_url = bytes_to_base64_url(img_bytes, mime_type)
                            parts.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": base64_url,
                                    "message_id": msg.id,
                                    "channel_id": msg.channel.id
                                }
                            })
                        except Exception as e:
                            print(f"[LOAD] Ошибка загрузки картинки из вложений: {e}", flush=True)
                    elif is_text_or_pdf_attachment(attachment):
                        try:
                            file_bytes = await attachment.read()
                            filename = attachment.filename
                            if filename.lower().endswith('.pdf'):
                                content = extract_text_from_pdf(file_bytes)
                            else:
                                content = file_bytes.decode("utf-8", errors="ignore")
                            
                            parts.append({
                                "type": "text",
                                "text": f"\n[Содержимое файла '{filename}']:\n{content}\n[Конец файла '{filename}']\n"
                            })
                        except Exception as e:
                            print(f"[LOAD] Ошибка чтения документа {attachment.filename}: {e}", flush=True)
                
                image_urls = extract_image_urls(msg)
                for url in image_urls:
                    base64_url = await fetch_image_as_base64(url)
                    if base64_url:
                        parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": base64_url,
                                "message_id": msg.id,
                                "channel_id": msg.channel.id
                            }
                        })
                            
                ref_msg = None
                if msg.reference:
                    ref_msg = msg_map.get(msg.reference.message_id) or msg.reference.cached_message

                full_message_text = format_message_with_metadata(
                    author_name=msg.author.display_name,
                    clean_text=clean_text,
                    timestamp=msg.created_at,
                    ref_msg=ref_msg
                )
                    
                if len(parts) == 1 and parts[0]["type"] == "text":
                    content_to_add = f"{full_message_text}\n{parts[0]['text']}"
                elif not parts:
                    content_to_add = full_message_text
                else:
                    parts.append({
                        "type": "text",
                        "text": full_message_text
                    })
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

        new_history = prune_history_local(new_history, max_tokens=limits["max_context_tokens"])
        self.bot.conversation_histories[context_id] = new_history
        register_channel_server(context_id, server_id_str)
        save_conversations(self.bot.conversation_histories)
        
        # === АВТОМАТИЧЕСКИЙ ЗАПУСК ОПИСАНИЙ ПОСЛЕ !load ===
        image_tasks_count = 0
        for msg_item in new_history:
            content_item = msg_item.get("content")
            if isinstance(content_item, list):
                for part in content_item:
                    if part.get("type") == "image_url":
                        url_obj = part.get("image_url", {})
                        url = url_obj.get("url", "")
                        m_id = url_obj.get("message_id")
                        c_id = url_obj.get("channel_id")
                        if url:
                            # Запуск асинхронной задачи генерации без блокирования ответа в Discord
                            asyncio.create_task(describe_image_async(
                                url, 
                                message_id=m_id, 
                                channel_id=c_id, 
                                bot=self.bot
                            ))
                            image_tasks_count += 1
        if image_tasks_count > 0:
            print(f"[LOAD] Автоматически запущено фоновое описание для {image_tasks_count} изображений из истории.", flush=True)
        
        log_last_message(new_history, "LOAD_END")
        log_context_occupancy(self.bot)
        await status_message.edit(content=f"Успешно загружено и обработано {len(messages)} сообщений в контекст!")

    @commands.command(name="search")
    async def force_search(self, ctx, *, query: str = None):
        if not query:
            await ctx.send("Укажите, что именно нужно найти. Пример: `!search последние новости ИИ`")
            return

        context_id = ctx.channel.id
        guild_id = ctx.guild.id if ctx.guild else None
        limits = get_server_limits(guild_id)
        server_id_str = str(ctx.guild.id) if ctx.guild else f"DM_{context_id}"

        if not check_and_increment_search(server_id_str):
            await ctx.send("Ошибка: Достигнут дневной лимит поисков в сети (5 поисков в день) для этого сервера.")
            return

        is_active = context_id in self.bot.conversation_histories
        if not is_active:
            max_channels = get_max_active_channels(server_id_str)
            active_count = get_active_channels_count(self.bot, server_id_str)
            if active_count >= max_channels:
                await ctx.send(
                    f"Не удалось активировать поиск. Достигнут лимит активных каналов на этом сервере ({max_channels}). "
                    "Попросите администратора изменить лимит через `!maxchannels` или отключите бота в другом канале командой `!stop`."
                )
                return
            self.bot.conversation_histories[context_id] = []
            register_channel_server(context_id, server_id_str)
            is_active = True
            save_conversations(self.bot.conversation_histories)

        queries_to_run = parse_queries_list(query)[:5]
        queries_display = ", ".join(f"*{q}*" for q in queries_to_run)

        status_msg = await ctx.send(f"🔍 Выполняю принудительный поиск в сети по запросам: {queries_display}...")
        search_results = await perform_search_async(queries_to_run)

        await status_msg.edit(content=f"🔍 Найдено! Анализирую результаты для ответа...")

        history = self.bot.conversation_histories[context_id]
        
        timestamp_str = format_message_timestamp(ctx.message.created_at)
        search_prompt = (
            f"Пользователь запросил принудительный поиск по теме: \"{query}\".\n"
            f"Вот результаты поиска из интернета:\n"
            f"{json.dumps(search_results, ensure_ascii=False, indent=2)}\n\n"
            f"Пожалуйста, ответь на этот запрос пользователя, лаконично и емко опираясь на эти результаты."
        )
        
        history.append({"role": "user", "content": f"[{timestamp_str}] Система: {search_prompt}"})
        history = prune_history_local(history, max_tokens=limits["max_context_tokens"])
        self.bot.conversation_histories[context_id] = history
        save_conversations(self.bot.conversation_histories)

        custom_inst = get_custom_system_instruction(server_id_str)
        sys_inst = custom_inst if custom_inst else BASE_SYSTEM_INSTRUCTION

        sys_inst += (
            "\n\nПРИМЕЧАНИЕ: Все сообщения в истории снабжены метками времени в формате '[YYYY-MM-DD HH:MM:SS] Имя:'. "
            "Это сделано исключительно для твоего контекста. Тебе самому писать таймстампы или свое имя в начале ответа КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО. "
            "Отвечай сразу по существу, без метаданных в начале."
        )

        if context_id in self.bot.thinking_channels:
            sys_inst += (
                "\n\nВАЖНО: Перед тем как написать финальный краткий ответ, ты ДОЛЖЕН подробно поразмышлять. "
                "Свои подробные размышления обязательно запиши внутри тегов <think> и </think> в самом начале ответа. "
                "Пример: <think>Тут твои размышления...</think>Твой финальный ответ."
            )

        async with ctx.channel.typing():
            try:
                # Передаем статус режима !think в API с привязкой бота
                response = await generate_content_with_retry(
                    history, 
                    sys_inst, 
                    thinking_enabled=(context_id in self.bot.thinking_channels),
                    bot=self.bot
                )
                message_obj = response.choices[0].message
                raw_content = message_obj.content or ""
                
                native_reasoning = None
                if hasattr(message_obj, "reasoning") and message_obj.reasoning:
                    native_reasoning = message_obj.reasoning
                elif getattr(message_obj, "model_extra", None) and "reasoning" in message_obj.model_extra:
                    native_reasoning = message_obj.model_extra["reasoning"]

                reply_text, tagged_reasoning = extract_and_strip_thoughts(raw_content)
                reply_text = re.sub(r'^(\s*\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]\s*)+', '', reply_text).strip()

                thoughts = tagged_reasoning or native_reasoning
                if thoughts:
                    print(f"\n[THINKING LOG - Channel {context_id}]:\n{thoughts.strip()}\n[END THINKING LOG]\n", flush=True)

                if not reply_text:
                    reply_text = "Не удалось сформулировать ответ."
                
                bot_timestamp_str = format_message_timestamp(datetime.now(timezone.utc))
                history.append({"role": "assistant", "content": f"[{bot_timestamp_str}] {reply_text}"})
                self.bot.conversation_histories[context_id] = prune_history_local(history, max_tokens=limits["max_context_tokens"])
                save_conversations(self.bot.conversation_histories)
                
                log_last_message(history, "SEARCH_REPLY")
                log_context_occupancy(self.bot)

                try:
                    await status_msg.delete()
                except Exception:
                    pass

                if len(reply_text) > 2000:
                    await ctx.reply(reply_text[:1900] + "\n\n*(Ответ обрезан из-за лимитов Discord)*")
                else:
                    await ctx.reply(reply_text)
                    
            except Exception as e:
                print(f"Ошибка API при обработке поиска: {e}", flush=True)
                await ctx.reply("Не удалось обработать запрос после поиска. Пожалуйста, попробуйте еще раз.")

    @commands.command(name="search-google")
    async def force_google_search(self, ctx, *, query: str = None):
        """Принудительный поиск исключительно через Google Search (без Tavily)."""
        if not query:
            await ctx.send("Укажите, что именно нужно найти через Google. Пример: `!search-google релиз GTA 6`")
            return

        context_id = ctx.channel.id
        guild_id = ctx.guild.id if ctx.guild else None
        limits = get_server_limits(guild_id)
        server_id_str = str(ctx.guild.id) if ctx.guild else f"DM_{context_id}"

        google_available = True
        reasons = []
        
        if not HAS_GOOGLE_GENAI:
            google_available = False
            reasons.append(f"библиотека 'google-genai' не импортируется (ошибка: {GOOGLE_GENAI_IMPORT_ERROR})")
        
        if not GEMINI_API_KEY:
            google_available = False
            reasons.append("переменная GEMINI_API_KEY отсутствует в .env")

        if not google_available:
            reasons_str = ", ".join(reasons)
            await ctx.send(f"❌ **Google Search недоступен:** {reasons_str}.\nПоиск не запущен. Запросы Tavily не использовались.")
            return

        is_active = context_id in self.bot.conversation_histories
        if not is_active:
            max_channels = get_max_active_channels(server_id_str)
            active_count = get_active_channels_count(self.bot, server_id_str)
            if active_count >= max_channels:
                await ctx.send(
                    f"Не удалось активировать поиск. Достигнут лимит активных каналов на этом сервере ({max_channels}). "
                    "Попросите администратора изменить лимит через `!maxchannels` или отключите бота в другом канале командой `!stop`."
                )
                return
            self.bot.conversation_histories[context_id] = []
            register_channel_server(context_id, server_id_str)
            is_active = True
            save_conversations(self.bot.conversation_histories)

        queries_to_run = parse_queries_list(query)[:5]
        queries_display = ", ".join(f"*{q}*" for q in queries_to_run)

        status_msg = await ctx.send(f"🔍 Выполняю принудительный поиск в Google по запросам: {queries_display}...")
        
        search_results = await perform_search_async(queries_to_run, force_google_only=True)

        if not search_results or (len(search_results) == 1 and search_results[0].get("title") == "Результаты отсутствуют"):
            await status_msg.edit(content=f"⚠️ Google Search не вернул результатов для запросов: {queries_display}. Запросы Tavily не использовались.")
            return

        await status_msg.edit(content=f"🔍 Найдено {len(search_results)} результатов в Google! Анализирую для ответа...")

        history = self.bot.conversation_histories[context_id]
        
        timestamp_str = format_message_timestamp(ctx.message.created_at)
        search_prompt = (
            f"Пользователь запросил принудительный поиск в Google по теме: \"{query}\".\n"
            f"Вот результаты поиска:\n"
            f"{json.dumps(search_results, ensure_ascii=False, indent=2)}\n\n"
            f"Пожалуйста, ответь на этот запрос пользователя, лаконично и емко опираясь на эти результаты."
        )
        
        history.append({"role": "user", "content": f"[{timestamp_str}] Система: {search_prompt}"})
        history = prune_history_local(history, max_tokens=limits["max_context_tokens"])
        self.bot.conversation_histories[context_id] = history
        save_conversations(self.bot.conversation_histories)

        custom_inst = get_custom_system_instruction(server_id_str)
        sys_inst = custom_inst if custom_inst else BASE_SYSTEM_INSTRUCTION

        sys_inst += (
            "\n\nПРИМЕЧАНИЕ: Все сообщения в истории снабжены метками времени в формате '[YYYY-MM-DD HH:MM:SS] Имя:'. "
            "Это сделано исключительно для твоего контекста. Тебе самому писать таймстампы или свое имя в начале ответа КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО. "
            "Отвечай сразу по существу, без метаданных в начале."
        )

        if context_id in self.bot.thinking_channels:
            sys_inst += (
                "\n\nВАЖНО: Перед тем как написать финальный краткий ответ, ты ДОЛЖЕН подробно поразмышлять. "
                "Свои подробные размышления обязательно запиши внутри тегов <think> и </think> в самом начале ответа. "
                "Пример: <think>Тут твои размышления...</think>Твой финальный ответ."
            )

        async with ctx.channel.typing():
            try:
                # Передаем статус режима !think в API с привязкой бота
                response = await generate_content_with_retry(
                    history, 
                    sys_inst, 
                    thinking_enabled=(context_id in self.bot.thinking_channels),
                    bot=self.bot
                )
                message_obj = response.choices[0].message
                raw_content = message_obj.content or ""
                
                native_reasoning = None
                if hasattr(message_obj, "reasoning") and message_obj.reasoning:
                    native_reasoning = message_obj.reasoning
                elif getattr(message_obj, "model_extra", None) and "reasoning" in message_obj.model_extra:
                    native_reasoning = message_obj.model_extra["reasoning"]

                reply_text, tagged_reasoning = extract_and_strip_thoughts(raw_content)

                thoughts = tagged_reasoning or native_reasoning
                if thoughts:
                    print(f"\n[THINKING LOG - Channel {context_id}]:\n{thoughts.strip()}\n[END THINKING LOG]\n", flush=True)

                if not reply_text:
                    reply_text = "Не удалось сформулировать ответ по результатам поиска Google."
                
                bot_timestamp_str = format_message_timestamp(datetime.now(timezone.utc))
                history.append({"role": "assistant", "content": f"[{bot_timestamp_str}] {reply_text}"})
                self.bot.conversation_histories[context_id] = prune_history_local(history, max_tokens=limits["max_context_tokens"])
                save_conversations(self.bot.conversation_histories)
                
                log_last_message(history, "GOOGLE_SEARCH_REPLY")
                log_context_occupancy(self.bot)

                try:
                    await status_msg.delete()
                except Exception:
                    pass

                if len(reply_text) > 2000:
                    await ctx.reply(reply_text[:1900] + "\n\n*(Ответ обрезан из-за лимитов Discord)*")
                else:
                    await ctx.reply(reply_text)
                    
            except Exception as e:
                print(f"Ошибка API при обработке поиска Google: {e}", flush=True)
                await ctx.reply("Не удалось обработать запрос после поиска Google. Пожалуйста, попробуйте еще раз.")

    @commands.command(name="note")
    async def add_note_cmd(self, ctx, *, text: str = None):
        """Быстрое сохранение текстовой заметки на диск вручную."""
        if not text:
            await ctx.send("Укажите текст заметки. Пример: `!note купить хлеб`")
            return
        try:
            server_id_str = str(ctx.guild.id) if ctx.guild else f"DM_{ctx.channel.id}"
            append_memory(server_id_str, f"({ctx.author.display_name}): {text}", is_manual=True)
            await ctx.send("Заметка успешно сохранена!")
        except Exception as e:
            print(f"[CMD_NOTE_ERR] {e}", flush=True)
            await ctx.send("Не удалось сохранить заметку из-за технической ошибки.")

    @commands.command(name="notes")
    async def read_notes_cmd(self, ctx):
        """Вывод всех ранее сохраненных заметок текущего сервера с диска."""
        try:
            server_id_str = str(ctx.guild.id) if ctx.guild else f"DM_{ctx.channel.id}"
            notes = read_memories(server_id_str)
            if len(notes) > 1900:
                file_data = io.BytesIO(notes.encode("utf-8"))
                discord_file = discord.File(fp=file_data, filename="memories.txt")
                await ctx.send("Файл заметок слишком большой. Вот файл с диска:", file=discord_file)
            else:
                await ctx.send(f"**Содержимое заметок этого сервера:**\n```\n{notes}\n```")
        except Exception as e:
            print(f"[CMD_NOTES_ERR] {e}", flush=True)
            await ctx.send("Не удалось прочитать файл заметок.")

async def setup(bot):
    await bot.add_cog(BotCommands(bot))
