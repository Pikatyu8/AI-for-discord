import json
import asyncio
import re
import discord
from discord.ext import commands
from datetime import datetime, timezone

from src.config import BASE_SYSTEM_INSTRUCTION, DISCORD_TOKEN, get_server_limits
from src.llm import generate_content_with_retry, TOOLS
from src.search import perform_search_async, parse_queries_list
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
    format_message_with_metadata,
    log_context_occupancy  # <-- ДОБАВЛЕН ИМПОРТ
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

    # === ДОБАВЛЕННЫЙ БЛОК: ПРОВЕРКА ПРАВ НА ОТПРАВКУ СООБЩЕНИЙ ===
    if message.guild and message.guild.me:
        permissions = message.channel.permissions_for(message.guild.me)
        if not permissions.send_messages:
            context_id = message.channel.id
            # Если канал без прав записи почему-то был активен в памяти, принудительно очищаем его
            if context_id in bot.conversation_histories:
                bot.conversation_histories.pop(context_id, None)
                unregister_channel_server(context_id)
                save_conversations(bot.conversation_histories)
                print(f"[PERMISSIONS] Канал {context_id} автоматически деактивирован: у бота нет прав на отправку сообщений.", flush=True)
                log_context_occupancy(bot)
            return  # Игнорируем любые действия в этом канале
    # =============================================================


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
                "Попросите администратора изменить лимит через `!maxchannels` or отключите бота в другом канале командой `!stop`."
            )
            return
        
        bot.conversation_histories[context_id] = []
        register_channel_server(context_id, server_id_str)
        is_active = True
        save_conversations(bot.conversation_histories)
        print(f"[WAKEUP] Бот проснулся в канале {context_id} на сервере {server_id_str}", flush=True)
        log_context_occupancy(bot)  # <-- ВЫЗОВ ПРИ ПРОБУЖДЕНИИ


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
        log_context_occupancy(bot)  # <-- ВЫЗОВ ПОСЛЕ ОБНОВЛЕНИЯ ИСТОРИИ СООБЩЕНИЕМ ПОЛЬЗОВАТЕЛЯ

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

        sys_inst += (
            "\n\nПРИМЕЧАНИЕ: Все сообщения в истории снабжены метками времени в формате '[YYYY-MM-DD HH:MM:SS] Имя:'. "
            "Это сделано исключительно для твоего контекста. Тебе самому писать таймстампы или свое имя в начале ответа КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО. "
            "Отвечай сразу по существу, без метаданных в начале."
        )

        # ДОБАВЛЕННЫЙ БЛОК ИНСТРУКЦИЙ ДЛЯ ПОСЛЕДОВАТЕЛЬНОГО ПОИСКА:
        sys_inst += (
            "\n\nТЕБЕ ДОСТУПЕН ПОСЛЕДОВАТЕЛЬНЫЙ МНОГОШАГОВЫЙ ПОИСК (ДО 3 ШАГОВ): "
            "Если после вызова `web_search` и получения результатов ты понимаешь, что информации для полного, "
            "точного и достоверного ответа всё ещё не хватает, либо появились новые важные факты, требующие "
            "уточнения, ты ДОЛЖЕН запустить инструмент `web_search` повторно с новыми скорректированными поисковыми запросами. "
            "Ты можешь повторять поиск последовательно до 3 раз за один диалог. Не пытайся выдумывать факты, "
            "если можешь найти их точное подтверждение в Google!"
        )

        if context_id in bot.thinking_channels:
            sys_inst += (
                "\n\nВАЖНО: Перед тем как написать финальный краткий ответ, ты ДОЛЖЕН подробно поразмышлять. "
                "Свои подробные размышления обязательно запиши внутри тегов <think> и </think> в самом начале ответа. "
                "Пример: <think>Тут твои размышления...</think>Твой финальный ответ."
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
                            if tool_call.function.name == "web_search":
                                try:
                                    args = json.loads(tool_call.function.arguments)
                                except Exception:
                                    args = {"query": tool_call.function.arguments}
                                    
                                if "queries" in args and isinstance(args["queries"], list):
                                    search_input = args["queries"]
                                elif "query" in args and args["query"]:
                                    search_input = args["query"]
                                else:
                                    search_input = tool_call.function.arguments
                                    
                                queries_to_run = parse_queries_list(search_input)[:5]
                                queries_display = ", ".join(f"*{q}*" for q in queries_to_run)
                                    
                                print(f"[TOOL_CALL] Запрос поиска в сети (список): {queries_to_run}", flush=True)
                                
                                if not check_and_increment_search(server_id_str):
                                    tool_err = "Ошибка: Превышен дневной лимит поисков в сети для этого сервера."
                                    print(f"[TOOL_CALL] Лимит поиска превышен для сервера {server_id_str}", flush=True)
                                    tool_msg = {
                                        "role": "tool",
                                        "tool_call_id": tool_call.id,
                                        "name": "web_search",
                                        "content": json.dumps([{"title": "Превышен лимит", "url": "", "snippet": tool_err}], ensure_ascii=False)
                                    }
                                    history.append(tool_msg)
                                else:
                                    status_msg = await message.reply(f"🔍 Ищу в сети: {queries_display}...")
                                    
                                    # ИЗМЕНЕНО: Добавлен именованный аргумент force_google_only=True
                                    search_results = await perform_search_async(queries_to_run, force_google_only=True)
                                    
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
                        reply_text = re.sub(r'^(\s*\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]\s*)+', '', reply_text).strip()

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
                                # === ДОБАВЛЕННЫЙ БЛОК ДЛЯ ИСПРАВЛЕНИЯ ЛОГИКИ ЦИКЛА ===
                # Если цикл завершился по лимиту шагов (current_loop == max_agent_loops),
                # но последнее сообщение в истории — это ответ от инструмента,
                # принудительно делаем финальный текстовый запрос к модели без инструментов.
                if history and history[-1].get("role") == "tool":
                    print(f"[AGENT] Достигнут лимит шагов ({max_agent_loops}). Запрашиваем финальный ответ...", flush=True)
                    response = await generate_content_with_retry(history, sys_inst, tools=None)
                    message_obj = response.choices[0].message
                    content_raw = message_obj.content or ""
                    
                    native_reasoning = None
                    if hasattr(message_obj, "reasoning") and message_obj.reasoning:
                        native_reasoning = message_obj.reasoning
                    elif getattr(message_obj, "model_extra", None) and "reasoning" in message_obj.model_extra:
                        native_reasoning = message_obj.model_extra["reasoning"]

                    reply_text, tagged_reasoning = extract_and_strip_thoughts(content_raw)
                    reply_text = re.sub(r'^(\s*\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]\s*)+', '', reply_text).strip()


                    thoughts = native_reasoning or tagged_reasoning
                    if thoughts:
                        print(f"\n[THINKING LOG - Channel {context_id} (Final)]: \n{thoughts.strip()}\n[END THINKING LOG]\n", flush=True)

                    if not reply_text:
                        reply_text = "Готово! Все запросы успешно выполнены."
                    
                    bot_timestamp_str = format_message_timestamp(datetime.now(timezone.utc))
                    history.append({"role": "assistant", "content": f"[{bot_timestamp_str}] {reply_text}"})
                    bot.conversation_histories[context_id] = prune_history_local(history, max_tokens=limits["max_context_tokens"])
                    save_conversations(bot.conversation_histories)
                # ===================================================

                log_last_message(history, "ASSISTANT_REPLY")
                log_context_occupancy(bot)

                if len(reply_text) > 2000:
                    await message.reply(reply_text[:1900] + "\n\n*(Ответ обрезан из-за лимитов Discord)*")
                else:
                    await message.reply(reply_text)

                    
            except Exception as e:
                print(f"Ошибка API: {e}", flush=True)
                await message.reply("Не удалось обработать запрос. Пожалуйста, попробуйте еще раз через полминуты.")
