"""
agent/ai_validator_redis.py — Отправка запросов в DeepSeek через Redis
"""

import json
import uuid
import time
import redis

REDIS_URL = "redis://127.0.0.1:6379"
QUEUE_KEY = "orch:ai:queue"
RESULT_PFX = "orch:ai:result:"
RESULT_TTL = 600

def send_to_deepseek(query: str, answer: str, frame: dict = None, sources: list = None) -> str:
    """Отправить запрос на валидацию в DeepSeek через Redis."""
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    
    # Формируем текст для DeepSeek
    text = f"Запрос: {query}\n\nОтвет для проверки:\n{answer}"
    
    task = {
        "task_id": task_id,
        "query": query,
        "answer": answer,
        "text": text,  # ← Добавлено поле text для расширения
        "frame": frame or {},
        "sources": sources or [],
        "ts": time.time()
    }
    
    r.rpush(QUEUE_KEY, json.dumps(task, ensure_ascii=False))
    r.setex(f"{RESULT_PFX}{task_id}", RESULT_TTL, "")
    
    return task_id

def get_result(task_id: str, timeout: int = 120) -> dict | None:
    """Получить результат валидации."""
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    key = f"{RESULT_PFX}{task_id}"
    
    start = time.time()
    while time.time() - start < timeout:
        data = r.get(key)
        if data:
            try:
                return json.loads(data)
            except:
                return {"raw": data, "verdict": "UNKNOWN", "error": "invalid json"}
        time.sleep(1)
    
    return {"verdict": "TIMEOUT", "error": "validation timeout"}
