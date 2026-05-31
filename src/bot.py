import json
import asyncio
import discord
from discord.ext import commands

from src.config import BASE_SYSTEM_INSTRUCTION, DISCORD_TOKEN
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
    save_conversations,
    load_conversations,
    is_text_or_pdf_attachment,
    extract_text_from_pdf
)
from src.commands import setup as setup_commands

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Состояние истории диалогов и каналов (загружается из файла)
bot.conversation_histories = load_conversations()
bot.max_active_channels = 2
bot.thinking_channels = set()  # Хранит ID каналов с включенным режимом размышлений


@bot.event
async def on_ready():
    print(f"Discord-бот {bot.user.name} успешно запущен через API-прокси в сети!", flush=True)


# Асинхронный хук инициализации бота для регистрации Cogs
@bot.event
async def setup_hook():
    await setup_commands(bot)
    print("[SETUP] Все команды успешно загружены из Cog-модуля!", flush=True)


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Обязательно проверяем контекст перед выполнением, чтобы префиксные команды "!" уходили в Cog
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
        save_conversations(bot.conversation_histories)
        print(f"[WAKEUP] Бот проснулся в канале {context_id}", flush=True)

    if "http://" in message.content or "https://" in message.content:
        await asyncio.sleep(0.8)
        try:
            message = await message.channel.fetch_message(message.id)
        except Exception:
            pass

    # Используем clean_content для автозамены упоминаний <@id> на имена пользователей
    clean_text = message.clean_content
    
    # Фильтруем упоминание самого бота из текста
    bot_mentions = [f"@{bot.user.name}", f"@{bot.user.display_name}"]
    if message.guild and message.guild.me:
        bot_mentions.append(f"@{message.guild.me.display_name}")
        bot_mentions.append(f"@{message.guild.me.name}")
        
    for mention in sorted(list(set(bot_mentions)), key=len, reverse=True):
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
        save_conversations(bot.conversation_histories)

    if is_pinged or is_reply_to_bot:
        if not bot.conversation_histories[context_id]:
            bot.conversation_histories[context_id] = [
                {"role": "user", "content": f"{message.author.display_name}: Привет"}
            ]
            save_conversations(bot.conversation_histories)
            
        history = bot.conversation_histories[context_id]

        # Настраиваем системную инструкцию в зависимости от режима размышлений
        sys_inst = BASE_SYSTEM_INSTRUCTION
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

                        # Вытаскиваем размышления, если они были сгенерированы перед вызовом инструмента
                        native_reasoning = None
                        if hasattr(message_obj, "reasoning") and message_obj.reasoning:
                            native_reasoning = message_obj.reasoning
                        elif getattr(message_obj, "model_extra", None) and "reasoning" in message_obj.model_extra:
                            native_reasoning = message_obj.model_extra["reasoning"]

                        clean_content, tagged_reasoning = extract_and_strip_thoughts(content_raw)
                        
                        thoughts = native_reasoning or tagged_reasoning
                        if thoughts:
                            print(f"\n[THINKING LOG - Channel {context_id} (Перед инструментом)]:\n{thoughts.strip()}\n[END THINKING LOG]\n", flush=True)

                        assistant_msg = {
                            "role": "assistant",
                            "content": clean_content,
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
                                    append_memory(f"({message.author.display_name} через ИИ): {note_text}")
                                    tool_result = "Заметка успешно сохранена на диске."
                                else:
                                    tool_result = "Ошибка: текст заметки оказался пустым."

                                print(f"[TOOL_CALL] Сохранение заметки: \"{note_text}\"", flush=True)
                                tool_msg = {
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "name": "save_note",
                                    "content": tool_result
                                }
                                history.append(tool_msg)

                            # 3. Чтение заметок
                            elif tool_call.function.name == "read_notes":
                                print(f"[TOOL_CALL] Чтение сохраненных заметок из memories.txt", flush=True)
                                notes_content = read_memories()
                                tool_msg = {
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "name": "read_notes",
                                    "content": notes_content
                                }
                                history.append(tool_msg)
                        
                        bot.conversation_histories[context_id] = history
                        save_conversations(bot.conversation_histories)
                        continue
                    else:
                        message_obj = response.choices[0].message
                        content_raw = message_obj.content or ""

                        # Проверяем нативные размышления
                        native_reasoning = None
                        if hasattr(message_obj, "reasoning") and message_obj.reasoning:
                            native_reasoning = message_obj.reasoning
                        elif getattr(message_obj, "model_extra", None) and "reasoning" in message_obj.model_extra:
                            native_reasoning = message_obj.model_extra["reasoning"]

                        # Извлекаем размышления из разметки
                        reply_text, tagged_reasoning = extract_and_strip_thoughts(content_raw)

                        thoughts = native_reasoning or tagged_reasoning
                        if thoughts:
                            print(f"\n[THINKING LOG - Channel {context_id}]:\n{thoughts.strip()}\n[END THINKING LOG]\n", flush=True)

                        if not reply_text:
                            reply_text = "Не удалось сформулировать ответ."

                        history.append({"role": "assistant", "content": reply_text})
                        bot.conversation_histories[context_id] = history
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
