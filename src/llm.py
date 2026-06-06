import asyncio
from openai import AsyncOpenAI
from src.config import PROXY_KEY, PROXY_BASE_URL, MODEL_NAME

client = AsyncOpenAI(api_key=PROXY_KEY, base_url=PROXY_BASE_URL)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск актуальной информации в интернете ИСКЛЮЧИТЕЛЬНО через Google Search. Поддерживает одновременный параллельный поиск по нескольким разным запросам (до 5 штук). Если после первого поиска информации всё ещё недостаточно для полного и точного ответа, ты должен совершить новый уточняющий поиск на следующем шаге (допускается до 3 последовательных шагов поиска).",
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        },
                        "description": "Список поисковых запросов для параллельного поиска (например: ['погода в Москве', 'курс доллара сегодня']). Используй для комплексных тем. Максимум 5 запросов."
                    },
                    "query": {
                        "type": "string",
                        "description": "Одиночный поисковый запрос (используй, если нужен только один конкретный запрос)."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "Сохранить важную информацию, заметку или воспоминание в текстовый файл memories.txt. Используй этот инструмент, когда пользователь просит тебя что-то запомнить, записать или сохранить.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Текст заметки или воспоминания для сохранения"
                    }
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_notes",
            "description": "Прочитать все ранее сохраненные заметки и воспоминания из файла memories.txt. Используй этот инструмент, когда пользователь спрашивает о том, что ты помнишь, или просит показать сохраненные заметки.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]


async def generate_content_with_retry(history: list, system_instruction: str, max_retries: int = 3, initial_delay: float = 2.0, tools=None):
    """
    Обертка для вызова API реселлера с автоповтором при временных ошибках.
    """
    delay = initial_delay
    messages = [{"role": "system", "content": system_instruction}] + history
    
    for attempt in range(max_retries):
        try:
            kwargs = {
                "model": MODEL_NAME,
                "messages": messages,
                "temperature": 0.7
            }
            if tools:
                kwargs["tools"] = tools
                
            response = await client.chat.completions.create(**kwargs)
            return response
        except Exception as e:
            err_msg = str(e)
            is_transient = any(code in err_msg for code in ["429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE"])
            
            if is_transient and attempt < max_retries - 1:
                print(f"[API] Временная ошибка ({err_msg}). Повторная попытка {attempt + 2}/{max_retries} через {delay} сек...", flush=True)
                await asyncio.sleep(delay)
                delay *= 2
            else:
                raise e
