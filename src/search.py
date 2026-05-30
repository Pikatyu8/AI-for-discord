import asyncio
import httpx
from src.config import GOOGLE_API_KEY, GOOGLE_CSE_ID, TAVILY_API_KEY

HAS_DDG = False
try:
    from duckduckgo_search import DDGS
    HAS_DDG = True
    print("[INIT] Резервная библиотека 'duckduckgo_search' импортирована успешно.", flush=True)
except Exception:
    HAS_DDG = False


async def perform_google_search(query: str, max_results: int = 5) -> list:
    """
    Выполняет официальный поиск через Google Custom Search API.
    """
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "num": max_results
    }
    
    print(f"[GOOGLE_SEARCH] Запуск поиска по запросу: '{query}'...", flush=True)
    try:
        async with httpx.AsyncClient() as http_client:
            response = await http_client.get(url, params=params, timeout=10.0)
            
            if response.status_code != 200:
                print(f"[GOOGLE_SEARCH] Ошибка API ({response.status_code}): {response.text}", flush=True)
                return []
                
            data = response.json()
            items = data.get("items", [])
            print(f"[GOOGLE_SEARCH] Найдено результатов: {len(items)}", flush=True)
            
            results = []
            for item in items:
                results.append({
                    "title": item.get("title") or "Без названия",
                    "url": item.get("link") or "",
                    "snippet": item.get("snippet") or "Нет описания"
                })
            return results
    except Exception as e:
        print(f"[GOOGLE_SEARCH_ERR] Ошибка при обращении к Google API: {e}", flush=True)
        return []


async def perform_tavily_search(query: str, max_results: int = 5) -> list:
    """
    Выполняет поиск через специализированный ИИ-поисковик Tavily.
    """
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


def perform_search_sync(query: str, max_results: int = 5) -> list:
    """
    Выполняет синхронный поиск в DuckDuckGo (резервный метод).
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    
    print(f"[DDG_SEARCH] Запуск резервного поиска по запросу: '{query}'...", flush=True)
    with DDGS(headers=headers) as ddgs:
        raw_results = ddgs.text(query, max_results=max_results)
        if raw_results is not None:
            raw_results = list(raw_results)
        else:
            raw_results = []
            
        print(f"[DDG_SEARCH] Найдено результатов: {len(raw_results)}", flush=True)
        results = []
        if raw_results:
            for r in raw_results:
                results.append({
                    "title": r.get("title") or r.get("name") or "Без названия",
                    "url": r.get("href") or r.get("url") or "",
                    "snippet": r.get("body") or r.get("snippet") or r.get("text") or "Нет описания"
                })
        return results


async def perform_search_async(query: str, max_results: int = 5) -> list:
    """
    Многоуровневый каскадный метод поиска.
    Поочередно пробует: Google (Custom) -> Tavily (AI-Search) -> DuckDuckGo (Fallback).
    """
    if GOOGLE_API_KEY and GOOGLE_CSE_ID:
        google_results = await perform_google_search(query, max_results)
        if google_results:
            return google_results
        print("[SEARCH] Google вернул 0 результатов или сработал со сбоем. Пробуем Tavily...", flush=True)

    if TAVILY_API_KEY:
        tavily_results = await perform_tavily_search(query, max_results)
        if tavily_results:
            return tavily_results
        print("[SEARCH] Tavily вернул 0 результатов или сработал со сбоем. Пробуем DuckDuckGo...", flush=True)

    if not HAS_DDG:
        print(f"[SEARCH] Попытка поиска по запросу '{query}' не удалась: резервный модуль DuckDuckGo выключен.", flush=True)
        return [{"title": "Ошибка", "url": "", "snippet": "Поисковые модули (Google/Tavily) не настроены, а DuckDuckGo не импортирован."}]
        
    try:
        results = await asyncio.to_thread(perform_search_sync, query, max_results)
        return results
    except Exception as e:
        print(f"[SEARCH_ERR] Исключение при поиске DuckDuckGo: {e}", flush=True)
        return [{"title": "Ошибка поиска", "url": "", "snippet": f"Все поисковые системы вернули ошибку или недоступны: {e}"}]