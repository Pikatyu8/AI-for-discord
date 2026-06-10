# EN
To make this work, you need:
1. A Discord bot token from [discord.com/developers](https://discord.com/developers) (Bot tab; make sure to grant the bot permissions and enable the Message Content Intent).
2. An AI access API key from [openrouter.ai](https://openrouter.ai/) (or another OpenAI-compatible provider).
3. (Optional) Web search keys: an API key from the specialized search engine [tavily.com](https://tavily.com/) or a Google API Key + Google CSE ID combination. If these keys are not provided, the bot will automatically fall back to a free search via DuckDuckGo (which works intermittently).
4. Insert all the obtained keys into your `.env` file (refer to the [.env.example](https://github.com/Pikatyu8/AI-for-discord/blob/main/.env.example) template for examples of how to fill in the fields).
5. Archive the project files into a single `.zip` file. Inside the archive, there must be: the `src` folder with all its contents, `main.py`, `.env`, `requirements.txt`, and `discloud.config`.
6. Go to [discloud.com](https://discloud.com/), log in, add your bot, and upload the created `.zip` archive.
7. Done! The bot should start up and respond to your mentions and commands.
8. Bot commands:
- - !load n - loads n messages from the chat, up to 201
- - !unload - unloads all messages from the chat
- - !export | json - displays the chat history in the bot (can be run with the json parameter)
- - !show - shows where the bot is active
- - !stop - pauses message logging in the channel
- - !maxchannels | n - sets the maximum number of active channels for the bot (default is 2)
- - !think | on/off - toggles the model's thinking mode
- - !search | info  - searches for information and prompts the bot to respond based on it
- - !search-google | info - same as !search but without a fallback to the bot
- - !note | text - creates an entry in the bot's memory for the current server
- - !notes - displays entries in memory
- - !show-descs - displays image descriptions with links to messages
- - !edit-desc | md5-hash/message link | desc - edits the image description based on the hash/message link if the current description is inaccurate
- - !help displays this text



---

# RU
Чтобы это заработало, вам нужно:
1. Токен от бота в дискорде с сайта [discord.com/developers](https://discord.com/developers) (вкладка Bot, не забудьте выдать боту права и включить Message Content Intent).
2. API-ключ для доступа к ИИ от [openrouter.ai](https://openrouter.ai/) (или другого OpenAI-совместимого провайдера).
3. (Опционально) Ключи для веб-поиска: API-ключ от специализированного поисковика [tavily.com](https://tavily.com/) или связка Google API Key + Google CSE ID. Если ключи не указаны, бот автоматически переключится на резервный бесплатный поиск через DuckDuckGo (работает с перебоями).
4. Все полученные ключи вставьте в свой файл `.env` (пример заполнения полей смотрите в шаблоне [.env.example](https://github.com/Pikatyu8/AI-for-discord/blob/main/.env.example)).
5. Заархивируйте файлы проекта в один `.zip` архив. Внутри архива должны находиться: папка `src` со всем содержимым, `main.py`, `.env`, `requirements.txt` и `discloud.config`.
6. Перейдите на [discloud.com](https://discloud.com/), авторизуйтесь, добавьте вашего бота и загрузите созданный `.zip` архив.
7. Готово, бот должен запуститься и отвечать на ваши упоминания и команды.
8. Команды бота:
- - !load n - загружает n сообщений в чате, до 201
- - !unload - выгружает все сообщения из чата
- - !export | json - показывает историю чата в боте (можно запустить с параметром json)
- - !show - показывает, где активен бот
- - !stop - приостанавливает запись сообщений в канале
- - !maxchannels | n - задает максимум активных каналов для бота (по умолчанию 2)
- - !think | on/off - переключает режим размышлений модели
- - !search | info  - ищет информацию и заставляет бота ответить на её основе
- - !search-google | info - тоже самое что и !search но без фолбека на тавили
- - !note | text - создает запись в память бота для текущего сервера
- - !notes - показывает записи в памяти
- - !show-descs - показывает описания картинок с ссылками на сообщения
- - !edit-desc | md5-hash/message link | desc - редактирует описание картинки по хешу/ссылке на сообщение, если текущее описание неточно
- - !help | en/ru - показывает этот текст
