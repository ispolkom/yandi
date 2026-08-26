"""
tool_redis.py — инспекция Redis для агента.
"""
import redis

REDIS_URL = "redis://127.0.0.1:6379"


def _r():
    return redis.from_url(REDIS_URL, decode_responses=True)


def keys(pattern: str = "*") -> list[str]:
    return sorted(_r().keys(pattern))


def get(key: str) -> str | None:
    return _r().get(key)


def set(key: str, value: str, ex: int = None) -> dict:
    _r().set(key, value, ex=ex)
    return {"ok": True, "key": key}


def delete(key: str) -> dict:
    n = _r().delete(key)
    return {"ok": True, "deleted": n}


def list_range(key: str, start: int = 0, end: int = 49) -> list[str]:
    return _r().lrange(key, start, end)


def stats() -> dict:
    info = _r().info()
    return {
        "used_memory_human": info.get("used_memory_human"),
        "connected_clients": info.get("connected_clients"),
        "total_keys": sum(
            _r().dbsize()
            for _ in [None]
        ),
        "uptime_days": info.get("uptime_in_days"),
        "redis_version": info.get("redis_version"),
    }


def scan(pattern: str = "*", count: int = 100) -> list[str]:
    r = _r()
    result = []
    cursor = 0
    while True:
        cursor, keys_ = r.scan(cursor, match=pattern, count=count)
        result.extend(keys_)
        if cursor == 0:
            break
    return sorted(result)
