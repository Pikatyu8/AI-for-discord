import json
import io
import discord
from discord.ext import commands

from src.config import BASE_SYSTEM_INSTRUCTION
from src.llm import generate_content_with_retry
from src.search import perform_search_async
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
    save_conversations,
    is_text_or_pdf_attachment,
    extract_text_from_pdf
)

class BotCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="stop")
    async def stop_bot(self, ctx):
        context_id = ctx.channel.id
        self.bot.thinking_channels.discard(context_id)
        if context_id in self.bot.conversation_histories:
            self.bot.conversation_histories.pop(context_id, None)
            save_conversations(self.bot.conversation_histories)
            await ctx.send("Бот успешно остановлен и переведен в спящий режим в этом канале. Логгирование прекращено.")
            print(f"[STOP] Бот остановлен в канале {context_id}", flush=True)
            log_last_message([], "STOP")
        else:
            await ctx.send("Бот уже находится в спящем режиме в этом канале.")

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
        
        lines = ["**Каналы, в которых бот сейчас активен и записывает контекст:**"]
        for cid in active_ids:
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
        if limit is None:
            await ctx.send(f"Текущее ограничение на количество активных каналов: **{self.bot.max_active_channels}**.")
            return
        
        if limit <= 0:
            await ctx.send("Лимит должен быть больше 0.")
            return
            
        self.bot.max_active_channels = limit
        await ctx.send(f"Максимальное количество активных каналов успешно установлено на: **{self.bot.max_active_channels}**.")

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
        else:
            await ctx.send("Память бота для этого канала уже пуста.")

    @commands.command(name="load")
    async def load_messages(self, ctx, limit: int = 10):
        if limit <= 0:
            await ctx.send("Укажите число больше 0.")
            return
        if limit > 401:
            await ctx.send("Лимит загрузки за один раз — 401 сообщение.")
            return

        context_id = ctx.channel.id
        is_active = context_id in self.bot.conversation_histories
        
        if not is_active:
            if len(self.bot.conversation_histories) >= self.bot.max_active_channels:
                await ctx.send(
                    f"Не удалось активировать канал. Достигнут лимит активных каналов ({self.bot.max_active_channels}). "
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
            if msg.author == self.bot.user:
                new_history.append({"role": "assistant", "content": msg.clean_content})
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
                                "image_url": {"url": base64_url}
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
                            "image_url": {"url": base64_url}
                        })
                            
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

        new_history = prune_history_local(new_history, max_tokens=128000)
        self.bot.conversation_histories[context_id] = new_history
        save_conversations(self.bot.conversation_histories)
        
        log_last_message(new_history, "LOAD_END")
        await status_message.edit(content=f"Успешно загружено и обработано {len(messages)} сообщений в контекст!")

    @commands.command(name="search")
    async def force_search(self, ctx, *, query: str = None):
        if not query:
            await ctx.send("Укажите, что именно нужно найти. Пример: `!search последние новости ИИ`")
            return

        context_id = ctx.channel.id
        is_active = context_id in self.bot.conversation_histories
        
        if not is_active:
            if len(self.bot.conversation_histories) >= self.bot.max_active_channels:
                await ctx.send(
                    f"Не удалось активировать поиск. Достигнут лимит активных каналов ({self.bot.max_active_channels}). "
                    "Попросите администратора изменить лимит через `!maxchannels` или отключите бота в другом канале командой `!stop`."
                )
                return
            self.bot.conversation_histories[context_id] = []
            is_active = True
            save_conversations(self.bot.conversation_histories)
            print(f"[WAKEUP-SEARCH] Бот проснулся по команде поиска в канале {context_id}", flush=True)

        status_msg = await ctx.send(f"🔍 Выполняю принудительный поиск в сети по запросу: *{query}*...")

        search_results = await perform_search_async(query)

        await status_msg.edit(content=f"🔍 Найдено! Анализирую результаты для ответа...")

        history = self.bot.conversation_histories[context_id]
        
        search_prompt = (
            f"Пользователь запросил принудительный поиск по теме: \"{query}\".\n"
            f"Вот результаты поиска из интернета:\n"
            f"{json.dumps(search_results, ensure_ascii=False, indent=2)}\n\n"
            f"Пожалуйста, ответь на этот запрос пользователя, лаконично и емко опираясь на эти результаты."
        )
        
        history.append({"role": "user", "content": search_prompt})
        history = prune_history_local(history, max_tokens=128000)
        self.bot.conversation_histories[context_id] = history
        save_conversations(self.bot.conversation_histories)

        # Инструкция с учетом режима размышлений
        sys_inst = BASE_SYSTEM_INSTRUCTION
        if context_id in self.bot.thinking_channels:
            sys_inst += (
                "\n\nВАЖНО: Перед тем как написать финальный краткий ответ, ты ДОЛЖЕН подробно поразмышлять. "
                "Свои подробные размышления обязательно запиши внутри тегов <think> и </think> в самом начале ответа. "
                "Пример: <think>Тут твои размышления...</think>Твой финальный ответ."
            )

        async with ctx.channel.typing():
            try:
                response = await generate_content_with_retry(history, sys_inst)
                message_obj = response.choices[0].message
                raw_content = message_obj.content or ""
                
                # Проверяем наличие нативных мыслей от VseGPT
                native_reasoning = None
                if hasattr(message_obj, "reasoning") and message_obj.reasoning:
                    native_reasoning = message_obj.reasoning
                elif getattr(message_obj, "model_extra", None) and "reasoning" in message_obj.model_extra:
                    native_reasoning = message_obj.model_extra["reasoning"]

                # Извлекаем мысли из тегов
                reply_text, tagged_reasoning = extract_and_strip_thoughts(raw_content)

                # Выводим размышления в лог консоли
                thoughts = native_reasoning or tagged_reasoning
                if thoughts:
                    print(f"\n[THINKING LOG - Channel {context_id}]:\n{thoughts.strip()}\n[END THINKING LOG]\n", flush=True)

                if not reply_text:
                    reply_text = "Не удалось сформулировать ответ по результатам поиска."
                
                history.append({"role": "assistant", "content": reply_text})
                self.bot.conversation_histories[context_id] = history
                save_conversations(self.bot.conversation_histories)
                
                log_last_message(history, "SEARCH_REPLY")

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

    @commands.command(name="note")
    async def add_note_cmd(self, ctx, *, text: str = None):
        """Быстрое сохранение текстовой заметки на диск напрямую."""
        if not text:
            await ctx.send("Укажите текст заметки. Пример: `!note купить хлеб`")
            return
        try:
            append_memory(f"({ctx.author.display_name}): {text}")
            await ctx.send("Заметка сохранена в `src/memories.txt`!")
        except Exception as e:
            print(f"[CMD_NOTE_ERR] {e}", flush=True)
            await ctx.send("Не удалось сохранить заметку из-за технической ошибки.")

    @commands.command(name="notes")
    async def read_notes_cmd(self, ctx):
        """Вывод всех ранее сохраненных заметок с диска."""
        try:
            notes = read_memories()
            if len(notes) > 1900:
                file_data = io.BytesIO(notes.encode("utf-8"))
                discord_file = discord.File(fp=file_data, filename="memories.txt")
                await ctx.send("Файл заметок слишком большой. Вот файл с диска:", file=discord_file)
            else:
                await ctx.send(f"**Содержимое `src/memories.txt`:**\n```\n{notes}\n```")
        except Exception as e:
            print(f"[CMD_NOTES_ERR] {e}", flush=True)
            await ctx.send("Не удалось прочитать файл заметок.")

async def setup(bot):
    await bot.add_cog(BotCommands(bot))
