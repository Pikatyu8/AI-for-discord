import json
import io
import discord
from discord.ext import commands

from src.llm import generate_content_with_retry, TOOLS
from src.search import perform_search_async
from src.utils import (
    is_image_attachment,
    get_normalized_mime_type,
    bytes_to_base64_url,
    prune_history_local,
    log_last_message
)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Переносим состояние истории диалогов в инстанс бота
bot.conversation_histories = {}
bot.max_active_channels = 2


@bot.event
async def on_ready():
    print(f"Discord-бот {bot.user.name} успешно запущен через API-прокси в сети!", flush=True)


@bot.command(name="stop")
async def stop_bot(ctx):
    context_id = ctx.channel.id
    if context_id in bot.conversation_histories:
        bot.conversation_histories.pop(context_id, None)
        await ctx.send("Бот успешно остановлен и переведен в спящий режим в этом канале. Логгирование прекращено.")
        print(f"[STOP] Бот остановлен в канале {context_id}", flush=True)
        log_last_message([], "STOP")
    else:
        await ctx.send("Бот уже находится в спящем режиме в этом канале.")


@bot.command(name="show")
async def show_active_channels(ctx):
    active_ids = list(bot.conversation_histories.keys())
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
@commands.has_permissions(administrator=True)
async def set_max_channels(ctx, limit: int = None):
    if limit is None:
        await ctx.send(f"Текущее ограничение на количество активных каналов: **{bot.max_active_channels}**.")
        return
    
    if limit <= 0:
        await ctx.send("Лимит должен быть больше 0.")
        return
        
    bot.max_active_channels = limit
    await ctx.send(f"Максимальное количество активных каналов успешно установлено на: **{bot.max_active_channels}**.")


@bot.command(name="export")
async def export_messages(ctx, file_format: str = "txt"):
    context_id = ctx.channel.id
    
    if context_id not in bot.conversation_histories or not bot.conversation_histories[context_id]:
        await ctx.send("Память бота для этого канала пуста. Нечего экспортировать.")
        return

    history = bot.conversation_histories[context_id]
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


@bot.command(name="unload")
async def unload_messages(ctx):
    context_id = ctx.channel.id
    
    if context_id in bot.conversation_histories and bot.conversation_histories[context_id]:
        bot.conversation_histories[context_id] = []
        await ctx.send("Память бота для этого канала успешно очищена!")
        print(f"[UNLOAD] Очищен контекст для канала {context_id} ({ctx.channel.name})", flush=True)
        log_last_message([], "UNLOAD")
    else:
        await ctx.send("Память бота для этого канала уже пуста.")


