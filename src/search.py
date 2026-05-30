import httpx
from src.config import TAVILY_API_KEY


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


async def perform_search_async(query: str, max_results: int = 5) -> list:
    """
    Выполняет поиск исключительно через Tavily.
    Если поиск не дал результатов или завершился ошибкой, возвращает сообщение об ошибке.
    """
    results = await perform_tavily_search(query, max_results)
    if results:
        return results

    print(f"[SEARCH] Tavily не вернул результатов для запроса '{query}'. Возвращаем ошибку.", flush=True)
    return [{
        "title": "Ошибка поиска",
        "url": "",
        "snippet": "Не удалось получить результаты поиска от сервиса Tavily или запрос не дал результатов."
    }]