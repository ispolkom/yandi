"""
agent/tools — router единой точки входа.

Использование:
    from agent.tools import tool
    result = tool("fs.read", path="pet/council_chat_server.py")
    result = tool("redis.keys", pattern="council:*")
    result = tool("system.full_report")
    result = tool("shell.run", cmd="pytest agent/tools/test_tools.py")
    result = tool("ai.ask_local", prompt="Привет!", model="heretic:q8")
"""

from agent.tools import tool_fs, tool_redis, tool_search, tool_system, tool_shell, tool_ai, tool_browser

_REGISTRY = {
    # Файловая система
    "fs.read":   lambda **kw: tool_fs.read(**kw),
    "fs.write":  lambda **kw: tool_fs.write(**kw),
    "fs.mkdir":  lambda **kw: tool_fs.mkdir(**kw),
    "fs.ls":     lambda **kw: tool_fs.ls(**kw),
    "fs.exists": lambda **kw: tool_fs.exists(**kw),
    "fs.delete": lambda **kw: tool_fs.delete(**kw),
    "fs.info":   lambda **kw: tool_fs.info(**kw),

    # Redis
    "redis.keys":       lambda **kw: tool_redis.keys(**kw),
    "redis.get":        lambda **kw: tool_redis.get(**kw),
    "redis.set":        lambda **kw: tool_redis.set(**kw),
    "redis.delete":     lambda **kw: tool_redis.delete(**kw),
    "redis.list_range": lambda **kw: tool_redis.list_range(**kw),
    "redis.stats":      lambda **kw: tool_redis.stats(**kw),
    "redis.scan":       lambda **kw: tool_redis.scan(**kw),

    # Поиск
    "search.find":      lambda **kw: tool_search.find(**kw),
    "search.grep":      lambda **kw: tool_search.grep(**kw),
    "search.tree":      lambda **kw: tool_search.file_tree(**kw),

    # Система
    "system.os":        lambda **kw: tool_system.os_info(**kw),
    "system.memory":    lambda **kw: tool_system.memory(**kw),
    "system.disk":      lambda **kw: tool_system.disk(**kw),
    "system.cpu":       lambda **kw: tool_system.cpu(**kw),
    "system.processes": lambda **kw: tool_system.processes(**kw),
    "system.services":  lambda **kw: tool_system.services(**kw),
    "system.full_report": lambda **kw: tool_system.full_report(**kw),

    # Shell
    "shell.run":      lambda **kw: tool_shell.run(**kw),
    "shell.allowed":  lambda **kw: tool_shell.allowed_commands(**kw),

    # AI
    "ai.ask_local":    lambda **kw: tool_ai.ask_local(**kw),
    "ai.ask_council":  lambda **kw: tool_ai.ask_council(**kw),
    "ai.build_plan":   lambda **kw: tool_ai.build_plan(**kw),
    "ai.review":       lambda **kw: tool_ai.review_result(**kw),

    # Browser / AI chat connections
    "browser.status":    lambda **kw: tool_browser.status(**kw),
    "browser.open":      lambda **kw: tool_browser.open_tab(**kw),
    "browser.connect":   lambda **kw: tool_browser.connect(**kw),
}


def tool(name: str, **kwargs):
    if name not in _REGISTRY:
        return {"ok": False, "error": f"Неизвестный инструмент: {name}",
                "available": list(_REGISTRY.keys())}
    try:
        return _REGISTRY[name](**kwargs)
    except Exception as e:
        return {"ok": False, "error": str(e), "tool": name}


def list_tools() -> list[str]:
    return list(_REGISTRY.keys())