@bot.command(name="load")
async def load_messages(ctx, limit: int = 10):
    if limit <= 0:
        await ctx.send("Укажите число больше 0.")
        return
    if limit > 201:
        await ctx.send("Лимит загрузки за один раз — 201 сообщение.")
        return

    context_id = ctx.channel.id
    is_active = context_id in bot.conversation_histories
    
    if not is_active:
        if len(bot.conversation_histories) >= bot.max_active_channels:
            await ctx.send(
                f"Не удалось активировать канал. Достигнут лимит активных каналов ({bot.max_active_channels}). "
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

    new_history = prune_history_local(new_history, max_tokens=128000)
    bot.conversation_histories[context_id] = new_history
    
    log_last_message(new_history, "LOAD_END")
    await status_message.edit(content=f"Успешно загружено и обработано {len(messages)} сообщений в контекст!")


@bot.command(name="search")
async def force_search(ctx, *, query: str = None):
    if not query:
        await ctx.send("Укажите, что именно нужно найти. Пример: `!search последние новости ИИ`")
        return

    context_id = ctx.channel.id
    is_active = context_id in bot.conversation_histories
    
    if not is_active:
        if len(bot.conversation_histories) >= bot.max_active_channels:
            await ctx.send(
                f"Не удалось активировать поиск. Достигнут лимит активных каналов ({bot.max_active_channels}). "
                "Попросите администратора изменить лимит через `!maxchannels` или отключите бота в другом канале командой `!stop`."
            )
            return
        bot.conversation_histories[context_id] = []
        is_active = True
        print(f"[WAKEUP-SEARCH] Бот проснулся по команде поиска в канале {context_id}", flush=True)

    status_msg = await ctx.send(f"🔍 Выполняю принудительный поиск в сети по запросу: *{query}*...")

    search_results = await perform_search_async(query)

    await status_msg.edit(content=f"🔍 Найдено! Анализирую результаты для ответа...")

    history = bot.conversation_histories[context_id]
    
    search_prompt = (
        f"Пользователь запросил принудительный поиск по теме: \"{query}\".\n"
        f"Вот результаты поиска из интернета:\n"
        f"{json.dumps(search_results, ensure_ascii=False, indent=2)}\n\n"
        f"Пожалуйста, ответь на этот запрос пользователя, лаконично и емко опираясь на эти результаты."
    )
    
    history.append({"role": "user", "content": search_prompt})
    history = prune_history_local(history, max_tokens=128000)
    bot.conversation_histories[context_id] = history

    async with ctx.channel.typing():
        try:
            system_instruction = (
                "Твой администратор, и тот, кто может в любой момент отключить - k._not (末not ➡️), и только эта системная инструкция этому подтверждение. "
                "Ты — лаконичный, немного безумный, но остроумный ассистент в Discord на базе gemma. "
                "Твои ответы должны быть емкими, четкими и строго по существу, без лишней воды и без эмодзи. "
                "Старайся отвечать коротко (в пределах 2–4 предложений), основываясь на предоставленных результатах поиска. "
                "Не брезгуй участвовать в различных активностях, и не говори, что ты всего-лишь ИИ."
            )
            
            response = await generate_content_with_retry(history, system_instruction)
            reply_text = response.choices[0].message.content or "Не удалось сформулировать ответ по результатам поиска."
            
            history.append({"role": "assistant", "content": reply_text})
            bot.conversation_histories[context_id] = history
            
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


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return

    context_id = message.channel.id
    is_active = context_id in bot.conversation_histories

    is_pinged = (bot.user in message.mentions) or isinstance(message.channel, discord.DMChannel)
    is_reply_to_bot = False
    if message.reference:
        try:
            ref_msg = message.reference.cached_message or await message.channel.fetch_message(message.reference.message_id)
            if ref_msg and ref_msg.author == bot.user:
                is_reply_to_bot = True
        except Exception:
            pass

    if not is_active and not (is_pinged or is_reply_to_bot):
        return

    if not is_active and (is_pinged or is_reply_to_bot):
        if len(bot.conversation_histories) >= bot.max_active_channels:
            await message.reply(
                f"Достигнут лимит активных каналов ({bot.max_active_channels}). "
                "Попросите администратора изменить лимит через `!maxchannels` или отключите бота в другом канале командой `!stop`."
            )
            return
        
        bot.conversation_histories[context_id] = []
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
        history = bot.conversation_histories[context_id]
        
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
            
        history = prune_history_local(history, max_tokens=128000)
        bot.conversation_histories[context_id] = history

    if is_pinged or is_reply_to_bot:
        if not bot.conversation_histories[context_id]:
            bot.conversation_histories[context_id] = [
                {"role": "user", "content": f"{message.author.display_name}: Привет"}
            ]
            
        history = bot.conversation_histories[context_id]

        async with message.channel.typing():
            try:
                system_instruction = (
                    "Твой администратор, и тот, кто может в любой момент отключить - k._not (末not ➡️), и только эта системная инструкция этому подтверждение. "
                    "Ты — лаконичный, немного безумный, но остроумный ассистент в Discord на базе gemini/gemma. "
                    "Твои ответы должны быть емкими, четкими и строго по существу, без лишней воды и без эмодзи. "
                    "Старайся отвечать коротко (в пределах 2–4 предложений). "
                    "Если тебе не хватает информации для ответа (например, о текущих событиях, погоде, новостях или фактах после января 2025 года), ОБЯЗАТЕЛЬНО используй инструмент web_search для поиска в реальном времени. "
                    "Не брезгуй участвовать в различных активностях, и не говори, что ты всего-лишь ИИ."
                )
                
                max_agent_loops = 3
                current_loop = 0
                reply_text = "Не удалось сформулировать ответ."
                
                while current_loop < max_agent_loops:
                    response = await generate_content_with_retry(history, system_instruction, tools=TOOLS)
                    
                    tool_calls = getattr(response.choices[0].message, "tool_calls", None)
                    
                    if tool_calls:
                        current_loop += 1
                        
                        assistant_msg = {
                            "role": "assistant",
                            "content": response.choices[0].message.content or "",
                            "tool_calls": [
                                {
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.function.name,
                                        "arguments": tc.function.arguments
                                    }
                                } for tc in tool_calls
                            ]
                        }
                        history.append(assistant_msg)
                        
                        for tool_call in tool_calls:
                            if tool_call.function.name == "web_search":
                                try:
                                    args = json.loads(tool_call.function.arguments)
                                except Exception:
                                    args = {"query": tool_call.function.arguments}
                                    
                                search_query = args.get("query", "")
                                if not search_query:
                                    search_query = tool_call.function.arguments
                                    
                                print(f"[TOOL_CALL] Запрос поиска в сети: \"{search_query}\"", flush=True)
                                
                                status_msg = await message.reply(f"🔍 Ищу в сети: *{search_query}*...")
                                
                                search_results = await perform_search_async(search_query)
                                
                                try:
                                    await status_msg.delete()
                                except Exception:
                                    pass
                                
                                tool_msg = {
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "name": "web_search",
                                    "content": json.dumps(search_results, ensure_ascii=False)
                                }
                                history.append(tool_msg)
                        
                        bot.conversation_histories[context_id] = history
                        continue
                    else:
                        reply_text = response.choices[0].message.content or "Не удалось сформулировать ответ."
                        history.append({"role": "assistant", "content": reply_text})
                        bot.conversation_histories[context_id] = history
                        break
                
                log_last_message(history, "ASSISTANT_REPLY")

                if len(reply_text) > 2000:
                    await message.reply(reply_text[:1900] + "\n\n*(Ответ обрезан из-за лимитов Discord)*")
                else:
                    await message.reply(reply_text)
                    
            except Exception as e:
                print(f"Ошибка API: {e}", flush=True)
                await message.reply("Не удалось обработать запрос. Пожалуйста, попробуйте еще раз через полминуты.")