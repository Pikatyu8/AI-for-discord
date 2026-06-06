import json
import httpx
import asyncio
from src.config import TAVILY_API_KEY, GEMINI_API_KEY

# Безопасный импорт библиотеки google-genai с сохранением ошибки импорта
try:
    from google import genai
    from google.genai import types
    HAS_GOOGLE_GENAI = True
    GOOGLE_GENAI_IMPORT_ERROR = None
except ImportError as e:
    HAS_GOOGLE_GENAI = False
    GOOGLE_GENAI_IMPORT_ERROR = str(e)


def parse_queries_list(query_input) -> list[str]:
    """
    Преобразует входные данные поиска в список запросов.
    Поддерживает:
    - Одиночную строку.
    - JSON-строку со списком (например, '["запрос1", "запрос2"]').
    - Список строк.
    """
    if not query_input:
        return []
        
    if isinstance(query_input, list):
        return [str(q).strip() for q in query_input if q]
        
    if isinstance(query_input, str):
        query_str = query_input.strip()
        # Пытаемся распарсить как JSON список
        if query_str.startswith("[") and query_str.endswith("]"):
            try:
                parsed = json.loads(query_str)
                if isinstance(parsed, list):
                    return [str(q).strip() for q in parsed if q]
            except Exception:
                pass
        
        # Если это не JSON список, возвращаем как одиночный запрос
        return [query_str]
        
    return [str(query_input).strip()]


# src/search.py

async def perform_google_search(query: str, max_results: int = 5) -> list:
    """
    Выполняет поиск через нативный Google Search Grounding API (Gemini).
    С поддержкой автоматических повторных попыток при временных ошибках (429, 503, 500).
    """
    if not HAS_GOOGLE_GENAI:
        print(f"[GOOGLE_SEARCH] Ошибка: библиотека 'google-genai' не установлена или импорт завершился ошибкой ({GOOGLE_GENAI_IMPORT_ERROR}).", flush=True)
        return []

    if not GEMINI_API_KEY:
        print("[GOOGLE_SEARCH] Ошибка: GEMINI_API_KEY не настроен в конфигурации.", flush=True)
        return []

    print(f"[GOOGLE_SEARCH] Запуск поиска по запросу: '{query}'...", flush=True)
    
    max_retries = 3
    delay = 2.0  # Начальная задержка между попытками в секундах
    
    for attempt in range(max_retries):
        try:
            # Инициализируем клиент Google GenAI
            client = genai.Client(api_key=GEMINI_API_KEY)
            
            # Вызываем асинхронную генерацию контента с подключением инструмента Google Search
            response = await client.aio.models.generate_content(
                model='gemini-2.5-flash',
                contents=query,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                )
            )
            
            candidate = response.candidates[0] if response.candidates else None
            metadata = getattr(candidate, "grounding_metadata", None) if candidate else None
            
            if not metadata:
                print("[GOOGLE_SEARCH] Предупреждение: Google Search не вернул метаданных заземления.", flush=True)
                return []
                
            chunks = getattr(metadata, "grounding_chunks", []) or []
            supports = getattr(metadata, "grounding_supports", []) or []
            
            print(f"[GOOGLE_SEARCH] Найдено источников: {len(chunks)}", flush=True)
            
            # Извлекаем фрагменты текста (снайпеты) из заземляющих сегментов для каждого источника
            chunk_snippets = {}
            for support in supports:
                if support.segment and support.segment.text:
                    text = support.segment.text.strip()
                    indices = support.grounding_chunk_indices or []
                    for idx in indices:
                        chunk_snippets.setdefault(idx, []).append(text)
                        
            results = []
            for idx, chunk in enumerate(chunks):
                if len(results) >= max_results:
                    break
                    
                if chunk.web:
                    title = chunk.web.title or "Без названия"
                    uri = chunk.web.uri or ""
                    
                    # Объединяем найденные цитаты, либо формируем стандартное описание
                    snippets = chunk_snippets.get(idx, [])
                    snippet = " | ".join(snippets) if snippets else f"Информация найдена по адресу: {uri}"
                    
                    results.append({
                        "title": title,
                        "url": uri,
                        "snippet": snippet
                    })
                    
            return results

        except Exception as e:
            err_msg = str(e)
            # Определяем, является ли ошибка временной (сетевые проблемы, лимиты, перегрузка сервиса)
            is_transient = any(code in err_msg for code in ["429", "503", "500", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL"])
            
            if is_transient and attempt < max_retries - 1:
                print(f"[GOOGLE_SEARCH] Временная ошибка API ({err_msg}). Повторная попытка {attempt + 2}/{max_retries} через {delay} сек...", flush=True)
                await asyncio.sleep(delay)
                delay *= 2.0  # Экспоненциальное увеличение времени ожидания
            else:
                print(f"[GOOGLE_SEARCH_ERR] Ошибка при обращении к Google Search Grounding API: {e}", flush=True)
                return []


async def perform_multiple_google_searches(queries: list[str], max_results: int = 5) -> list:
    """
    Выполняет параллельный поиск по списку запросов исключительно через Google Search.
    Запросы запускаются с небольшой задержкой (staggered launch), чтобы избежать
    мгновенного превышения лимитов (Rate Limit / Quota) на бесплатном тарифе API.
    Ограничивает количество запросов до 5 штук.
    """
    queries = queries[:5]
    if not queries:
        return []
        
    print(f"[GOOGLE_SEARCH] Запуск множественного параллельного поиска для запросов: {queries}", flush=True)
    
    # Запускаем задачи с интервалом в 1.5 сек, чтобы сгладить пиковую нагрузку на API (RPM)
    tasks = []
    for idx, q in enumerate(queries):
        if idx > 0:
            await asyncio.sleep(1.5)
        tasks.append(perform_google_search(q, max_results))
        
    tasks_results = await asyncio.gather(*tasks, return_exceptions=True)
    
    combined_results = []
    for query, res in zip(queries, tasks_results):
        if isinstance(res, Exception):
            print(f"[GOOGLE_SEARCH_ERR] Ошибка параллельного поиска по запросу '{query}': {res}", flush=True)
            continue
        if res:
            # Помечаем результаты поисковым контекстом, чтобы модель понимала источник данных
            for item in res:
                item["query_context"] = query
            combined_results.extend(res)
            
    return combined_results

async def perform_tavily_search(query: str, max_results: int = 5) -> list:
    """
    Выполняет поиск через специализированный ИИ-поисковик Tavily.
    """
    if not TAVILY_API_KEY:
        print("[TAVILY_SEARCH] Ошибка: TAVILY_API_KEY не настроен в конфигурации.", flush=True)
        return []

    url = "https://api.tavily.com/search"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TAVILY_API_KEY}"
    }
    payload = {
        "query": query,
        "max_results": max_results
    }
    
    print(f"[TAVILY_SEARCH] Запуск поиска по запросу: '{query}'...", flush=True)
    try:
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(url, json=payload, headers=headers, timeout=10.0)
            
            if response.status_code != 200:
                print(f"[TAVILY_SEARCH] Ошибка API ({response.status_code}): {response.text}", flush=True)
                return []
                
            data = response.json()
            items = data.get("results", [])
            print(f"[TAVILY_SEARCH] Найдено результатов: {len(items)}", flush=True)
            
            results = []
            for item in items:
                results.append({
                    "title": item.get("title") or "Без названия",
                    "url": item.get("url") or "",
                    "snippet": item.get("content") or "Нет описания"
                })
            return results
    except Exception as e:
        print(f"[TAVILY_SEARCH_ERR] Ошибка при обращении к Tavily API: {e}", flush=True)
        return []


