"""
tool_fs.py — файловая система агента.
Все операции ограничены PROJECT_ROOT (yandi/).
"""
import os
from pathlib import Path
from typing import Union

PROJECT_ROOT = Path(__file__).parent.parent.parent  # yandi/


def _allowed_roots() -> list[Path]:
    roots = [PROJECT_ROOT.resolve()]
    extra = os.environ.get("AGENT_ALLOW_PATHS", "")
    for p in extra.split(":"):
        p = p.strip()
        if p:
            roots.append(Path(p).resolve())
    return roots


def _safe(path: Union[str, Path]) -> Path:
    p = Path(path)
    # Абсолютный путь — проверяем напрямую, относительный — относительно PROJECT_ROOT
    resolved = (p if p.is_absolute() else PROJECT_ROOT / p).resolve()
    for root in _allowed_roots():
        if str(resolved).startswith(str(root)):
            return resolved
    raise PermissionError(f"Доступ запрещён: {resolved}. Разрешённые пути: {_allowed_roots()}")


def read(path: str) -> str:
    return _safe(path).read_text(encoding="utf-8")


def write(path: str, content: str, append: bool = False) -> dict:
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    p.write_text(content, encoding="utf-8") if not append else open(p, "a").write(content)
    return {"ok": True, "path": str(p), "bytes": len(content.encode())}


def mkdir(path: str) -> dict:
    p = _safe(path)
    p.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": str(p)}


def ls(path: str = ".", pattern: str = "*") -> list[str]:
    p = _safe(path)
    return sorted(str(f.relative_to(PROJECT_ROOT)) for f in p.glob(pattern))


def exists(path: str) -> bool:
    try:
        return _safe(path).exists()
    except PermissionError:
        return False


def delete(path: str) -> dict:
    p = _safe(path)
    if p.is_dir():
        import shutil
        shutil.rmtree(p)
    else:
        p.unlink()
    return {"ok": True, "deleted": str(p)}


def info(path: str) -> dict:
    p = _safe(path)
    s = p.stat()
    return {
        "path": str(p.relative_to(PROJECT_ROOT)),
        "is_dir": p.is_dir(),
        "size": s.st_size,
        "modified": s.st_mtime,
    }
