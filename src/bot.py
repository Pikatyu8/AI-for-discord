import json
import asyncio
import discord
from discord.ext import commands
from datetime import datetime, timezone

from src.config import BASE_SYSTEM_INSTRUCTION, DISCORD_TOKEN, get_server_limits
from src.llm import generate_content_with_retry, TOOLS
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
    get_custom_system_instruction,
    check_and_increment_search,
    save_conversations,
    load_conversations,
    is_text_or_pdf_attachment,
    extract_text_from_pdf,
    get_max_active_channels,
    get_active_channels_count,
    register_channel_server,
    format_message_timestamp,
    format_message_with_metadata
)
from src.commands import setup as setup_commands

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

bot.conversation_histories = load_conversations()
bot.max_active_channels = 2
bot.thinking_channels = set()


async def get_message_reference(message):
    """
    Асинхронно получает сообщение, на которое был сделан ответ.
    """
    if not message.reference:
        return None
    try:
        if message.reference.cached_message:
            return message.reference.cached_message
        return await message.channel.fetch_message(message.reference.message_id)
    except Exception:
        return None


@bot.event
async def on_ready():
    print(f"Discord-бот {bot.user.name} успешно запущен через API-прокси в сети!", flush=True)


@bot.event
async def setup_hook():
    await setup_commands(bot)
    print("[SETUP] Все команды успешно загружены из Cog-модуля!", flush=True)


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Гарантируем инициализацию bot_mentions в самом начале, чтобы избежать UnboundLocalError
    bot_mentions = []
    if bot.user:
        if bot.user.name:
            bot_mentions.append(f"@{bot.user.name}")
        if bot.user.display_name:
            bot_mentions.append(f"@{bot.user.display_name}")
    if message.guild and message.guild.me:
        if message.guild.me.display_name:
            bot_mentions.append(f"@{message.guild.me.display_name}")
        if message.guild.me.name:
            bot_mentions.append(f"@{message.guild.me.name}")
            
    bot_mentions = sorted(list(set(m for m in bot_mentions if m and m != "@")), key=len, reverse=True)

    # =================== [ДЕТАЛЬНЫЙ ДЕБАГ-ЛОГ] ===================
    print(f"\n=================== [DEBUG ON_MESSAGE START] ===================", flush=True)
    print(f"Сообщение от пользователя: {message.author} (ID: {message.author.id})", flush=True)
    print(f"Канал ID: {message.channel.id} | Сервер ID: {message.guild.id if message.guild else 'DM'}", flush=True)
    print(f"Интент Message Content в коде бота: {bot.intents.message_content}", flush=True)
    print(f"Сырое содержание (message.content): '{message.content}'", flush=True)
    print(f"Очищенное содержание (message.clean_content): '{message.clean_content}'", flush=True)
    print(f"Количество вложений (attachments): {len(message.attachments)}", flush=True)
    print(f"Сгенерированные упоминания для удаления: {bot_mentions}", flush=True)
    
    temp_clean = message.clean_content
    for mention in bot_mentions:
        temp_clean = temp_clean.replace(mention, "")
    temp_clean = temp_clean.strip()
    print(f"Результат фильтрации (clean_text): '{temp_clean}'", flush=True)
    print(f"==================== [DEBUG ON_MESSAGE END] ====================\n", flush=True)
    # =============================================================

    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return

    context_id = message.channel.id
    is_active = context_id in bot.conversation_histories

    # Бот реагирует на прямое упоминание (ping) или на ЛС. 
    # В соответствии с требованиями, на ответы без пинга бот не реагирует.
    is_pinged = (bot.user in message.mentions) or isinstance(message.channel, discord.DMChannel)

    if not is_active and not is_pinged:
        return

    if not is_active and is_pinged:
        server_id_str = str(message.guild.id) if message.guild else f"DM_{context_id}"
        max_channels = get_max_active_channels(server_id_str)
        active_count = get_active_channels_count(bot, server_id_str)

        if active_count >= max_channels:
            await message.reply(
                f"Достигнут лимит активных каналов на этом сервере ({max_channels}). "
                "Попросите администратора изменить лимит через `!maxchannels` или отключите бота в другом канале командой `!stop`."
            )
            return
        
        bot.conversation_histories[context_id] = []
        register_channel_server(context_id, server_id_str)  # Регистрируем канал за этим сервером
        is_active = True
        save_conversations(bot.conversation_histories)
        print(f"[WAKEUP] Бот проснулся в канале {context_id} на сервере {server_id_str}", flush=True)


    if "http://" in message.content or "https://" in message.content:
        await asyncio.sleep(0.8)
        try:
            message = await message.channel.fetch_message(message.id)
        except Exception:
            pass

    clean_text = message.clean_content
    for mention in bot_mentions:
        clean_text = clean_text.replace(mention, "")
    clean_text = clean_text.strip()
    
    embeds_text = extract_embeds_text(message)
    if embeds_text:
        if clean_text:
            clean_text = f"{clean_text}\n\n[Текст из прикрепленных ссылок/эмбедов]:\n{embeds_text}"
        else:
            clean_text = f"[Текст из прикрепленных ссылок/эмбедов]:\n{embeds_text}"
            
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
                print(f"[Вложение] Успешно прочитан файл: {filename}", flush=True)
            except Exception as e:
                print(f"[Вложение] Ошибка при чтении документа {attachment.filename}: {e}", flush=True)

    image_urls = extract_image_urls(message)
    if image_urls:
        print(f"[Ссылки/Эмбеды] Найдено изображений по ссылкам: {len(image_urls)} в сообщении от {message.author.display_name}", flush=True)
        
    for url in image_urls:
        base64_url = await fetch_image_as_base64(url)
        if base64_url:
            parts.append({
                "type": "image_url",
                "image_url": {"url": base64_url}
            })

    server_id_str = str(message.guild.id) if message.guild else f"DM_{context_id}"
    guild_id = message.guild.id if message.guild else None
    limits = get_server_limits(guild_id)

    # Получаем ссылку на сообщение (если есть) для красивой разметки в контексте
    ref_msg = await get_message_reference(message)
    full_message_text = format_message_with_metadata(
        author_name=message.author.display_name,
        clean_text=clean_text,
        timestamp=message.created_at,
        ref_msg=ref_msg
    )

    if parts or clean_text:
        history = bot.conversation_histories[context_id]
        
        if len(parts) == 1 and parts[0]["type"] == "text":
            content_to_add = f"{full_message_text}\n{parts[0]['text']}"
        elif not parts and clean_text:
            content_to_add = full_message_text
        else:
            parts.append({
                "type": "text",
                "text": full_message_text
            })
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
            
        history = prune_history_local(history, max_tokens=limits["max_context_tokens"])
        bot.conversation_histories[context_id] = history
        save_conversations(bot.conversation_histories)

    if is_pinged:
        if not bot.conversation_histories[context_id]:
            init_timestamp = format_message_timestamp(datetime.now(timezone.utc))
            bot.conversation_histories[context_id] = [
                {"role": "user", "content": f"[{init_timestamp}] {message.author.display_name}: Привет"}
            ]
            save_conversations(bot.conversation_histories)
            
        history = bot.conversation_histories[context_id]

        custom_inst = get_custom_system_instruction(server_id_str)
        sys_inst = custom_inst if custom_inst else BASE_SYSTEM_INSTRUCTION

        # Добавляем жесткую инструкцию против повторения/генерации таймстампов
        sys_inst += (
            "\n\nПРИМЕЧАНИЕ: Все сообщения в истории снабжены метками времени в формате '[YYYY-MM-DD HH:MM:SS] Имя:'. "
            "Это сделано исключительно для твоего контекста. Тебе самому писать таймстампы или свое имя в начале ответа КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО. "
            "Отвечай сразу по существу, без метаданных в начале."
        )

        if context_id in bot.thinking_channels:
            sys_inst += (
                "\n\nВАЖНО: Перед тем как написать финальный краткий ответ, ты ДОЛЖЕН подробно поразмышлять. "
                "Свои подробные размышления обязательно запиши внутри тегов <think> и </think> в самом начале ответа. "
                "Пример: <think>Тут твои размышления...</think>Твой финальный ответ."
            )

        async with message.channel.typing():
            try:
                max_agent_loops = 3
                current_loop = 0
                reply_text = "Не удалось сформулировать ответ."
                
                while current_loop < max_agent_loops:
                    response = await generate_content_with_retry(history, sys_inst, tools=TOOLS)
                    tool_calls = getattr(response.choices[0].message, "tool_calls", None)
                    
                    if tool_calls:
                        current_loop += 1
                        message_obj = response.choices[0].message
                        content_raw = message_obj.content or ""
                        print(f"[API_RESPONSE] Вызовы инструментов: {len(tool_calls)}", flush=True)

                        native_reasoning = None
                        if hasattr(message_obj, "reasoning") and message_obj.reasoning:
                            native_reasoning = message_obj.reasoning
                        elif getattr(message_obj, "model_extra", None) and "reasoning" in message_obj.model_extra:
                            native_reasoning = message_obj.model_extra["reasoning"]

                        clean_content, tagged_reasoning = extract_and_strip_thoughts(content_raw)
                        
                        thoughts = native_reasoning or tagged_reasoning
                        if thoughts:
                            print(f"\n[THINKING LOG - Channel {context_id} (Перед инструментом)]:\n{thoughts.strip()}\n[END THINKING LOG]\n", flush=True)

                        bot_timestamp_str = format_message_timestamp(datetime.now(timezone.utc))
                        assistant_msg = {
                            "role": "assistant",
                            "content": f"[{bot_timestamp_str}] {clean_content}" if clean_content else "",
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
                            # 1. Поиск в сети
                            if tool_call.function.name == "web_search":
                                try:
                                    args = json.loads(tool_call.function.arguments)
                                except Exception:
                                    args = {"query": tool_call.function.arguments}
                                    
                                search_query = args.get("query", "")
                                if not search_query:
                                    search_query = tool_call.function.arguments
                                    
                                print(f"[TOOL_CALL] Запрос поиска в сети: \"{search_query}\"", flush=True)
                                
                                if not check_and_increment_search(server_id_str):
                                    tool_err = "Ошибка: Превышен дневной лимит поисков в сети (5 поисков в день) для этого сервера."
                                    print(f"[TOOL_CALL] Лимит поиска превышен для сервера {server_id_str}", flush=True)
                                    tool_msg = {
                                        "role": "tool",
                                        "tool_call_id": tool_call.id,
                                        "name": "web_search",
                                        "content": json.dumps([{"title": "Превышен лимит", "url": "", "snippet": tool_err}], ensure_ascii=False)
                                    }
                                    history.append(tool_msg)
                                else:
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

                            # 2. Сохранение заметок
                            elif tool_call.function.name == "save_note":
                                try:
                                    args = json.loads(tool_call.function.arguments)
                                except Exception:
                                    args = {"text": tool_call.function.arguments}
                                
                                note_text = args.get("text", "")
                                if note_text:
                                    saved = append_memory(
                                        server_id_str, 
                                        f"({message.author.display_name} через ИИ): {note_text}", 
                                        is_manual=False
                                    )
                                    if saved:
                                        tool_result = "Заметка успешно сохранена на диске."
                                    else:
                                        tool_result = f"Ошибка: Достигнут лимит автоматических заметок ({limits['max_tool_notes']}) на этом сервере."
                                else:
                                    tool_result = "Ошибка: текст заметки оказался пустым."

                                print(f"[TOOL_CALL] Сохранение заметки: \"{note_text}\" | Результат: {tool_result}", flush=True)
                                tool_msg = {
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "name": "save_note",
                                    "content": tool_result
                                }
                                history.append(tool_msg)

                            # 3. Чтение заметок
                            elif tool_call.function.name == "read_notes":
                                print(f"[TOOL_CALL] Чтение сохраненных заметок для {server_id_str}", flush=True)
                                notes_content = read_memories(server_id_str)
                                tool_msg = {
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "name": "read_notes",
                                    "content": notes_content
                                }
                                history.append(tool_msg)
                        
                        bot.conversation_histories[context_id] = prune_history_local(history, max_tokens=limits["max_context_tokens"])
                        save_conversations(bot.conversation_histories)
                        continue
                    else:
                        message_obj = response.choices[0].message
                        content_raw = message_obj.content or ""
                        print(f"[API_RESPONSE] Получен сырой текст ответа от модели: '{content_raw[:150]}...'", flush=True)

                        native_reasoning = None
                        if hasattr(message_obj, "reasoning") and message_obj.reasoning:
                            native_reasoning = message_obj.reasoning
                        elif getattr(message_obj, "model_extra", None) and "reasoning" in message_obj.model_extra:
                            native_reasoning = message_obj.model_extra["reasoning"]

                        reply_text, tagged_reasoning = extract_and_strip_thoughts(content_raw)

                        thoughts = native_reasoning or tagged_reasoning
                        if thoughts:
                            print(f"\n[THINKING LOG - Channel {context_id}]:\n{thoughts.strip()}\n[END THINKING LOG]\n", flush=True)

                        if not reply_text:
                            has_tool_call = any(msg.get("role") == "tool" for msg in history)
                            if has_tool_call:
                                reply_text = "Готово! Запрос успешно выполнен."
                            else:
                                reply_text = "Не удалось сформулировать ответ."

                        bot_timestamp_str = format_message_timestamp(datetime.now(timezone.utc))
                        history.append({"role": "assistant", "content": f"[{bot_timestamp_str}] {reply_text}"})
                        bot.conversation_histories[context_id] = prune_history_local(history, max_tokens=limits["max_context_tokens"])
                        save_conversations(bot.conversation_histories)
                        break
                
                log_last_message(history, "ASSISTANT_REPLY")

                if len(reply_text) > 2000:
                    await message.reply(reply_text[:1900] + "\n\n*(Ответ обрезан из-за лимитов Discord)*")
                else:
                    await message.reply(reply_text)
                    
            except Exception as e:
                print(f"Ошибка API: {e}", flush=True)
                await message.reply("Не удалось обработать запрос. Пожалуйста, попробуйте еще раз через полминуты.")
