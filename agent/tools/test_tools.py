"""Smoke test для всех tools."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.tools import tool, list_tools

def test_list_tools():
    t = list_tools()
    assert len(t) > 10
    print(f"  tools: {len(t)}")

def test_fs_write_read_delete():
    r = tool("fs.write", path="registry/queue/_test.txt", content="hello agent")
    assert r["ok"]
    content = tool("fs.read", path="registry/queue/_test.txt")
    assert content == "hello agent"
    tool("fs.delete", path="registry/queue/_test.txt")
    assert not tool("fs.exists", path="registry/queue/_test.txt")
    print("  fs: ok")

def test_fs_ls():
    r = tool("fs.ls", path="agent/tools")
    assert any("tool_fs" in f for f in r)
    print(f"  fs.ls: {len(r)} файлов")

def test_search_find():
    r = tool("search.find", pattern="*.py", path="agent/tools")
    assert len(r) > 0
    print(f"  search.find: {len(r)} файлов")

def test_search_grep():
    r = tool("search.grep", pattern="PROJECT_ROOT", path="agent/tools")
    assert len(r) > 0
    print(f"  search.grep: {len(r)} совпадений")

def test_system():
    r = tool("system.full_report")
    assert "os" in r and "memory" in r and "disk" in r
    mem = r["memory"]
    print(f"  system: RAM {mem.get('used_gb')}GB / {mem.get('total_gb')}GB")

def test_redis():
    r = tool("redis.stats")
    assert "redis_version" in r
    print(f"  redis: v{r['redis_version']}, {r['total_keys']} keys")

def test_shell():
    r = tool("shell.run", cmd="echo hello_from_agent")
    assert r["ok"] and "hello_from_agent" in r["stdout"]
    # banned
    r2 = tool("shell.run", cmd="rm -rf /")
    assert not r2["ok"]
    print("  shell: ok (banned cmd blocked)")

def test_unknown_tool():
    r = tool("nonexistent.tool")
    assert not r["ok"]
    assert "available" in r
    print("  unknown tool: correctly rejected")

if __name__ == "__main__":
    tests = [
        test_list_tools, test_fs_write_read_delete, test_fs_ls,
        test_search_find, test_search_grep, test_system,
        test_redis, test_shell, test_unknown_tool,
    ]
    passed = failed = 0
    for t in tests:
        try:
            print(f"▶ {t.__name__}")
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ FAIL: {e}")
            failed += 1
    print(f"\n{'✅' if failed == 0 else '❌'} {passed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)
