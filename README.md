Чтобы это заработало, вам нужно:
1. Токен от бота в дискорде с сайта [discord.com/developers](https://discord.com/developers) (вкладка Bot, не забудьте выдать боту права и интент Message Content Intent)
2. API ключ для оплаты ИИ (например у меня - [proxyapi.ru](https://proxyapi.ru/))
3. Эти две вещи вы вставляете в свой .env, для удобства я оставил [.env.example](https://github.com/Pikatyu8/AI-for-discord/blob/main/.env.example) в качестве примера
4. Далее заархивируйте в .zip ваши 4 файла (.env, discloud.config, main.py, requirements.txt)
5. После идите на [discloud.com/](https://discloud.com/), зарегестрировавшись, создайте вашего бота и опубликуйте туда .zip
6. Готово, бот должен запуститься и отвечать на команды
7. Команды бота:
- - !load n - загружает n сообщений в чате, до 201
- - !unload - выгружает все сообщения из чата
- - !export | json - показывает историю чата в боте (можно запустить с параметром json)
- - !show - показывает где активен бот
- - !maxchannels - задает максимум активных каналов для бота (по умолчанию 2)