async def perform_search_async(query_input, max_results: int = 5, force_google_only: bool = False) -> list:
    """
    Выполняет поиск в интернете по одному или нескольким запросам.
    Если передан список из нескольких запросов или активен флаг force_google_only,
    поиск выполняется параллельно исключительно через Google Search (без Tavily).
    """
    queries = parse_queries_list(query_input)
    queries = queries[:5]
    
    if not queries:
        return []
        
    # Если запросов больше 1 или принудительно затребован только Google
    if len(queries) > 1 or force_google_only:
        results = await perform_multiple_google_searches(queries, max_results)
        if results:
            return results
        print("[SEARCH] Множественный Google поиск не вернул результатов или недоступен.", flush=True)
        return [{
            "title": "Результаты отсутствуют",
            "url": "",
            "snippet": f"Google Search не вернул результатов для запросов: {queries}."
        }]

    # Одиночный запрос
    single_query = queries[0]
    
    # Детальная проверка доступности Google Search
    google_available = True
    google_reasons = []

    if not HAS_GOOGLE_GENAI:
        google_available = False
        google_reasons.append(f"библиотека 'google-genai' не импортируется (ошибка: {GOOGLE_GENAI_IMPORT_ERROR})")
    
    if not GEMINI_API_KEY:
        google_available = False
        google_reasons.append("переменная GEMINI_API_KEY отсутствует или не загружена из .env")

    # 1. Пробуем использовать Google Search
    if google_available:
        results = await perform_google_search(single_query, max_results)
        if results:
            print(f"[SEARCH] Успешно получено {len(results)} результатов через нативный Google Search.", flush=True)
            return results
        print("[SEARCH] Нативный Google Search не вернул результатов, переключаемся на резервный Tavily...", flush=True)
    else:
        reasons_str = ", ".join(google_reasons)
        print(f"[SEARCH] Нативный Google Search недоступен по причине: {reasons_str}. Используем резервный Tavily...", flush=True)

    # 2. Резервный поиск через Tavily
    if TAVILY_API_KEY:
        results = await perform_tavily_search(single_query, max_results)
        if results:
            print(f"[SEARCH] Найдено {len(results)} результатов через резервный Tavily Search.", flush=True)
            return results

    print(f"[SEARCH] Все источники поиска не вернули результатов для запроса '{single_query}'. Возвращаем ошибку.", flush=True)
    return [{
        "title": "Ошибка поиска",
        "url": "",
        "snippet": "Не удалось получить результаты поиска от сервисов Google Search или Tavily."
    }]
